import pytest

from smlgw.sml import SmlStreamParser, decode_field, extract_results
from smlgw.sml.builder import (
    ESCAPE,
    START,
    build_frame,
    build_get_list_res,
    build_message,
    build_meter_frame,
    encode_int,
    encode_octet,
    encode_uint,
)

SERVER_ID = bytes.fromhex("0a01484c5900010203")
READINGS = {
    "1-0:96.1.0*255": dict(value=bytes.fromhex("0102030405")),
    "1-0:1.8.0*255": dict(value=123456, scaler=-1, unit=30),
    "1-0:1.8.1*255": dict(value=100000, scaler=-1, unit=30),
    "1-0:1.8.2*255": dict(value=23456, scaler=-1, unit=30),
    "1-0:16.7.0*255": dict(value=550, scaler=-1, unit=27),
}


def test_tl_roundtrip_primitives():
    assert decode_field(encode_uint(300))[0] == 300
    assert decode_field(encode_int(-12345))[0] == -12345
    assert decode_field(encode_octet(b"\x00\x01\x02"))[0] == b"\x00\x01\x02"


def test_tl_roundtrip_long_octet_string():
    # Length > 15 forces a multi-byte TL field.
    data = bytes(range(50))
    value, pos = decode_field(encode_octet(data))
    assert value == data
    assert pos == len(encode_octet(data))


def test_full_frame_decodes_all_values():
    frame = build_meter_frame(SERVER_ID, READINGS)
    parser = SmlStreamParser(verify_crc=True)
    results = parser.feed(frame)
    assert len(results) == 1
    result = results[0]
    assert result.server_id == SERVER_ID
    values = result.as_dict()
    # 1.8.0 is unit 30 (Wh): 123456 * 10^-1 = 12345.6 Wh -> /1000 = 12.3456 kWh.
    assert values["1-0:1.8.0*255"].value_to_string() == "12.3456"
    assert values["1-0:16.7.0*255"].full_string() == "55 W"
    assert values["1-0:96.1.0*255"].value_to_string() == "0102030405"


def test_stream_reassembles_across_arbitrary_chunks():
    frame = build_meter_frame(SERVER_ID, READINGS)
    parser = SmlStreamParser()
    got = []
    for i in range(0, len(frame), 7):  # feed 7 bytes at a time
        got.extend(parser.feed(frame[i : i + 7]))
    assert len(got) == 1
    assert got[0].as_dict()["1-0:1.8.0*255"].value_to_string() == "12.3456"


def test_two_frames_back_to_back():
    frame = build_meter_frame(SERVER_ID, READINGS)
    parser = SmlStreamParser()
    results = parser.feed(frame + frame)
    assert len(results) == 2


def test_leading_garbage_is_skipped():
    frame = build_meter_frame(SERVER_ID, READINGS)
    parser = SmlStreamParser()
    results = parser.feed(b"\xde\xad\xbe\xef" + frame)
    assert len(results) == 1


def test_malformed_frame_does_not_break_stream():
    good = build_meter_frame(SERVER_ID, READINGS)
    # A frame whose body is nonsense but framing is intact.
    bad = build_frame([b"\x72\x63\x99\x99"])  # truncated list -> parse error
    parser = SmlStreamParser()
    results = parser.feed(bad + good)
    # The bad frame is dropped; the good one still arrives.
    assert any(r.as_dict().get("1-0:1.8.0*255") for r in results)


def test_crc_mismatch_is_dropped_when_verifying():
    frame = bytearray(build_meter_frame(SERVER_ID, READINGS))
    frame[-1] ^= 0xFF  # corrupt CRC
    parser = SmlStreamParser(verify_crc=True)
    assert parser.feed(bytes(frame)) == []


def test_crc_mismatch_ignored_when_not_verifying():
    frame = bytearray(build_meter_frame(SERVER_ID, READINGS))
    frame[-1] ^= 0xFF
    parser = SmlStreamParser(verify_crc=False)
    results = parser.feed(bytes(frame))
    assert len(results) == 1


def test_escaped_1b_bytes_in_payload_are_unescaped():
    # A server id containing four 0x1b bytes must survive transport escaping.
    server = b"\x1b\x1b\x1b\x1b\xaa"
    frame = build_meter_frame(server, {"1-0:1.8.0*255": dict(value=1, scaler=0, unit=30)})
    # Sanity: the builder doubled the escape in the wire bytes.
    assert ESCAPE * 2 in frame
    parser = SmlStreamParser()
    results = parser.feed(frame)
    assert results[0].server_id == server
