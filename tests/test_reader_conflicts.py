"""Two meters must not use the same reader (issue #3)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import yaml
from conftest import wait_until
from starlette.testclient import TestClient

from smlgw import __main__ as cli
from smlgw import devices
from smlgw.config import AppConfig, MeterConfig, load_config, save_config
from smlgw.devices import find_conflicts, reader_key
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.transport import BytesTransport, SerialTransport
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


def test_find_conflicts_groups_enabled_meters_only():
    meters = [
        M("heat", "usb:serial=AAA"),
        M("dup", "/dev/ttyUSB0"),
        M("house", "usb:serial=BBB"),
        M("spare", "/dev/ttyUSB0", enabled=False),
        M("empty1", ""),
        M("empty2", ""),
    ]
    assert find_conflicts(meters, [A, B]) == {"heat": ["dup"], "dup": ["heat"]}
    # three-way
    meters.append(M("third", "usb:location=1-1.2"))
    got = find_conflicts(meters, [A, B])
    assert got["heat"] == ["dup", "third"] and got["third"] == ["heat", "dup"]
    assert "spare" not in got and "house" not in got and "empty1" not in got


def test_find_conflicts_enumerates_only_when_a_selector_is_used(monkeypatch):
    def boom():
        raise AssertionError("must not enumerate for plain paths")

    monkeypatch.setattr(devices, "_comports", boom)
    assert find_conflicts([M("a", "/dev/ttyUSB0"), M("b", "/dev/ttyUSB0")]) == {
        "a": ["b"],
        "b": ["a"],
    }
    # enumeration failing is not fatal: selectors compare by criteria
    monkeypatch.setattr(
        devices, "_comports", lambda: (_ for _ in ()).throw(OSError("no sysfs"))
    )
    assert find_conflicts([M("a", "usb:serial=X"), M("b", "usb:serial=X")]) == {
        "a": ["b"],
        "b": ["a"],
    }


# --------------------------------------------------------------------------- #
# web API: working and blocking paths
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


def test_create_rejects_same_reader_by_another_name(env):
    client, manager, path = env
    for port in ("/dev/ttyUSB0", "usb:location=1-1.2", "usb:serial=AAA"):
        r = client.post("/api/meters", json={"id": "dup", "port": port})
        assert r.status_code == 409, port
        assert "same reader as meter 'heat'" in r.json()["detail"]
    assert manager.config.get_meter("dup") is None
    # a different reader, a disabled spare and an empty port are all fine
    assert (
        client.post(
            "/api/meters", json={"id": "house", "port": "usb:serial=BBB"}
        ).status_code
        == 201
    )
    r = client.post(
        "/api/meters", json={"id": "spare", "port": "/dev/ttyUSB0", "enabled": False}
    )
    assert r.status_code == 201
    assert client.post("/api/meters", json={"id": "blank"}).status_code == 201
    assert [m.id for m in load_config(path).meters] == [
        "heat",
        "house",
        "spare",
        "blank",
    ]


def test_update_rejects_port_or_enable_that_creates_conflict(env):
    client, _manager, path = env
    client.post("/api/meters", json={"id": "house", "port": "usb:serial=BBB"})
    client.post(
        "/api/meters", json={"id": "spare", "port": "/dev/ttyUSB0", "enabled": False}
    )

    r = client.put("/api/meters/house", json={"port": "/dev/ttyUSB0"})
    assert r.status_code == 409 and "'heat'" in r.json()["detail"]
    assert load_config(path).get_meter("house").port == "usb:serial=BBB"

    r = client.put("/api/meters/spare", json={"enabled": True})
    assert r.status_code == 409
    assert load_config(path).get_meter("spare").enabled is False

    # disabling heat frees the reader for spare
    assert client.put("/api/meters/heat", json={"enabled": False}).status_code == 200
    assert client.put("/api/meters/spare", json={"enabled": True}).status_code == 200
    # ...and now heat cannot come back
    assert client.put("/api/meters/heat", json={"enabled": True}).status_code == 409


def test_conflicting_hand_edited_config_stays_editable_and_is_flagged(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(devices, "_comports", lambda: [A, B])
    cfg = AppConfig(meters=[M("heat", "/dev/ttyUSB0"), M("copy", "usb:serial=AAA")])
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport()
    )
    manager.start()
    try:
        client = TestClient(create_app(manager, os.path.join(tmp_path, "c.yaml")))
        status = {m["id"]: m for m in client.get("/api/status").json()["meters"]}
        assert status["heat"]["conflicts"] == ["copy"]
        assert status["copy"]["conflicts"] == ["heat"]
        assert "⚠ same reader as copy" in client.get("/settings").text
        page = client.get("/meter/copy").text
        assert 'id="m-conflict"' in page and 'href="/meter/heat"' in page
        assert (
            'id="m-conflict"' not in client.get("/meter/nonexistent").text
        )  # 404 page
        # renaming does not touch port/enabled: allowed despite the conflict
        assert client.put("/api/meters/copy", json={"name": "Copy"}).status_code == 200
        # fixing it is allowed and clears the flag
        assert (
            client.put("/api/meters/copy", json={"port": "usb:serial=BBB"}).status_code
            == 200
        )
        status = {m["id"]: m for m in client.get("/api/status").json()["meters"]}
        assert status["heat"]["conflicts"] == [] and status["copy"]["conflicts"] == []
        assert 'id="m-conflict"' not in client.get("/meter/copy").text
    finally:
        manager.stop()


def test_import_rejects_shared_reader(env):
    client, manager, path = env
    bad = {
        "meters": [
            {"id": "a", "port": "/dev/ttyUSB1"},
            {"id": "b", "port": "usb:serial=BBB"},
        ]
    }
    r = client.post(
        "/api/config/import",
        content=yaml.safe_dump(bad),
        headers={"Content-Type": "application/x-yaml"},
    )
    assert r.status_code == 400
    assert "meters 'a' and 'b' use the same reader" in r.json()["detail"]
    assert [m.id for m in manager.config.meters] == ["heat"]

    ok = {
        "meters": [
            {"id": "a", "port": "/dev/ttyUSB1"},
            {"id": "b", "port": "usb:serial=BBB", "enabled": False},
        ]
    }
    r = client.post(
        "/api/config/import",
        content=yaml.safe_dump(ok),
        headers={"Content-Type": "application/x-yaml"},
    )
    assert r.status_code == 200
    assert [m.id for m in load_config(path).meters] == ["a", "b"]


def test_status_survives_detection_failure(env, monkeypatch):
    client, _manager, _ = env

    def broken(meters, ports=None):
        raise RuntimeError("boom")

    import smlgw.manager as manager_mod

    monkeypatch.setattr(manager_mod, "find_conflicts", broken)
    r = client.get("/api/status")
    assert r.status_code == 200 and r.json()["meters"][0]["conflicts"] == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_ports_reports_conflicts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(devices, "_comports", lambda: [A, B])
    path = os.path.join(tmp_path, "config.yaml")
    save_config(
        AppConfig(meters=[M("heat", "usb:serial=AAA"), M("dup", "/dev/ttyUSB0")]), path
    )
    assert cli.main(["ports", "--config", path]) == 1
    out = capsys.readouterr().out
    assert "ERROR: meter dup uses the same reader as heat" in out
    assert "ERROR: meter heat uses the same reader as dup" in out

    save_config(
        AppConfig(
            meters=[M("heat", "usb:serial=AAA"), M("dup", "/dev/ttyUSB0", False)]
        ),
        path,
    )
    assert cli.main(["ports", "--config", path]) == 0
    assert "same reader" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# runtime: the port is locked, end to end on a real pty with pyserial
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs ptys")
def test_second_open_of_same_port_is_refused_with_clear_error():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    first, second = (
        SerialTransport(path, timeout=0.1),
        SerialTransport(path, timeout=0.1),
    )
    try:
        first.open()
        import serial

        with pytest.raises(
            serial.SerialException, match="already in use by another meter or program"
        ):
            second.open()
        assert not second.is_open
        first.close()
        second.open()  # free again once the first one let go
        assert second.is_open
    finally:
        first.close()
        second.close()
        os.close(master)


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs ptys")
def test_e2e_conflicting_meters_one_reads_other_reports_error(monkeypatch):
    """A conflicting config (hand-edited, bypassing the API) must not tear frames."""
    import threading

    from smlgw.config import Mapping
    from smlgw.sml.builder import build_meter_frame

    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    frame = build_meter_frame(
        b"\x01", {"1-0:1.8.0*255": {"value": 7000, "scaler": 0, "unit": 30}}
    )
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            try:
                os.write(master, frame)
            except OSError:
                pass
            stop.wait(0.05)

    threading.Thread(target=pump, daemon=True).start()
    monkeypatch.setattr(devices, "_comports", lambda: [adapter(path, serial="ONE")])
    cfg = AppConfig(
        meters=[
            MeterConfig(id="a", port=path, mappings=[Mapping("1-0:1.8.0*255", "t/a")]),
            MeterConfig(
                id="b",
                port="usb:serial=ONE",
                mappings=[Mapping("1-0:1.8.0*255", "t/b")],
            ),
        ]
    )
    pub = RecordingPublisher()
    manager = MeterManager(cfg, pub)
    manager.start()
    try:
        states = lambda: sorted(manager.get_worker(i).reader.state for i in ("a", "b"))
        assert wait_until(lambda: states() == ["error", "reading"], timeout=10)
        loser = next(
            i for i in ("a", "b") if manager.get_worker(i).reader.state == "error"
        )
        winner = "b" if loser == "a" else "a"
        assert "already in use" in manager.get_worker(loser).reader.last_error
        assert wait_until(lambda: pub.payloads_for(f"t/{winner}"), timeout=5)
        assert pub.payloads_for(f"t/{loser}") == []
        assert set(pub.payloads_for(f"t/{winner}")) == {"7"}
        status = {s["id"]: s for s in manager.status()}
        assert status[loser]["conflicts"] == [winner]
    finally:
        manager.stop()
        stop.set()
        os.close(master)
