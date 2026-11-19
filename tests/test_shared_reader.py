"""Several meters on one reader: detection (#3), allowed with a warning and shared (#5)."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest
import yaml
from conftest import wait_until
from starlette.testclient import TestClient

from smlgw import __main__ as cli
from smlgw import devices
from smlgw.config import AppConfig, Mapping, MeterConfig, load_config, save_config
from smlgw.devices import find_shared_readers, reader_key
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.sml.builder import build_meter_frame
from smlgw.transport import BytesTransport, ReaderHub, SerialTransport, SharedPort
from smlgw.web import create_app


def adapter(device, serial=None, location=None):
    return SimpleNamespace(
        device=device,
        description="FT232R",
        manufacturer=None,
        product=None,
        serial_number=serial,
        vid=0x0403,
        pid=0x6001,
        location=location,
    )


A = adapter("/dev/ttyUSB0", serial="AAA", location="1-1.2:1.0")
B = adapter("/dev/ttyUSB1", serial="BBB", location="1-1.3:1.0")


def M(meter_id, port, enabled=True):
    return MeterConfig(id=meter_id, port=port, enabled=enabled)


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_reader_key_resolves_every_spelling_of_one_adapter(tmp_path):
    ports = [A, B]
    dev = reader_key("/dev/ttyUSB0", ports)
    assert reader_key("usb:serial=AAA", ports) == dev
    assert reader_key("usb:location=1-1.2", ports) == dev
    assert reader_key(" /dev/ttyUSB0 ", ports) == dev
    assert reader_key("usb:serial=BBB", ports) != dev
    # a by-id symlink is the device it points to
    target = tmp_path / "ttyUSB5"
    target.write_text("")
    link = tmp_path / "usb-FTDI_AAA-if00-port0"
    os.symlink(target, link)
    assert reader_key(str(link), ports) == reader_key(str(target), ports)


def test_reader_key_edge_cases():
    assert reader_key("", []) is None and reader_key("   ", []) is None
    assert reader_key("socket://Box:5000", []) == reader_key("socket://box:5000", [])
    assert reader_key("socket://box:5000", []) != reader_key("socket://box:5001", [])
    # unplugged readers compare by their criteria, independent of key order/case
    assert reader_key("usb:serial=X,vid=0403", []) == reader_key(
        "USB:vid=403, serial=X", []
    )
    assert reader_key("usb:serial=X", []) != reader_key("usb:serial=Y", [])
    # malformed selectors are not a crash here (validation reports them)
    assert reader_key("usb:bogus", []) == ("raw", "usb:bogus")


def test_find_shared_readers_groups_enabled_meters_only():
    meters = [
        M("heat", "usb:serial=AAA"),
        M("dup", "/dev/ttyUSB0"),
        M("house", "usb:serial=BBB"),
        M("spare", "/dev/ttyUSB0", enabled=False),
        M("empty1", ""),
        M("empty2", ""),
    ]
    assert find_shared_readers(meters, [A, B]) == {"heat": ["dup"], "dup": ["heat"]}
    # three-way
    meters.append(M("third", "usb:location=1-1.2"))
    got = find_shared_readers(meters, [A, B])
    assert got["heat"] == ["dup", "third"] and got["third"] == ["heat", "dup"]
    assert "spare" not in got and "house" not in got and "empty1" not in got


def test_find_shared_readers_enumerates_only_when_a_selector_is_used(monkeypatch):
    def boom():
        raise AssertionError("must not enumerate for plain paths")

    monkeypatch.setattr(devices, "_comports", boom)
    assert find_shared_readers([M("a", "/dev/ttyUSB0"), M("b", "/dev/ttyUSB0")]) == {
        "a": ["b"],
        "b": ["a"],
    }
    # enumeration failing is not fatal: selectors compare by criteria
    monkeypatch.setattr(
        devices, "_comports", lambda: (_ for _ in ()).throw(OSError("no sysfs"))
    )
    assert find_shared_readers([M("a", "usb:serial=X"), M("b", "usb:serial=X")]) == {
        "a": ["b"],
        "b": ["a"],
    }


# --------------------------------------------------------------------------- #
# ReaderHub: one transport, every port gets every byte
# --------------------------------------------------------------------------- #
class CountingTransport(BytesTransport):
    def __init__(self, chunks):
        super().__init__(chunks)
        self.opens = 0
        self.closes = 0

    def open(self):
        self.opens += 1
        super().open()

    def close(self):
        self.closes += 1
        super().close()


def test_hub_fans_out_every_chunk_to_every_open_port():
    raw = CountingTransport([b"a", b"b", b"c"])
    hub = ReaderHub(raw)
    p1, p2 = hub.port(), hub.port()
    p1.open()
    p2.open()
    assert raw.opens == 1 and hub.open_ports == 2
    got1 = [p1.read(10) for _ in range(4)]
    got2 = [p2.read(10) for _ in range(3)]
    assert got1 == [b"a", b"b", b"c", b""]
    assert got2 == [b"a", b"b", b"c"]  # served from its queue, not re-read
    assert p1.device is None and isinstance(p1, SharedPort)


def test_hub_only_queues_for_open_ports_and_closes_with_the_last():
    raw = CountingTransport([b"1", b"2", b"3"])
    hub = ReaderHub(raw)
    p1, p2 = hub.port(), hub.port()
    p1.open()
    assert p1.read(10) == b"1"  # p2 not open yet: nothing queued for it
    p2.open()
    assert p1.read(10) == b"2"
    assert p2.read(10) == b"2"
    p2.close()
    assert raw.is_open and raw.closes == 0 and not p2.is_open
    assert p1.read(10) == b"3"
    p1.close()
    assert raw.closes == 1 and not raw.is_open and not p1.is_open
    with pytest.raises(RuntimeError, match="not open"):
        p1.read(1)


def test_hub_backlog_is_bounded():
    raw = BytesTransport([bytes([i]) for i in range(10)])
    hub = ReaderHub(raw)
    hub.max_backlog = 3
    fast, slow = hub.port(), hub.port()
    fast.open()
    slow.open()
    for _ in range(10):
        fast.read(1)
    # the slow port keeps only the newest chunks instead of growing forever
    assert [slow.read(1) for _ in range(3)] == [b"\x07", b"\x08", b"\x09"]


def test_hub_read_error_closes_reader_for_everyone_and_reopens():
    class Flaky(CountingTransport):
        fail = True

        def read(self, size):
            if self.fail:
                self.fail = False
                raise OSError("device disconnected")
            return super().read(size)

    raw = Flaky([b"x"])
    hub = ReaderHub(raw)
    p1, p2 = hub.port(), hub.port()
    p1.open()
    p2.open()
    with pytest.raises(OSError, match="disconnected"):
        p1.read(1)
    assert not p1.is_open and not p2.is_open and raw.closes == 1
    p2.open()  # reconnect loop of either meter reopens the one transport
    p1.open()
    assert raw.opens == 2
    assert p2.read(1) == b"x" and p1.read(1) == b"x"


def test_hub_write_does_not_wait_for_a_blocking_read():
    started, release = threading.Event(), threading.Event()

    class SlowRead(BytesTransport):
        def read(self, size):
            started.set()
            release.wait(2)
            return b""

    raw = SlowRead()
    hub = ReaderHub(raw)
    reader, writer = hub.port(), hub.port()
    reader.open()
    writer.open()
    t = threading.Thread(target=reader.read, args=(1,))
    t.start()
    assert started.wait(1)
    assert writer.write(b"\x00\x00") == 2  # returns while the read still blocks
    assert raw.written == b"\x00\x00"
    release.set()
    t.join(1)


# --------------------------------------------------------------------------- #
# web API: shared readers need confirmation, then work
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "_comports", lambda: [A, B])
    cfg = AppConfig(meters=[M("heat", "usb:serial=AAA")])
    path = os.path.join(tmp_path, "config.yaml")
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport()
    )
    manager.start()
    yield TestClient(create_app(manager, path)), manager, path
    manager.stop()


def test_create_on_used_reader_warns_then_saves_when_confirmed(env):
    client, manager, path = env
    for port in ("/dev/ttyUSB0", "usb:location=1-1.2", "usb:serial=AAA"):
        r = client.post("/api/meters", json={"id": "dup", "port": port})
        assert r.status_code == 409, port
        assert r.json()["shared_with"] == ["heat"]
        assert "same reader as meter 'heat'" in r.json()["detail"]
        assert "allow_shared_reader" in r.json()["detail"]
    assert manager.config.get_meter("dup") is None
    # only the literal true opts in
    body = {"id": "dup", "port": "/dev/ttyUSB0", "allow_shared_reader": "yes"}
    assert client.post("/api/meters", json=body).status_code == 409

    body["allow_shared_reader"] = True
    r = client.post("/api/meters", json=body)
    assert r.status_code == 201 and r.json()["shared_with"] == ["heat"]
    assert load_config(path).get_meter("dup").port == "/dev/ttyUSB0"
    status = {m["id"]: m for m in client.get("/api/status").json()["meters"]}
    assert status["heat"]["shared_with"] == ["dup"] and status["dup"][
        "shared_with"
    ] == ["heat"]
    # both meters are attached to one hub
    assert (
        manager.get_worker("heat").transport.hub
        is manager.get_worker("dup").transport.hub
    )

    # other readers, disabled spares and empty ports never ask
    r = client.post("/api/meters", json={"id": "house", "port": "usb:serial=BBB"})
    assert r.status_code == 201 and r.json()["shared_with"] == []
    r = client.post(
        "/api/meters", json={"id": "spare", "port": "/dev/ttyUSB0", "enabled": False}
    )
    assert r.status_code == 201
    assert client.post("/api/meters", json={"id": "blank"}).status_code == 201


def test_update_onto_used_reader_warns_then_saves_when_confirmed(env):
    client, _manager, path = env
    client.post("/api/meters", json={"id": "house", "port": "usb:serial=BBB"})
    client.post(
        "/api/meters", json={"id": "spare", "port": "/dev/ttyUSB0", "enabled": False}
    )

    r = client.put("/api/meters/house", json={"port": "/dev/ttyUSB0"})
    assert r.status_code == 409 and r.json()["shared_with"] == ["heat"]
    assert load_config(path).get_meter("house").port == "usb:serial=BBB"
    r = client.put(
        "/api/meters/house", json={"port": "/dev/ttyUSB0", "allow_shared_reader": True}
    )
    assert r.status_code == 200 and r.json()["shared_with"] == ["heat"]

    # enabling a spare on that reader asks too, naming both users
    r = client.put("/api/meters/spare", json={"enabled": True})
    assert r.status_code == 409 and sorted(r.json()["shared_with"]) == ["heat", "house"]
    assert load_config(path).get_meter("spare").enabled is False
    r = client.put(
        "/api/meters/spare", json={"enabled": True, "allow_shared_reader": True}
    )
    assert r.status_code == 200

    # re-saving with the same port (what the meter page does) does not ask again
    r = client.put(
        "/api/meters/house", json={"port": "/dev/ttyUSB0", "name": "H", "enabled": True}
    )
    assert r.status_code == 200
    # ...but joining a reader that gained a new user since does
    client.put("/api/meters/heat", json={"enabled": False})
    client.put("/api/meters/house", json={"port": "usb:serial=BBB"})
    r = client.put("/api/meters/heat", json={"enabled": True})  # spare uses AAA: asks
    assert r.status_code == 409 and r.json()["shared_with"] == ["spare"]
    client.put("/api/meters/heat", json={"enabled": True, "allow_shared_reader": True})
    r = client.put("/api/meters/house", json={"port": "usb:serial=AAA"})
    assert r.status_code == 409 and sorted(r.json()["shared_with"]) == ["heat", "spare"]
    client.put(
        "/api/meters/house", json={"port": "/dev/ttyUSB0", "allow_shared_reader": True}
    )
    # renaming a meter that already shares needs no confirmation
    r = client.put("/api/meters/house", json={"name": "House"})
    assert r.status_code == 200 and sorted(r.json()["shared_with"]) == ["heat", "spare"]
    # moving away is never questioned
    r = client.put("/api/meters/house", json={"port": "usb:serial=BBB"})
    assert r.status_code == 200 and r.json()["shared_with"] == []


def test_shared_reader_is_shown_in_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "_comports", lambda: [A, B])
    cfg = AppConfig(meters=[M("heat", "/dev/ttyUSB0"), M("copy", "usb:serial=AAA")])
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport()
    )
    manager.start()
    try:
        client = TestClient(create_app(manager, os.path.join(tmp_path, "c.yaml")))
        assert "⇄ shares reader with copy" in client.get("/settings").text
        page = client.get("/meter/copy").text
        assert 'id="m-shared"' in page and 'href="/meter/heat"' in page
        assert "allow_shared_reader: true" in page  # the confirm-and-resend helper
        client.put("/api/meters/copy", json={"port": "usb:serial=BBB"})
        assert 'id="m-shared"' not in client.get("/meter/copy").text
        assert "⇄" not in client.get("/settings").text
    finally:
        manager.stop()


def test_import_allows_shared_reader_and_warns(env):
    client, _manager, path = env
    data = {
        "meters": [
            {"id": "a", "port": "/dev/ttyUSB1"},
            {"id": "b", "port": "usb:serial=BBB"},
        ]
    }
    r = client.post(
        "/api/config/import",
        content=yaml.safe_dump(data),
        headers={"Content-Type": "application/x-yaml"},
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == [
        "meter 'a' shares its reader with 'b'",
        "meter 'b' shares its reader with 'a'",
    ]
    assert [m.id for m in load_config(path).meters] == ["a", "b"]
    data["meters"][1]["port"] = "usb:serial=AAA"
    r = client.post(
        "/api/config/import",
        content=yaml.safe_dump(data),
        headers={"Content-Type": "application/x-yaml"},
    )
    assert r.status_code == 200 and r.json()["warnings"] == []


def test_status_survives_detection_failure(env, monkeypatch):
    client, _manager, _ = env

    def broken(meters, ports=None):
        raise RuntimeError("boom")

    import smlgw.manager as manager_mod

    monkeypatch.setattr(manager_mod, "find_shared_readers", broken)
    r = client.get("/api/status")
    assert r.status_code == 200 and r.json()["meters"][0]["shared_with"] == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_ports_notes_shared_readers(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(devices, "_comports", lambda: [A, B])
    path = os.path.join(tmp_path, "config.yaml")
    save_config(
        AppConfig(meters=[M("heat", "usb:serial=AAA"), M("dup", "/dev/ttyUSB0")]), path
    )
    assert cli.main(["ports", "--config", path]) == 0
    out = capsys.readouterr().out
    assert "note: meter dup shares its reader with heat (same readings)" in out
    assert "note: meter heat shares its reader with dup (same readings)" in out
    assert "ERROR" not in out


# --------------------------------------------------------------------------- #
# runtime, end to end on real ptys with pyserial
# --------------------------------------------------------------------------- #
class PtyMeter:
    def __init__(self, total):
        self.frame = build_meter_frame(
            b"\x01", {"1-0:1.8.0*255": {"value": total, "scaler": 0, "unit": 30}}
        )
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        while not self.stop.is_set():
            try:
                os.write(self.master, self.frame)
            except OSError:
                pass
            self.stop.wait(0.05)

    def close(self):
        self.stop.set()
        self.thread.join(1)
        os.close(self.master)


pty_only = pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs ptys")


@pty_only
def test_another_program_still_cannot_open_a_port_in_use():
    meter = PtyMeter(1)
    first, second = (
        SerialTransport(meter.path, timeout=0.1),
        SerialTransport(meter.path, timeout=0.1),
    )
    try:
        first.open()
        import serial

        with pytest.raises(
            serial.SerialException, match="already in use by another meter or program"
        ):
            second.open()
        first.close()
        second.open()
        assert second.is_open
    finally:
        first.close()
        second.close()
        meter.close()


@pty_only
def test_e2e_three_meters_on_one_reader_all_get_complete_frames(monkeypatch):
    meter = PtyMeter(7000)  # 7 kWh
    monkeypatch.setattr(
        devices, "_comports", lambda: [adapter(meter.path, serial="ONE")]
    )

    def m(meter_id, port, unit=None):
        return MeterConfig(
            id=meter_id,
            port=port,
            mappings=[Mapping("1-0:1.8.0*255", f"t/{meter_id}", unit=unit)],
        )

    cfg = AppConfig(
        meters=[
            m("a", meter.path),
            m("b", "usb:serial=ONE", unit="Wh"),  # same reader, other unit and topic
            m("c", "usb:location=1-9"),  # unrelated, absent reader
        ]
    )
    cfg.meters.append(m("d", meter.path))
    pub = RecordingPublisher()
    manager = MeterManager(cfg, pub)  # real SerialTransport, exclusive lock on
    manager.start()
    try:
        assert wait_until(
            lambda: all(pub.payloads_for(f"t/{i}") for i in ("a", "b", "d")), timeout=10
        )
        for i in ("a", "b", "d"):
            assert manager.get_worker(i).reader.state == "reading", i
            assert manager.get_worker(i).transport.device == meter.path
        hub = manager.get_worker("a").transport.hub
        assert manager.get_worker("b").transport.hub is hub
        assert manager.get_worker("d").transport.hub is hub
        assert manager.get_worker("c").transport.hub is not hub
        # every frame decoded intact by every meter: no torn or lost values
        assert set(pub.payloads_for("t/a")) == {"7"}
        assert set(pub.payloads_for("t/d")) == {"7"}
        assert set(pub.payloads_for("t/b")) == {"7000"}

        # once all are attached each one sees the same frames (the first one to
        # open may also have drained what the pty buffered before the others)
        def counts():
            return [manager.discovered(i)[0].count for i in ("a", "b", "d")]

        before = counts()
        time.sleep(1.0)
        deltas = [after - b for after, b in zip(counts(), before)]
        assert min(deltas) >= 5 and max(deltas) - min(deltas) <= 2, deltas
        status = {s["id"]: s for s in manager.status()}
        assert (
            status["a"]["shared_with"] == ["b", "d"]
            and status["c"]["shared_with"] == []
        )

        # a restart of one meter (config change) keeps the others reading
        new = AppConfig(
            meters=[*cfg.meters[:1], m("b", "usb:serial=ONE"), *cfg.meters[2:]]
        )
        manager.apply_config(new)
        pub.messages.clear()
        assert wait_until(
            lambda: pub.payloads_for("t/b") and pub.payloads_for("t/a"), timeout=10
        )
        assert set(pub.payloads_for("t/b")) == {"7"}  # unit change took effect
        assert (
            manager.get_worker("b").transport.hub
            is manager.get_worker("a").transport.hub
        )
    finally:
        manager.stop()
        meter.close()


@pty_only
def test_e2e_pin_entry_on_shared_reader_while_other_meter_reads(monkeypatch):
    meter = PtyMeter(5000)
    cfg = AppConfig(
        meters=[
            MeterConfig(
                id="a", port=meter.path, mappings=[Mapping("1-0:1.8.0*255", "t/a")]
            ),
            MeterConfig(id="b", port=meter.path),
        ]
    )
    pub = RecordingPublisher()
    manager = MeterManager(cfg, pub)
    manager.start()
    try:
        assert wait_until(lambda: pub.payloads_for("t/a"), timeout=10)
        with manager.exclusive_transport("b") as t:
            assert t.write(b"\x00" * 4) == 4  # pulses reach the shared port
            got = b""
            deadline = time.monotonic() + 3
            while b"\x1b\x1b\x1b\x1b" not in got and time.monotonic() < deadline:
                got += t.read(512)
            assert b"\x1b\x1b\x1b\x1b" in got  # PIN detection still sees the stream
            pub.messages.clear()
            assert wait_until(
                lambda: pub.payloads_for("t/a"), timeout=5
            )  # a kept reading
        assert wait_until(lambda: manager.get_worker("b") is not None, timeout=2)
    finally:
        manager.stop()
        meter.close()
