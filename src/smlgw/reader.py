"""Drive a transport, decode SML and deliver readings to a callback.

:class:`MeterReader` is deliberately transport-agnostic and free of MQTT or
config concerns so it can be unit tested against :class:`BytesTransport`.  It is
resilient by design: opening the port, reading and decoding are all guarded so
that a port which is missing at startup (the central complaint about the legacy
gateway) simply retries in the background instead of aborting the process.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .sml import SmlGetListResult, SmlStreamParser
from .transport import Transport

log = logging.getLogger(__name__)

ResultCallback = Callable[[SmlGetListResult], None]


class MeterReader:
    def __init__(
        self,
        transport: Transport,
        on_result: ResultCallback,
        *,
        name: str = "meter",
        verify_crc: bool = False,
        read_size: int = 512,
        reconnect_delay: float = 5.0,
        idle_delay: float = 0.2,
        on_state_change: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self.transport = transport
        self.on_result = on_result
        self.name = name
        self.read_size = read_size
        self.reconnect_delay = reconnect_delay
        # Wait this long after an empty read so a fast/simulated transport does
        # not busy-loop; a real serial port already paces via its read timeout.
        self.idle_delay = idle_delay
        self.on_state_change = on_state_change
        self._parser = SmlStreamParser(verify_crc=verify_crc)
        self._state = "stopped"
        self._last_error: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _set_state(self, state: str, error: str | None = None) -> None:
        self._state = state
        self._last_error = error
        if self.on_state_change is not None:
            try:
                self.on_state_change(state, error)
            except Exception:  # a broken observer must not kill the reader
                log.exception("state change callback failed for %s", self.name)

    def process_bytes(self, data: bytes) -> list[SmlGetListResult]:
        """Feed raw bytes to the decoder and dispatch any completed results."""
        results = self._parser.feed(data)
        for result in results:
            try:
                self.on_result(result)
            except Exception:
                log.exception("result callback failed for %s", self.name)
        return results

    def poll_once(self) -> list[SmlGetListResult]:
        """Read one chunk and process it. Returns decoded results (may be empty)."""
        data = self.transport.read(self.read_size)
        if not data:
            return []
        return self.process_bytes(data)

    def run(self, stop_event: threading.Event) -> None:
        """Blocking loop: (re)connect and read until *stop_event* is set."""
        self._set_state("connecting")
        while not stop_event.is_set():
            try:
                if not self.transport.is_open:
                    self.transport.open()
                    self._set_state("connected")
            except Exception as exc:  # noqa: BLE001 - report and retry, never crash
                self._set_state("error", str(exc))
                log.warning("%s: cannot open transport: %s", self.name, exc)
                stop_event.wait(self.reconnect_delay)
                continue

            try:
                results = self.poll_once()
                if results:
                    if self._state != "reading":
                        self._set_state("reading")
                elif self.idle_delay:
                    stop_event.wait(self.idle_delay)
            except Exception as exc:  # noqa: BLE001
                self._set_state("error", str(exc))
                log.warning("%s: read failed, will reconnect: %s", self.name, exc)
                try:
                    self.transport.close()
                except Exception:
                    pass
                stop_event.wait(self.reconnect_delay)

        try:
            self.transport.close()
        finally:
            self._set_state("stopped")
