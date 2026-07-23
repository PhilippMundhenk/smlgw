"""Byte transports for talking to a meter's optical/serial interface.

The rest of the system only depends on the small :class:`Transport` protocol,
so a real serial port, an in-memory fake (used by the tests) or a replay file
are all interchangeable.  ``pyserial`` is imported lazily so the package can be
imported — and the web UI can run — on a machine without the serial extension
or without the port physically present.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Minimal bidirectional byte transport."""

    def open(self) -> None: ...

    def read(self, size: int) -> bytes:
        """Return up to *size* bytes; ``b""`` on timeout (never blocks forever)."""

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


class SerialTransport:
    """A :class:`Transport` backed by a hardware serial port via ``pyserial``."""

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None

    def open(self) -> None:
        import serial  # imported lazily; only needed for real hardware

        if self._serial is not None and self._serial.is_open:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
        )

    def read(self, size: int) -> bytes:
        if self._serial is None:
            raise RuntimeError("transport is not open")
        return self._serial.read(size)

    def write(self, data: bytes) -> int:
        if self._serial is None:
            raise RuntimeError("transport is not open")
        return self._serial.write(data)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open


class BytesTransport:
    """In-memory transport used by the test suite and the simulator.

    Reads drain a queue of pre-loaded byte chunks (looping if *repeat* is set);
    writes are recorded so PIN/command output can be asserted on.
    """

    def __init__(self, chunks: list[bytes] | None = None, *, repeat: bool = False) -> None:
        self._chunks = list(chunks or [])
        self._repeat = repeat
        self._pos = 0
        self.written = bytearray()
        self._open = False

    def open(self) -> None:
        self._open = True

    def feed(self, data: bytes) -> None:
        self._chunks.append(data)

    def read(self, size: int) -> bytes:
        if self._pos >= len(self._chunks):
            if self._repeat and self._chunks:
                self._pos = 0
            else:
                return b""
        chunk = self._chunks[self._pos]
        self._pos += 1
        return chunk[:size] if size and len(chunk) > size else chunk

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open
