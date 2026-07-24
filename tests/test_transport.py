from smlgw.transport import BytesTransport, SerialTransport


def test_is_network_detection():
    assert SerialTransport("/dev/ttyUSB0").is_network is False
    assert SerialTransport("COM3").is_network is False
    assert SerialTransport("socket://meter:5000").is_network is True
    assert SerialTransport("rfc2217://meter:5000").is_network is True


def test_network_url_opens_via_serial_for_url_loopback():
    # pyserial's in-process loop:// URL exercises the serial_for_url branch with
    # no real hardware or network: written bytes come back on read.
    t = SerialTransport("loop://", timeout=0.5)
    t.open()
    try:
        assert t.is_open is True
        t.write(b"\x01\x02\x03")
        assert t.read(3) == b"\x01\x02\x03"
    finally:
        t.close()
    assert t.is_open is False


def test_bytes_transport_records_writes_and_drains_reads():
    t = BytesTransport([b"ab", b"cd"])
    t.open()
    assert t.read(10) == b"ab"
    assert t.read(10) == b"cd"
    assert t.read(10) == b""  # exhausted
    t.write(b"xy")
    assert bytes(t.written) == b"xy"
