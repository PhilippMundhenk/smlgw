"""Optical PIN entry and recovery.

Some meters (eBZ, EMH and others) hide the detailed registers -- notably the
energy total ``1-0:1.8.0*255`` -- behind a four digit PIN that is entered
through the optical interface by pulsing an IR LED: a short flash acts as a
"button press".  The legacy ``pin.sh`` implemented exactly this by writing a
block of null bytes to toggle the LED and then grepping ``vzlogger.log`` to see
whether a value had appeared.

This module reimplements that faithfully but cleanly:

* PIN entry is a configurable pulse waveform (:class:`~smlgw.config.PinConfig`).
* Unlock is detected directly from the live SML stream (does the target OBIS
  code start arriving?) instead of scraping a log file.
* A :class:`BruteforceRunner` drives the full 0000-9999 sweep with progress,
  cancellation and a found-PIN result, for callers that have lost the PIN.

This is a recovery tool for the meter's rightful owner, who by law has both the
right to the data and the physical access to the optical port that operating it
requires.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import PinConfig
from .sml import SmlStreamParser
from .transport import Transport

log = logging.getLogger(__name__)

Sleep = Callable[[float], None]
Clock = Callable[[], float]


class PinController:
    """Enters a PIN over a transport and detects whether the meter unlocked."""

    def __init__(
        self,
        transport: Transport,
        config: PinConfig | None = None,
        *,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.transport = transport
        self.config = config or PinConfig()
        self.sleep = sleep
        self.clock = clock
        self._pulse = bytes.fromhex(self.config.pulse)

    def _pulse_once(self) -> None:
        self.transport.write(self._pulse)

    def reset(self) -> None:
        """Wake/reset the meter's input state before entering digits."""
        self._pulse_once()
        self.sleep(self.config.digit_gap)
        self._pulse_once()
        self.sleep(self.config.settle)

    def enter_pin(self, pin: str, *, reset: bool = True) -> None:
        """Enter *pin* digit by digit as pulse counts."""
        if not pin.isdigit():
            raise ValueError(f"pin must be digits only, got {pin!r}")
        if reset:
            self.reset()
        for digit in pin:
            for _ in range(int(digit)):
                self._pulse_once()
                self.sleep(self.config.digit_gap)
            self.sleep(self.config.group_gap)

    def detect_unlock(self, timeout: float | None = None, *, stop: threading.Event | None = None) -> bool:
        """Read the stream until the target register reports a real value.

        A meter that is still locked may keep emitting the target OBIS code with
        a value of zero, so -- as the legacy ``pin.sh`` did -- a numeric target
        only counts as unlocked once its value is non-zero.  Non-numeric targets
        fall back to mere presence.
        """
        if timeout is None:
            timeout = self.config.detect_timeout
        parser = SmlStreamParser()
        deadline = self.clock() + timeout
        target = self.config.detect_obis
        while self.clock() < deadline:
            if stop is not None and stop.is_set():
                return False
            data = self.transport.read(512)
            if data:
                for result in parser.feed(data):
                    for value in result.values:
                        if value.code != target:
                            continue
                        numeric = value.numeric_value()
                        if numeric is None or numeric != 0:
                            return True
            else:
                self.sleep(0.05)
        return False

    def try_pin(self, pin: str, *, stop: threading.Event | None = None) -> bool:
        """Enter *pin* and report whether the meter unlocked."""
        self.enter_pin(pin)
        return self.detect_unlock(stop=stop)


@dataclass
class BruteforceProgress:
    running: bool = False
    tried: int = 0
    total: int = 0
    current: str | None = None
    found: str | None = None
    finished: bool = False
    cancelled: bool = False
    error: str | None = None
    started_at: float | None = None
    updated_at: float | None = None

    def to_dict(self) -> dict:
        pct = (self.tried / self.total * 100.0) if self.total else 0.0
        return {**self.__dict__, "percent": round(pct, 2)}


class BruteforceRunner:
    """Sweeps the PIN space, reporting progress and honouring cancellation."""

    def __init__(
        self,
        controller: PinController,
        *,
        length: int = 4,
        start: int = 0,
        end: int | None = None,
        clock: Clock = time.monotonic,
        on_progress: Callable[[BruteforceProgress], None] | None = None,
    ) -> None:
        self.controller = controller
        self.length = length
        self.start = start
        self.end = end if end is not None else (10 ** length - 1)
        self.clock = clock
        self.on_progress = on_progress
        self.progress = BruteforceProgress(total=self.end - start + 1)
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def _emit(self) -> None:
        self.progress.updated_at = self.clock()
        if self.on_progress is not None:
            try:
                self.on_progress(self.progress)
            except Exception:
                log.exception("bruteforce progress callback failed")

    def run(self) -> str | None:
        """Run the sweep synchronously; return the PIN if found, else ``None``."""
        self.progress.running = True
        self.progress.started_at = self.clock()
        self._emit()
        try:
            for value in range(self.start, self.end + 1):
                if self._stop.is_set():
                    self.progress.cancelled = True
                    break
                pin = str(value).zfill(self.length)
                self.progress.current = pin
                self.progress.tried = value - self.start + 1
                self._emit()
                if self.controller.try_pin(pin, stop=self._stop):
                    self.progress.found = pin
                    log.info("bruteforce found PIN %s", pin)
                    break
            else:
                self.progress.current = None
        except Exception as exc:  # noqa: BLE001
            self.progress.error = str(exc)
            log.exception("bruteforce failed")
        finally:
            self.progress.running = False
            self.progress.finished = True
            self._emit()
        return self.progress.found
