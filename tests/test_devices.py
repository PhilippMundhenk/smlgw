"""Stable device selection (issue #1): usb: selectors, listing, end-to-end swap."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import wait_until

from smlgw import devices
from smlgw.devices import (
    DeviceSelectorError,
    is_selector,
    is_unstable_path,
    list_devices,
    parse_selector,
    resolve_port,
    stable_selector,
    validate_port,
)
from smlgw.transport import SerialTransport


def port(
    device, serial=None, vid=0x0403, pid=0x6001, location=None, description="FT232R"
):
    return SimpleNamespace(
        device=device,
        description=description,
        manufacturer="FTDI",
        product=description,
        serial_number=serial,
        vid=vid,
        pid=pid,
        location=location,
    )


READER_A = port("/dev/ttyUSB0", serial="A50285BI", location="1-1.2:1.0")
READER_B = port("/dev/ttyUSB1", serial="B7700XQ", location="1-1.3:1.0")
CH340_1 = port(
    "/dev/ttyUSB2", serial=None, vid=0x1A86, pid=0x7523, location="1-1.4.1:1.0"
)
CH340_2 = port(
    "/dev/ttyUSB3", serial=None, vid=0x1A86, pid=0x7523, location="1-1.4.2:1.0"
)
ONBOARD = port(
    "/dev/ttyAMA0",
    serial=None,
    vid=None,
    pid=None,
    location=None,
    description="ttyAMA0",
)
ALL = [READER_A, READER_B, CH340_1, CH340_2, ONBOARD]


# --------------------------------------------------------------------------- #
# parsing / validation
# --------------------------------------------------------------------------- #
def test_parse_single_and_combined_keys():
    assert parse_selector("usb:serial=A50285BI") == {"serial": "A50285BI"}
    assert parse_selector("USB: vid=0x10C4 , pid=ea60 ,serial=0001") == {
        "vid": 0x10C4,
        "pid": 0xEA60,
        "serial": "0001",
    }
    assert parse_selector("usb:location=1-1.2") == {"location": "1-1.2"}


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ("usb:", "empty"),
        ("usb:   ", "empty"),
        ("usb:serial", "key=value"),
        ("usb:serial=", "key=value"),
        ("usb:=abc", "key=value"),
        ("usb:serial=a,,pid=1", "key=value"),
        ("usb:name=foo", "unknown"),
        ("usb:serial=a,serial=b", "duplicate"),
        ("usb:vid=xyz", "hex"),
        ("usb:pid=123456", "hex"),
    ],
)
def test_parse_rejects_malformed(bad, fragment):
    with pytest.raises(DeviceSelectorError, match=fragment):
        parse_selector(bad)
    with pytest.raises(DeviceSelectorError):
        validate_port(bad)


def test_parse_requires_prefix():
    with pytest.raises(DeviceSelectorError):
        parse_selector("/dev/ttyUSB0")


def test_validate_accepts_non_selectors():
    for ok in (
        "",
        "/dev/ttyUSB0",
        "COM3",
        "socket://h:5000",
        "/dev/serial/by-id/usb-x",
        "sim",
    ):
        validate_port(ok)  # no exception


def test_is_selector_and_unstable_path():
    assert is_selector("usb:serial=x") and is_selector("  USB:serial=x")
    assert not is_selector("/dev/ttyUSB0")
    for unstable in (
        "/dev/ttyUSB0",
        "/dev/ttyUSB12",
        "/dev/ttyACM1",
        "/dev/ttyAMA0",
        "/dev/ttyS0",
    ):
        assert is_unstable_path(unstable), unstable
    for stable in (
        "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0",
        "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0-port0",
        "usb:serial=A50285BI",
        "socket://h:1",
        "COM3",
        "",
    ):
        assert not is_unstable_path(stable), stable


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_plain_paths_and_urls_pass_through_without_enumeration():
    # ports=[] would make any selector fail, so passing proves no lookup happens.
    for p in ("/dev/ttyUSB0", "COM3", "socket://h:5000", "rfc2217://h:1", "loop://"):
        assert resolve_port(p, ports=[]) == p


def test_resolve_by_serial_location_and_ids():
    assert resolve_port("usb:serial=A50285BI", ALL) == "/dev/ttyUSB0"
    assert resolve_port("usb:serial=B7700XQ", ALL) == "/dev/ttyUSB1"
    # location with or without the ":config.interface" suffix
    assert resolve_port("usb:location=1-1.4.2", ALL) == "/dev/ttyUSB3"
    assert resolve_port("usb:location=1-1.4.2:1.0", ALL) == "/dev/ttyUSB3"
    # a location prefix is NOT a match (1-1.4 is the hub, not a reader)
    with pytest.raises(DeviceSelectorError, match="no serial device"):
        resolve_port("usb:location=1-1.4", ALL)
    assert resolve_port("usb:vid=0403,pid=6001,serial=B7700XQ", ALL) == "/dev/ttyUSB1"


def test_resolve_follows_reader_after_renumbering():
    """The bug in #1: after a replug A and B swap kernel names."""
    swapped = [
        port("/dev/ttyUSB1", serial="A50285BI", location="1-1.2:1.0"),
        port("/dev/ttyUSB0", serial="B7700XQ", location="1-1.3:1.0"),
    ]
    assert resolve_port("usb:serial=A50285BI", swapped) == "/dev/ttyUSB1"
    assert resolve_port("usb:location=1-1.3", swapped) == "/dev/ttyUSB0"


def test_resolve_no_match_is_an_error_not_a_guess():
    with pytest.raises(
        DeviceSelectorError, match="no serial device matches.*plugged in"
    ):
        resolve_port("usb:serial=GONE", ALL)
    with pytest.raises(DeviceSelectorError, match="no serial device"):
        resolve_port("usb:serial=A50285BI", [])
    # serial is exact and case-sensitive
    with pytest.raises(DeviceSelectorError):
        resolve_port("usb:serial=a50285bi", ALL)


def test_resolve_ambiguous_is_an_error_listing_candidates():
    with pytest.raises(DeviceSelectorError, match=r"ambiguous.*ttyUSB2, /dev/ttyUSB3"):
        resolve_port("usb:vid=1a86,pid=7523", ALL)
    # identical serials (cheap clones) are ambiguous too
    clones = [port("/dev/ttyUSB0", serial="0001"), port("/dev/ttyUSB1", serial="0001")]
    with pytest.raises(DeviceSelectorError, match="ambiguous"):
        resolve_port("usb:serial=0001", clones)


def test_resolve_malformed_selector_raises():
    with pytest.raises(DeviceSelectorError, match="unknown"):
        resolve_port("usb:colour=red", ALL)


def test_resolve_uses_pyserial_enumeration_by_default(monkeypatch):
    monkeypatch.setattr(devices, "_comports", lambda: ALL)
    assert resolve_port("usb:serial=A50285BI") == "/dev/ttyUSB0"


# --------------------------------------------------------------------------- #
# suggestions / listing
# --------------------------------------------------------------------------- #
def test_stable_selector_prefers_serial_then_location():
    assert stable_selector(READER_A, ALL) == "usb:serial=A50285BI"
    assert stable_selector(CH340_1, ALL) == "usb:location=1-1.4.1"
    assert stable_selector(ONBOARD, ALL) is None  # not USB


def test_stable_selector_disambiguates_duplicate_serials():
    a = port(
        "/dev/ttyUSB0", serial="0001", vid=0x10C4, pid=0xEA60, location="1-1.2:1.0"
    )
    b = port(
        "/dev/ttyUSB1", serial="0001", vid=0x0403, pid=0x6001, location="1-1.3:1.0"
    )
    assert stable_selector(a, [a, b]) == "usb:vid=10c4,pid=ea60,serial=0001"
    c = port(
        "/dev/ttyUSB2", serial="0001", vid=0x10C4, pid=0xEA60, location="1-1.4:1.0"
    )
    # same model + same serial: only the socket tells them apart
    assert stable_selector(a, [a, c]) == "usb:location=1-1.2"
    # two interfaces of one multi-port adapter share the socket path
    i0 = port("/dev/ttyUSB0", serial=None, location="1-1.2:1.0")
    i1 = port("/dev/ttyUSB1", serial=None, location="1-1.2:1.1")
    assert stable_selector(i0, [i0, i1]) == "usb:location=1-1.2:1.0"
    assert resolve_port("usb:location=1-1.2:1.1", [i0, i1]) == "/dev/ttyUSB1"
    # nothing distinguishes these: no suggestion rather than a wrong one
    n0 = port("/dev/ttyUSB0", serial=None, location=None)
    n1 = port("/dev/ttyUSB1", serial=None, location=None)
    assert stable_selector(n0, [n0, n1]) is None


def test_every_suggestion_resolves_back_to_its_device():
    for info in ALL:
        sel = stable_selector(info, ALL)
        if sel:
            assert resolve_port(sel, ALL) == info.device


def test_list_devices_reports_symlinks(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyUSB0").write_text("")
    (dev / "ttyUSB1").write_text("")
    by_id, by_path = tmp_path / "by-id", tmp_path / "by-path"
    by_id.mkdir()
    by_path.mkdir()
    os.symlink(dev / "ttyUSB0", by_id / "usb-FTDI_FT232R_A50285BI-if00-port0")
    os.symlink(dev / "ttyUSB1", by_path / "platform-usb-0:1.3:1.0-port0")
    ports = [
        port(str(dev / "ttyUSB1"), serial=None, location="1-1.3:1.0"),
        port(str(dev / "ttyUSB0"), serial="A50285BI", location="1-1.2:1.0"),
    ]
    result = list_devices(ports, by_id_dir=str(by_id), by_path_dir=str(by_path))
    assert [d["device"] for d in result] == [str(dev / "ttyUSB0"), str(dev / "ttyUSB1")]
    first, second = result
    assert first["by_id"] == [str(by_id / "usb-FTDI_FT232R_A50285BI-if00-port0")]
    assert first["by_path"] == []
    assert first["selector"] == "usb:serial=A50285BI"
    assert (first["vid"], first["pid"]) == ("0403", "6001")
    assert second["by_path"] == [str(by_path / "platform-usb-0:1.3:1.0-port0")]
    assert second["selector"] == "usb:location=1-1.3"


def test_list_devices_without_pyserial_or_sysfs_is_empty(monkeypatch):
    def boom():
        raise OSError("no /sys")

    monkeypatch.setattr(devices, "_comports", boom)
    assert list_devices(by_id_dir="/nonexistent", by_path_dir="/nonexistent") == []


# --------------------------------------------------------------------------- #
# SerialTransport integration
# --------------------------------------------------------------------------- #
def test_transport_opens_resolved_device(monkeypatch):
    import serial

    opened = []

    class FakeSerial:
        def __init__(self, port, **kwargs):
            opened.append(port)
            self.is_open = True

        def close(self):
            self.is_open = False

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    ports = [READER_A, READER_B]
    monkeypatch.setattr(devices, "_comports", lambda: ports)

    t = SerialTransport("usb:serial=B7700XQ")
    assert t.device is None
    t.open()
    assert opened == ["/dev/ttyUSB1"] and t.device == "/dev/ttyUSB1"
    t.close()
    # replug: the kernel renumbers; the next open() follows the reader
    ports[:] = [
        port("/dev/ttyUSB0", serial="B7700XQ"),
        port("/dev/ttyUSB1", serial="A50285BI"),
    ]
    t.open()
    assert opened == ["/dev/ttyUSB1", "/dev/ttyUSB0"] and t.device == "/dev/ttyUSB0"
    t.close()
    # unplugged: open fails loudly, nothing is opened
    ports[:] = [port("/dev/ttyUSB0", serial="A50285BI")]
    with pytest.raises(DeviceSelectorError, match="no serial device"):
        t.open()
    assert len(opened) == 2 and not t.is_open


def test_transport_plain_path_still_opened_verbatim(monkeypatch):
    import serial

    opened = []
    monkeypatch.setattr(
        serial,
        "Serial",
        lambda port, **kw: (
            opened.append(port) or SimpleNamespace(is_open=True, close=lambda: None)
        ),
    )

    def no_enumeration():
        raise AssertionError("plain paths must not enumerate ports")

    monkeypatch.setattr(devices, "_comports", no_enumeration)
    t = SerialTransport("/dev/serial/by-id/usb-FTDI-if00-port0")
    t.open()
    assert opened == ["/dev/serial/by-id/usb-FTDI-if00-port0"]
    assert t.device == "/dev/serial/by-id/usb-FTDI-if00-port0"


def test_transport_url_records_device():
    t = SerialTransport("loop://", timeout=0.1)
    t.open()
    try:
        assert t.device == "loop://"
    finally:
        t.close()


# --------------------------------------------------------------------------- #
# End to end: two readers on real pseudo-terminals swap names
# --------------------------------------------------------------------------- #
class FakeReader:
    """An IR head on a pty: the master side streams this meter's SML frames."""

    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)  # the gateway opens the slave by path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                os.write(self.master, self.frame)
            except OSError:
                pass  # nobody has the slave open yet / buffer full
            self._stop.wait(0.05)

    def unplug(self) -> None:
        self._stop.set()
        self._thread.join(1)
        os.close(self.master)  # slave reads now fail -> reader reconnects


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs ptys")
def test_e2e_meters_follow_their_reader_when_kernel_names_swap(monkeypatch):
    from smlgw.config import AppConfig, Mapping, MeterConfig
    from smlgw.manager import MeterManager
    from smlgw.publisher import RecordingPublisher
    from smlgw.sml.builder import build_meter_frame

    def frame(total):
        return build_meter_frame(
            b"\x01", {"1-0:1.8.0*255": {"value": total, "scaler": 0, "unit": 30}}
        )

    heating_frame, house_frame = frame(111000), frame(222000)  # 111 kWh vs 222 kWh

    enumerated: list = []
    monkeypatch.setattr(devices, "_comports", lambda: list(enumerated))

    heating, house = FakeReader(heating_frame), FakeReader(house_frame)
    enumerated[:] = [
        port(heating.path, serial="HEAT-1", location="1-1.2:1.0"),
        port(house.path, serial="HOUSE-2", location="1-1.3:1.0"),
    ]
    cfg = AppConfig(
        meters=[
            MeterConfig(
                id="heating",
                port="usb:serial=HEAT-1",
                mappings=[Mapping("1-0:1.8.0*255", "power/heating/total")],
            ),
            MeterConfig(
                id="house",
                port="usb:location=1-1.3",
                mappings=[Mapping("1-0:1.8.0*255", "power/house/total")],
            ),
        ]
    )
    pub = RecordingPublisher()
    manager = MeterManager(cfg, pub)  # default factory: real SerialTransport + pyserial
    manager.start()
    replugged = []
    try:
        for meter_id in ("heating", "house"):
            manager.get_worker(meter_id).reader.reconnect_delay = 0.1
        assert wait_until(lambda: len(pub.topics) == 2, timeout=10)
        assert pub.topics == {"power/heating/total": "111", "power/house/total": "222"}
        status = {s["id"]: s for s in manager.status()}
        assert status["heating"]["device"] == heating.path
        assert status["house"]["device"] == house.path
        assert status["heating"]["unstable_port"] is False

        # Replug both: new kernel names, handed out in the opposite order.
        heating.unplug()
        house.unplug()
        new_house, new_heating = FakeReader(house_frame), FakeReader(heating_frame)
        replugged = [new_house, new_heating]
        enumerated[:] = [
            port(new_house.path, serial="HOUSE-2", location="1-1.3:1.0"),
            port(new_heating.path, serial="HEAT-1", location="1-1.2:1.0"),
        ]
        assert wait_until(
            lambda: (
                manager.get_worker("heating").transport.device == new_heating.path
                and manager.get_worker("house").transport.device == new_house.path
            ),
            timeout=15,
        )
        pub.messages.clear()
        assert wait_until(lambda: len(pub.topics) == 2, timeout=10)
        time.sleep(0.3)  # collect a few more frames: none may be crossed over
        assert set(pub.payloads_for("power/heating/total")) == {"111"}
        assert set(pub.payloads_for("power/house/total")) == {"222"}
    finally:
        manager.stop()
        for r in replugged:
            r.unplug()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs ptys")
def test_e2e_missing_reader_reports_error_then_recovers(monkeypatch):
    from smlgw.config import AppConfig, MeterConfig
    from smlgw.manager import MeterManager
    from smlgw.publisher import RecordingPublisher
    from smlgw.sml.builder import build_meter_frame

    enumerated: list = []
    monkeypatch.setattr(devices, "_comports", lambda: list(enumerated))
    cfg = AppConfig(meters=[MeterConfig(id="m", port="usb:serial=LATE")])
    manager = MeterManager(cfg, RecordingPublisher())
    manager.start()
    reader = None
    try:
        worker = manager.get_worker("m")
        worker.reader.reconnect_delay = 0.1
        assert wait_until(lambda: worker.reader.state == "error", timeout=5)
        assert "no serial device matches 'usb:serial=LATE'" in worker.reader.last_error
        assert manager.status()[0]["device"] is None

        f = build_meter_frame(
            b"\x01", {"1-0:1.8.0*255": {"value": 5000, "scaler": 0, "unit": 30}}
        )
        reader = FakeReader(f)
        enumerated[:] = [port(reader.path, serial="LATE")]
        assert wait_until(lambda: manager.discovered("m"), timeout=10)
        assert worker.transport.device == reader.path
    finally:
        manager.stop()
        if reader is not None:
            reader.unplug()
