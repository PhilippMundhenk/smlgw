"""Web UI / API / CLI paths for stable device selection (issue #1)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import yaml
from starlette.testclient import TestClient

from smlgw import __main__ as cli
from smlgw import devices
from smlgw.config import AppConfig, MeterConfig, load_config, save_config
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.transport import BytesTransport
from smlgw.web import create_app

READERS = [
    SimpleNamespace(
        device="/dev/ttyUSB0",
        description="FT232R USB UART",
        manufacturer="FTDI",
        product="FT232R USB UART",
        serial_number="A50285BI",
        vid=0x0403,
        pid=0x6001,
        location="1-1.2:1.0",
    ),
    SimpleNamespace(
        device="/dev/ttyUSB1",
        description="USB Serial",
        manufacturer=None,
        product=None,
        serial_number=None,
        vid=0x1A86,
        pid=0x7523,
        location="1-1.3:1.0",
    ),
]


def make_env(tmp_path, monkeypatch, meters=None, ports=READERS):
    monkeypatch.setattr(devices, "_comports", lambda: list(ports))
    cfg = AppConfig(meters=meters or [MeterConfig(id="m", port="/dev/ttyUSB0")])
    path = os.path.join(tmp_path, "config.yaml")
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport()
    )
    manager.start()
    return TestClient(create_app(manager, path)), manager, path


def test_api_ports_lists_stable_addresses(tmp_path, monkeypatch):
    client, manager, _ = make_env(tmp_path, monkeypatch)
    try:
        ports = client.get("/api/ports").json()["ports"]
        assert [p["device"] for p in ports] == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
        assert ports[0]["selector"] == "usb:serial=A50285BI"
        assert ports[0]["serial_number"] == "A50285BI"
        assert (ports[0]["vid"], ports[0]["pid"]) == ("0403", "6001")
        assert ports[1]["selector"] == "usb:location=1-1.3"  # CH340: no serial number
        assert set(ports[0]) >= {"description", "location", "by_id", "by_path"}
    finally:
        manager.stop()


def test_api_ports_empty_without_enumeration(tmp_path, monkeypatch):
    client, manager, _ = make_env(tmp_path, monkeypatch, ports=[])
    try:
        assert client.get("/api/ports").json() == {"ports": []}
        assert "No local serial adapters found" in client.get("/settings").text
    finally:
        manager.stop()


def test_create_and_update_meter_with_selector_persists(tmp_path, monkeypatch):
    other = [MeterConfig(id="m", port="/dev/ttyUSB7")]  # not one of the two readers
    client, manager, path = make_env(tmp_path, monkeypatch, meters=other)
    try:
        r = client.post(
            "/api/meters", json={"id": "heat", "port": "  usb:serial=A50285BI "}
        )
        assert r.status_code == 201
        assert load_config(path).get_meter("heat").port == "usb:serial=A50285BI"
        r = client.put(
            "/api/meters/heat", json={"port": "usb:vid=1a86,pid=7523,location=1-1.3"}
        )
        assert r.status_code == 200
        assert (
            load_config(path).get_meter("heat").port
            == "usb:vid=1a86,pid=7523,location=1-1.3"
        )
        # by-id paths and network URLs are still accepted verbatim
        for ok in (
            "/dev/serial/by-id/usb-FTDI_FT232R_A50285BI-if00-port0",
            "socket://box:5000",
        ):
            assert client.put("/api/meters/heat", json={"port": ok}).status_code == 200
            assert load_config(path).get_meter("heat").port == ok
    finally:
        manager.stop()


def test_malformed_selector_rejected_and_not_saved(tmp_path, monkeypatch):
    client, manager, path = make_env(tmp_path, monkeypatch)
    try:
        r = client.post("/api/meters", json={"id": "bad", "port": "usb:colour=red"})
        assert r.status_code == 400
        assert "invalid port" in r.json()["detail"] and "unknown" in r.json()["detail"]
        assert manager.config.get_meter("bad") is None
        assert not os.path.exists(path) or load_config(path).get_meter("bad") is None

        assert (
            client.post(
                "/api/meters", json={"id": "ok", "port": "usb:serial=X"}
            ).status_code
            == 201
        )
        r = client.put("/api/meters/ok", json={"port": "usb:"})
        assert r.status_code == 400 and "empty" in r.json()["detail"]
        assert load_config(path).get_meter("ok").port == "usb:serial=X"
        # non-string port is treated as empty, not a crash
        assert client.put("/api/meters/ok", json={"port": 5}).status_code == 200
    finally:
        manager.stop()


def test_config_import_rejects_malformed_selector(tmp_path, monkeypatch):
    client, manager, path = make_env(tmp_path, monkeypatch)
    try:
        data = {"meters": [{"id": "x", "port": "usb:serial=a,serial=b"}]}
        r = client.post(
            "/api/config/import",
            content=yaml.safe_dump(data),
            headers={"Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 400
        assert "meter 'x'" in r.json()["detail"] and "duplicate" in r.json()["detail"]
        assert manager.config.get_meter("x") is None

        data = {"meters": [{"id": "x", "port": "usb:serial=A50285BI"}]}
        r = client.post(
            "/api/config/import",
            content=yaml.safe_dump(data),
            headers={"Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 200
        assert load_config(path).get_meter("x").port == "usb:serial=A50285BI"
    finally:
        manager.stop()


def test_status_flags_unstable_ports(tmp_path, monkeypatch):
    meters = [
        MeterConfig(id="old", port="/dev/ttyUSB0"),
        MeterConfig(id="pinned", port="usb:serial=A50285BI"),
        MeterConfig(id="off", port="/dev/ttyACM0", enabled=False),
    ]
    client, manager, _ = make_env(tmp_path, monkeypatch, meters=meters)
    try:
        status = {m["id"]: m for m in client.get("/api/status").json()["meters"]}
        assert status["old"]["unstable_port"] is True
        assert status["pinned"]["unstable_port"] is False
        assert (
            status["off"]["unstable_port"] is True and status["off"]["device"] is None
        )
        assert "device" in status["pinned"]
        page = client.get("/settings").text
        assert page.count("⚠ unstable") == 2
    finally:
        manager.stop()


def test_pages_render_port_picker_with_stable_choices(tmp_path, monkeypatch):
    client, manager, _ = make_env(tmp_path, monkeypatch)
    try:
        for url, input_id in (("/settings", "f-port"), ("/meter/m", "m-port")):
            page = client.get(url).text
            assert f'id="{input_id}-options"' in page
            assert 'value="usb:serial=A50285BI"' in page
            assert 'data-port-value="usb:location=1-1.3"' in page
            assert f'id="{input_id}-warn"' in page
            assert "Detected serial adapters (2)" in page
        assert 'id="m-device"' in client.get("/meter/m").text
    finally:
        manager.stop()


def test_discovered_reports_device_and_error(tmp_path, monkeypatch):
    class Failing(BytesTransport):
        device = None

        def open(self):
            raise devices.DeviceSelectorError("no serial device matches 'usb:serial=Z'")

    monkeypatch.setattr(devices, "_comports", list)
    cfg = AppConfig(meters=[MeterConfig(id="m", port="usb:serial=Z")])
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: Failing()
    )
    manager.start()
    try:
        from conftest import wait_until

        client = TestClient(create_app(manager, os.path.join(tmp_path, "c.yaml")))
        assert wait_until(
            lambda: client.get("/api/meters/m/discovered").json()["state"] == "error"
        )
        body = client.get("/api/meters/m/discovered").json()
        assert "usb:serial=Z" in body["last_error"]
        assert body["device"] is None
    finally:
        manager.stop()


def test_cli_ports_lists_adapters_and_resolves_meters(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(devices, "_comports", lambda: list(READERS))
    path = os.path.join(tmp_path, "config.yaml")
    save_config(
        AppConfig(
            meters=[
                MeterConfig(id="heat", port="usb:serial=A50285BI"),
                MeterConfig(id="house", port="/dev/ttyUSB1"),
            ]
        ),
        path,
    )
    assert cli.main(["ports", "--config", path]) == 0
    out = capsys.readouterr().out
    assert "/dev/ttyUSB0  FT232R USB UART  [0403:6001]" in out
    assert "use as port:   usb:serial=A50285BI" in out
    assert "use as port:   usb:location=1-1.3" in out
    assert "heat: usb:serial=A50285BI -> /dev/ttyUSB0" in out
    assert "house: /dev/ttyUSB1" in out

    # a reader that is gone makes the command fail (usable in scripts)
    save_config(
        AppConfig(meters=[MeterConfig(id="gone", port="usb:serial=NOPE")]), path
    )
    assert cli.main(["ports", "--config", path]) == 1
    assert (
        "gone: usb:serial=NOPE ERROR: no serial device matches"
        in capsys.readouterr().out
    )


def test_cli_ports_without_adapters(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(devices, "_comports", list)
    assert cli.main(["ports", "--config", os.path.join(tmp_path, "none.yaml")]) == 0
    assert "no serial adapters found" in capsys.readouterr().out
