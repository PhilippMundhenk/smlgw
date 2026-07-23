"""Decoder for the SML (Smart Message Language) protocol.

The decoder is split into three layers:

* :class:`SmlStreamParser` consumes raw bytes from the serial port, reassembles
  complete transport frames (handling the ``1b1b1b1b`` escape framing, padding
  and CRC) and hands each frame's payload to the message decoder.
* :func:`decode_field` decodes the recursive SML Type-Length encoding into plain
  Python values (``bytes``/``int``/``bool``/``list``/``None``).
* :func:`extract_results` walks the decoded messages and pulls out every
  ``SML_GetList.Res`` as a :class:`SmlGetListResult` of :class:`ObisValue`.

Only the pieces a residential meter actually emits are interpreted; unknown
message bodies are decoded structurally and then ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..obis import ObisValue, format_obis
from .crc import crc16_sml

START = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
ESCAPE = b"\x1b\x1b\x1b\x1b"
END_MARKER = 0x1A

SML_GETLIST_RES = 0x0701

# Sentinel: a scan pass consumed leading junk and wants the caller to rescan.
_RESCAN = object()


class SmlParseError(Exception):
    """Raised when a frame cannot be decoded structurally."""


@dataclass
class SmlGetListResult:
    """A decoded ``SML_GetList.Res``: the meter identity and its readings."""

    server_id: bytes | None
    values: list[ObisValue] = field(default_factory=list)

    def as_dict(self) -> dict[str, ObisValue]:
        return {v.code: v for v in self.values}


# --------------------------------------------------------------------------- #
# Type-Length decoding
# --------------------------------------------------------------------------- #

def decode_field(data: bytes, pos: int = 0) -> tuple[object, int]:
    """Decode one SML field starting at *pos*; return ``(value, new_pos)``."""
    if pos >= len(data):
        raise SmlParseError("unexpected end of data")

    first = data[pos]
    # A bare 0x00 is EndOfSmlMsg / an omitted optional value (NULL).
    if first == 0x00:
        return None, pos + 1

    type_bits = first & 0x70
    length = first & 0x0F
    tl_bytes = 1
    cont = first & 0x80
    cursor = pos + 1
    while cont:
        if cursor >= len(data):
            raise SmlParseError("truncated multi-byte TL field")
        nxt = data[cursor]
        length = (length << 4) | (nxt & 0x0F)
        cont = nxt & 0x80
        tl_bytes += 1
        cursor += 1

    if type_bits == 0x70:  # list: length is the element count
        items: list[object] = []
        for _ in range(length):
            value, cursor = decode_field(data, cursor)
            items.append(value)
        return items, cursor

    # primitive: length counts the TL bytes as well
    data_len = length - tl_bytes
    if data_len < 0 or cursor + data_len > len(data):
        raise SmlParseError("primitive field length out of range")
    raw = data[cursor : cursor + data_len]
    cursor += data_len

    if type_bits == 0x00:  # octet string
        return raw, cursor
    if type_bits == 0x50:  # signed integer
        return int.from_bytes(raw, "big", signed=True) if raw else 0, cursor
    if type_bits == 0x60:  # unsigned integer
        return int.from_bytes(raw, "big", signed=False) if raw else 0, cursor
    if type_bits == 0x40:  # boolean
        return raw != b"\x00" * len(raw), cursor
    raise SmlParseError(f"unknown SML type 0x{type_bits:02x}")


def decode_messages(payload: bytes) -> list[object]:
    """Decode every top-level SML message in *payload*."""
    messages: list[object] = []
    pos = 0
    length = len(payload)
    while pos < length:
        # Trailing padding shows up as bare 0x00 bytes; skip them.
        if payload[pos] == 0x00:
            pos += 1
            continue
        value, pos = decode_field(payload, pos)
        messages.append(value)
    return messages


# --------------------------------------------------------------------------- #
# Semantic extraction
# --------------------------------------------------------------------------- #

def _as_int(value: object, default: int | None = None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _entry_to_obis(entry: object) -> ObisValue | None:
    if not isinstance(entry, list) or len(entry) < 6:
        return None
    obj_name = entry[0]
    if not isinstance(obj_name, (bytes, bytearray)) or len(obj_name) not in (5, 6):
        return None
    status = _as_int(entry[1])
    val_time = entry[2]
    unit = _as_int(entry[3])
    scaler = _as_int(entry[4], 0)
    raw_value = entry[5]
    if isinstance(raw_value, list) or raw_value is None:
        return None

    value_time = None
    if isinstance(val_time, list) and len(val_time) == 2:
        value_time = _as_int(val_time[1])

    return ObisValue(
        code=format_obis(bytes(obj_name)),
        raw_value=raw_value,
        scaler=scaler or 0,
        unit_code=unit if unit is not None else 255,
        status=status,
        value_time=value_time,
    )


def _extract_from_message(message: object) -> SmlGetListResult | None:
    if not isinstance(message, list) or len(message) < 4:
        return None
    body = message[3]
    if not isinstance(body, list) or len(body) != 2:
        return None
    tag, content = body
    if tag != SML_GETLIST_RES or not isinstance(content, list) or len(content) < 5:
        return None

    server_id = content[1] if isinstance(content[1], (bytes, bytearray)) else None
    val_list = content[4]
    values: list[ObisValue] = []
    if isinstance(val_list, list):
        for entry in val_list:
            obis = _entry_to_obis(entry)
            if obis is not None:
                values.append(obis)
    return SmlGetListResult(
        server_id=bytes(server_id) if server_id is not None else None,
        values=values,
    )


def extract_results(payload: bytes) -> list[SmlGetListResult]:
    """Decode *payload* and return all ``SML_GetList.Res`` results."""
    results = []
    for message in decode_messages(payload):
        result = _extract_from_message(message)
        if result is not None:
            results.append(result)
    return results


# --------------------------------------------------------------------------- #
# Transport framing / streaming
# --------------------------------------------------------------------------- #

def _deframe(frame_body: bytes) -> bytes:
    """Undo the ``1b1b1b1b 1b1b1b1b`` escaping inside a frame body."""
    if ESCAPE * 2 not in frame_body:
        return frame_body
    out = bytearray()
    i = 0
    n = len(frame_body)
    while i < n:
        if frame_body[i : i + 8] == ESCAPE * 2:
            out += ESCAPE
            i += 8
        else:
            out.append(frame_body[i])
            i += 1
    return bytes(out)


class SmlStreamParser:
    """Reassembles SML frames from a byte stream and decodes their readings.

    Feed arbitrary chunks via :meth:`feed`; it returns the list of
    :class:`SmlGetListResult` completed by that chunk.  Partial frames are held
    until the rest of their bytes arrive.
    """

    def __init__(self, *, verify_crc: bool = False, max_buffer: int = 1 << 16) -> None:
        self.verify_crc = verify_crc
        self.max_buffer = max_buffer
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[SmlGetListResult]:
        self._buffer += chunk
        results: list[SmlGetListResult] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                break
            payload = frame
            try:
                results.extend(extract_results(payload))
            except SmlParseError:
                # A single malformed frame must never stop the stream.
                continue
        # Bound the buffer so a stream without valid start markers can't grow
        # without limit.
        if len(self._buffer) > self.max_buffer:
            keep = self._buffer.rfind(START[:4])
            self._buffer = self._buffer[keep:] if keep > 0 else bytearray()
        return results

    def _take_frame(self) -> bytes | None:
        """Pop the next complete frame payload from the buffer, or ``None``.

        Implemented as a loop rather than recursion: a stream full of start
        escapes with no valid end (garbage or a hostile peer) would otherwise
        recurse once per start marker and raise ``RecursionError``.
        """
        while True:
            frame = self._scan_one()
            if frame is not _RESCAN:
                return frame

    def _scan_one(self):
        """One scan pass. Returns a frame payload, ``None``, or ``_RESCAN``."""
        start = self._buffer.find(START)
        if start < 0:
            return None
        # Drop anything before the start escape.
        if start > 0:
            del self._buffer[:start]

        body_start = len(START)
        search = body_start
        buf = bytes(self._buffer)
        while True:
            esc = buf.find(ESCAPE, search)
            if esc < 0:
                return None  # need more bytes
            marker_pos = esc + len(ESCAPE)
            if marker_pos >= len(buf):
                return None
            marker = buf[marker_pos]
            if marker == 0x01 and buf[marker_pos : marker_pos + 4] == b"\x01\x01\x01\x01":
                # A new start escape before the end -> previous frame was junk.
                # Drop up to it and rescan (iteratively, via the outer loop).
                del self._buffer[:esc]
                return _RESCAN
            if buf[esc : esc + 8] == ESCAPE * 2:
                # Escaped data, not a real end. Skip past both escapes.
                search = esc + 8
                continue
            if marker == END_MARKER:
                # end block = ESCAPE + 0x1a + padByte + crcHi + crcLo
                end = marker_pos + 1 + 1 + 2
                if end > len(buf):
                    return None  # end block not fully received yet
                frame = buf[:end]
                pad = buf[marker_pos + 1]
                if self.verify_crc:
                    calc = crc16_sml(frame[:-2])
                    wire = (frame[-2] << 8) | frame[-1]
                    if calc != wire:
                        del self._buffer[:end]
                        return b""  # decoded to nothing; keeps the stream going
                body = _deframe(buf[body_start:esc])
                if pad:
                    body = body[: len(body) - pad] if pad <= len(body) else body
                del self._buffer[:end]
                return body
            # Unknown escape sequence; skip it and keep scanning.
            search = marker_pos
