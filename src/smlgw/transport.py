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
    """A :class:`Transport` backed by ``pyserial``.

    ``port`` may be a local device (``/dev/ttyUSB0``, ``COM3``) or a pyserial URL
    for a meter read on another machine and exported over the network, e.g.
    ``socket://meter-host:5000`` or ``rfc2217://meter-host:5000`` (as served by
    ``ser2net``/``socat``). URLs are opened via ``serial_for_url`` so the gateway
    can run anywhere, not only on the box the IR reader is plugged into.
    """

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None

    @property
    def is_network(self) -> bool:
        return "://" in self.port

    def open(self) -> None:
        import serial  # imported lazily; only needed for real hardware

        if self._serial is not None and self._serial.is_open:
            return
        if self.is_network:
            # serial_for_url handles socket://, rfc2217://, loop://, etc. Serial
            # line params are set before opening (RFC2217 honours them; plain
            # socket:// ignores them harmlessly).
            ser = serial.serial_for_url(self.port, do_not_open=True)
            ser.baudrate = self.baudrate
            ser.bytesize = serial.EIGHTBITS
            ser.parity = serial.PARITY_NONE
            ser.stopbits = serial.STOPBITS_ONE
            ser.timeout = self.timeout
            ser.open()
            self._serial = ser
        else:
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
