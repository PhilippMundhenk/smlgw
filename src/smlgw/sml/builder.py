"""Encoder for SML frames.

This is the mirror image of :mod:`smlgw.sml.parser`.  It is used by the test
suite and by :mod:`smlgw.simulator` to synthesise realistic meter output
(``SML_GetList.Res`` messages wrapped in a valid transport frame with CRC), so
that the decoder can be exercised without physical hardware.
"""

from __future__ import annotations

from .crc import crc16_sml

START = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
ESCAPE = b"\x1b\x1b\x1b\x1b"

TYPE_OCTET = 0x00
TYPE_BOOL = 0x40
TYPE_INT = 0x50
TYPE_UINT = 0x60
TYPE_LIST = 0x70


def _encode_primitive_tl(type_bits: int, data_len: int) -> bytes:
    """Encode a TL field for a primitive whose length counts the TL bytes."""
    tl_bytes = 1
    while data_len + tl_bytes >= (1 << (4 * tl_bytes)):
        tl_bytes += 1
    total = data_len + tl_bytes
    nibbles = [(total >> (4 * i)) & 0x0F for i in range(tl_bytes)][::-1]
    out = bytearray()
    for i, nib in enumerate(nibbles):
        byte = nib
        if i == 0:
            byte |= type_bits
        if i != len(nibbles) - 1:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def _encode_list_tl(count: int) -> bytes:
    """Encode a TL field for a list whose length is the element count."""
    tl_bytes = 1
    while count >= (1 << (4 * tl_bytes)):
        tl_bytes += 1
    nibbles = [(count >> (4 * i)) & 0x0F for i in range(tl_bytes)][::-1]
    out = bytearray()
    for i, nib in enumerate(nibbles):
        byte = nib
        if i == 0:
            byte |= TYPE_LIST
        if i != len(nibbles) - 1:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def encode_octet(data: bytes) -> bytes:
    return _encode_primitive_tl(TYPE_OCTET, len(data)) + data


def encode_uint(value: int, size: int | None = None) -> bytes:
    if value < 0:
        raise ValueError("unsigned value must be non-negative")
    if size is None:
        size = max(1, (value.bit_length() + 7) // 8)
    data = value.to_bytes(size, "big")
    return _encode_primitive_tl(TYPE_UINT, size) + data


def encode_int(value: int, size: int | None = None) -> bytes:
    if size is None:
        size = max(1, (value.bit_length() + 8) // 8)
    data = value.to_bytes(size, "big", signed=True)
    return _encode_primitive_tl(TYPE_INT, size) + data


def encode_bool(value: bool) -> bytes:
    return _encode_primitive_tl(TYPE_BOOL, 1) + (b"\x01" if value else b"\x00")


NULL = b"\x00"


def encode_list(items: list[bytes]) -> bytes:
    return _encode_list_tl(len(items)) + b"".join(items)


def encode_value(value: object) -> bytes:
    """Encode a Python value using its natural SML type."""
    if value is None:
        return NULL
    if isinstance(value, bool):
        return encode_bool(value)
    if isinstance(value, int):
        return encode_uint(value) if value >= 0 else encode_int(value)
    if isinstance(value, (bytes, bytearray)):
        return encode_octet(bytes(value))
    raise TypeError(f"cannot encode value of type {type(value)!r}")


def obis_to_bytes(code: str) -> bytes:
    """Convert an ``A-B:C.D.E*F`` code into its six object-name bytes."""
    left, rest = code.split("-", 1)
    b, rest = rest.split(":", 1)
    cde, f = rest.split("*", 1)
    c, d, e = cde.split(".")
    return bytes([int(left), int(b), int(c), int(d), int(e), int(f)])


def build_list_entry(
    code: str,
    value: object,
    *,
    scaler: int = 0,
    unit: int | None = None,
    status: int | None = None,
    value_time: int | None = None,
) -> bytes:
    """Build one ``SML_ListEntry`` (list of 7)."""
    fields = [
        encode_octet(obis_to_bytes(code)),
        NULL if status is None else encode_uint(status, 8),
        NULL if value_time is None else encode_list([encode_uint(1), encode_uint(value_time)]),
        NULL if unit is None else encode_uint(unit),
        NULL if scaler == 0 and unit is None else encode_int(scaler),
        encode_value(value),
        NULL,
    ]
    return encode_list(fields)


def build_get_list_res(server_id: bytes, entries: list[bytes], *, list_name: bytes | None = None) -> bytes:
    """Build a ``SML_GetList.Res`` message body (tag 0x0701)."""
    body = encode_list(
        [
            NULL,  # clientId
            encode_octet(server_id),
            NULL if list_name is None else encode_octet(list_name),
            NULL,  # actSensorTime
            encode_list(entries),
            NULL,  # listSignature
            NULL,  # actGatewayTime
        ]
    )
    message_body = encode_list([encode_uint(0x0701), body])
    return message_body


def build_message(message_body: bytes, *, transaction_id: bytes = b"\x00", group_no: int = 0) -> bytes:
    """Wrap a message body in an ``SML_Message`` (list of 6)."""
    return encode_list(
        [
            encode_octet(transaction_id),
            encode_uint(group_no),
            encode_uint(0),  # abortOnError
            message_body,
            encode_uint(0),  # per-message crc16 (not verified by our parser)
            NULL,  # endOfSmlMsg
        ]
    )


def build_frame(messages: list[bytes]) -> bytes:
    """Assemble complete transport frame (start escape, padding, CRC).

    Any literal ``1b1b1b1b`` inside the message data is escaped by doubling, as
    required by the SML transport layer.
    """
    payload = b"".join(messages).replace(ESCAPE, ESCAPE * 2)
    pad = (-len(payload)) % 4
    payload += b"\x00" * pad
    frame = START + payload + ESCAPE + bytes([0x1A, pad])
    crc = crc16_sml(frame)
    frame += bytes([crc >> 8, crc & 0xFF])
    return frame


def build_meter_frame(server_id: bytes, readings: dict[str, dict]) -> bytes:
    """Convenience: build a full frame from a mapping of OBIS code -> kwargs.

    Each value in *readings* is a dict accepted by :func:`build_list_entry`
    (must contain at least ``value``).
    """
    entries = [build_list_entry(code, **spec) for code, spec in readings.items()]
    body = build_get_list_res(server_id, entries)
    return build_frame([build_message(body)])
