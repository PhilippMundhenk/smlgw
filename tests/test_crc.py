from smlgw.sml.crc import crc16_sml, crc16_x25


def test_x25_check_vector():
    # The canonical CRC-16/X-25 check value for "123456789" is 0x906E.
    assert crc16_x25(b"123456789") == 0x906E


def test_sml_is_byteswapped_x25():
    data = b"hello world"
    raw = crc16_x25(data)
    assert crc16_sml(data) == (((raw & 0xFF) << 8) | (raw >> 8))


def test_sml_frame_crc_roundtrips():
    from smlgw.sml.builder import build_meter_frame

    frame = build_meter_frame(b"\x01\x02", {"1-0:1.8.0*255": {"value": 1, "scaler": 0, "unit": 30}})
    calc = crc16_sml(frame[:-2])
    wire = (frame[-2] << 8) | frame[-1]
    assert calc == wire
