"""Byte transports for talking to a meter's optical/serial interface.

The rest of the system only depends on the small :class:`Transport` protocol,
so a real serial port, an in-memory fake (used by the tests) or a replay file
are all interchangeable.  ``pyserial`` is imported lazily so the package can be
imported — and the web UI can run — on a machine without the serial extension
or without the port physically present.
"""

from __future__ import annotations

import threading
from collections import deque
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

    ``port`` may also be a ``usb:`` selector (``usb:serial=A50285BI``, see
    :mod:`smlgw.devices`) pinning the reader by serial number or USB socket.
    It is resolved to the current ``/dev/tty*`` name on every :meth:`open`, so
    a reconnect after an unplug finds the reader under its new name.
    """

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None
        self.device: str | None = None  # path actually opened (resolved selector)

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
            self.device = self.port
        else:
            from .devices import resolve_port

            device = resolve_port(self.port)
            try:
                # exclusive: take a flock on POSIX so a second meter (or a
                # `smlgw bruteforce` next to the service) cannot read the same
                # reader and tear its frames; Windows ports are exclusive anyway.
                self._serial = serial.Serial(
                    port=device,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self.timeout,
                    exclusive=True,
                )
            except serial.SerialException as exc:
                if "exclusively lock" in str(exc):
                    raise serial.SerialException(
                        f"{device} is already in use by another meter or program"
                    ) from exc
                raise
            self.device = device

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


class ReaderHub:
    """One physical reader shared by several meters (issue #5).

    The underlying transport is opened once (pyserial's exclusive lock stays
    in force against *other* programs) and every chunk read from it is handed
    to each attached :class:`SharedPort`, so every meter decodes complete
    frames instead of each getting torn halves of the stream.

    There is no extra thread: whichever port reads first performs the blocking
    read under a lock and queues a copy for the others, which find it waiting
    on their next read.  Only ports that are currently open receive copies, so
    a paused meter (e.g. held for PIN entry) does not accumulate a backlog.
    """

    #: chunks kept per port before the oldest are dropped (a stuck consumer
    #: must not grow memory without bound; SML frames resync on their own)
    max_backlog = 256

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._lock = threading.Lock()  # guards open/close/read and the queues
        # Writes (PIN pulses) must not wait up to a read timeout behind another
        # meter's blocking read: serial ports are full duplex.
        self._write_lock = threading.Lock()
        self._queues: dict[SharedPort, deque] = {}

    def port(self) -> SharedPort:
        """Return a new :class:`Transport` view on this reader."""
        return SharedPort(self)

    @property
    def device(self) -> str | None:
        return getattr(self.transport, "device", None)

    @property
    def open_ports(self) -> int:
        return len(self._queues)

    def _open(self, port: SharedPort) -> None:
        with self._lock:
            if not self.transport.is_open:
                self.transport.open()
            self._queues.setdefault(port, deque(maxlen=self.max_backlog))

    def _read(self, port: SharedPort, size: int) -> bytes:
        with self._lock:
            queue = self._queues.get(port)
            if queue is None:
                raise RuntimeError("transport is not open")
            if queue:
                return queue.popleft()
            try:
                data = self.transport.read(size)
            except Exception:
                # The reader is gone (unplugged, I/O error): close it for all
                # sharers so each one's reconnect loop reopens it.
                self._close_locked()
                raise
            if data:
                for other, other_queue in self._queues.items():
                    if other is not port:
                        other_queue.append(data)
            return data

    def _write(self, data: bytes) -> int:
        with self._write_lock:
            return self.transport.write(data)

    def _close(self, port: SharedPort) -> None:
        with self._lock:
            self._queues.pop(port, None)
            if not self._queues:
                self._close_locked()

    def _close_locked(self) -> None:
        self._queues.clear()
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001 - closing a dead port must not mask the cause
            pass

    def _is_open(self, port: SharedPort) -> bool:
        return port in self._queues and self.transport.is_open


class SharedPort:
    """A meter's view on a :class:`ReaderHub`; behaves like any :class:`Transport`."""

    def __init__(self, hub: ReaderHub) -> None:
        self.hub = hub

    @property
    def device(self) -> str | None:
        return self.hub.device

    def open(self) -> None:
        self.hub._open(self)

    def read(self, size: int) -> bytes:
        return self.hub._read(self, size)

    def write(self, data: bytes) -> int:
        return self.hub._write(data)

    def close(self) -> None:
        self.hub._close(self)

    @property
    def is_open(self) -> bool:
        return self.hub._is_open(self)
