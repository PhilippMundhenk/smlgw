"""CRC16 as used by the SML transport layer.

SML uses the FCS defined by ISO/IEC 13239 (the X.25 / HDLC frame check
sequence): CRC-16 with polynomial 0x1021, reflected input/output, initial
value 0xFFFF and a final XOR of 0xFFFF.

The two CRC bytes carried in the SML end-of-message escape are transmitted in
big-endian order but are the byte-swapped X.25 result, which is what
:func:`crc16_sml` returns directly.
"""

from __future__ import annotations

# Precomputed table for the reflected 0x1021 polynomial (0x8408).
_TABLE: list[int] = []
for _byte in range(256):
    _crc = _byte
    for _ in range(8):
        if _crc & 0x0001:
            _crc = (_crc >> 1) ^ 0x8408
        else:
            _crc >>= 1
    _TABLE.append(_crc)


def crc16_x25(data: bytes) -> int:
    """Return the raw X.25 FCS of *data* (init 0xFFFF, xorout 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFF


def crc16_sml(data: bytes) -> int:
    """Return the CRC value as it appears on the wire in an SML frame.

    The value is the X.25 FCS with its two bytes swapped, i.e. what you get by
    reading the two trailing CRC bytes of a frame as a big-endian integer.
    """
    crc = crc16_x25(data)
    return ((crc & 0xFF) << 8) | (crc >> 8)
