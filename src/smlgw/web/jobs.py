"""Background PIN jobs for the web UI.

A meter's optical port can only be used by one thing at a time, so at most one
PIN job may run per meter.  Each job runs on its own thread, uses the manager's
``exclusive_transport`` to borrow the port from the reader, and exposes a
pollable progress dict.
"""

from __future__ import annotations

import logging
import threading

from ..manager import MeterManager
from ..pin import BruteforceRunner, BruteforceProgress, PinController

log = logging.getLogger(__name__)


class PinJob:
    """A single running PIN operation (send or bruteforce) for one meter."""

    def __init__(self, manager: MeterManager, meter_id: str, kind: str) -> None:
        self.manager = manager
        self.meter_id = meter_id
        self.kind = kind  # "send" or "bruteforce"
        self.progress = BruteforceProgress()
        self._thread: threading.Thread | None = None
        self._runner: BruteforceRunner | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _on_progress(self, progress: BruteforceProgress) -> None:
        self.progress = progress

    def start_send(self, pin: str) -> None:
        self._thread = threading.Thread(
            target=self._run_send, args=(pin,), name=f"pin-send-{self.meter_id}", daemon=True
        )
        self._thread.start()

    def start_bruteforce(self, length: int, start: int, end: int | None) -> None:
        self._thread = threading.Thread(
            target=self._run_bruteforce,
            args=(length, start, end),
            name=f"pin-brute-{self.meter_id}",
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        if self._runner is not None:
            self._runner.cancel()

    def _run_send(self, pin: str) -> None:
        self.progress = BruteforceProgress(running=True, total=1, current=pin)
        try:
            with self.manager.exclusive_transport(self.meter_id) as transport:
                controller = PinController(transport, self.manager.config.pin)
                unlocked = controller.try_pin(pin)
            self.progress.found = pin if unlocked else None
            self.progress.tried = 1
        except Exception as exc:  # noqa: BLE001
            self.progress.error = str(exc)
            log.exception("pin send failed for %s", self.meter_id)
        finally:
            self.progress.running = False
            self.progress.finished = True

    def _run_bruteforce(self, length: int, start: int, end: int | None) -> None:
        try:
            with self.manager.exclusive_transport(self.meter_id) as transport:
                controller = PinController(transport, self.manager.config.pin)
                self._runner = BruteforceRunner(
                    controller,
                    length=length,
                    start=start,
                    end=end,
                    on_progress=self._on_progress,
                )
                self._runner.run()
        except Exception as exc:  # noqa: BLE001
            self.progress.error = str(exc)
            self.progress.running = False
            self.progress.finished = True
            log.exception("bruteforce failed for %s", self.meter_id)


class PinJobManager:
    def __init__(self, manager: MeterManager) -> None:
        self.manager = manager
        self._jobs: dict[str, PinJob] = {}
        self._lock = threading.Lock()

    def get(self, meter_id: str) -> PinJob | None:
        return self._jobs.get(meter_id)

    def send(self, meter_id: str, pin: str) -> PinJob:
        # The whole check-create-start sequence runs under the lock so two
        # concurrent requests can never both launch a job on the same port.
        with self._lock:
            self._reject_if_running(meter_id)
            job = PinJob(self.manager, meter_id, "send")
            self._jobs[meter_id] = job
            job.start_send(pin)
            return job

    def bruteforce(self, meter_id: str, length: int, start: int, end: int | None) -> PinJob:
        with self._lock:
            self._reject_if_running(meter_id)
            job = PinJob(self.manager, meter_id, "bruteforce")
            self._jobs[meter_id] = job
            job.start_bruteforce(length, start, end)
            return job

    def _reject_if_running(self, meter_id: str) -> None:
        existing = self._jobs.get(meter_id)
        if existing is not None and existing.running:
            raise RuntimeError("a PIN operation is already running for this meter")

    def cancel(self, meter_id: str) -> bool:
        job = self._jobs.get(meter_id)
        # A single "send" is a short blocking sequence that cannot be interrupted
        # mid-flight; only a bruteforce sweep is cancellable.
        if job is None or not job.running or job.kind != "bruteforce":
            return False
        job.cancel()
        return True
