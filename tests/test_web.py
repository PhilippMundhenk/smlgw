import os

import pytest
from fastapi.testclient import TestClient

from smlgw.config import AppConfig, MeterConfig, PinConfig
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.sml.builder import build_meter_frame
from smlgw.simulator import UnlockableTransport
from smlgw.transport import BytesTransport
from smlgw.web import create_app

from conftest import wait_until

FAST = PinConfig(pulse="00", digit_gap=0.0, group_gap=0.0, settle=0.0, detect_timeout=0.2)
DATA_FRAME = build_meter_frame(
    b"\x01\x02",
    {
        "1-0:1.8.0*255": dict(value=734512, scaler=-1, unit=30),
        "1-0:16.7.0*255": dict(value=4123, scaler=-1, unit=27),
    },
)


def make_env(tmp_path, config, factory):
    manager = MeterManager(config, RecordingPublisher(), transport_factory=factory)
    manager.start()
    app = create_app(manager, os.path.join(tmp_path, "config.yaml"))
    client = TestClient(app)
    return client, manager


def test_index_and_status(tmp_path):
    cfg = AppConfig(meters=[MeterConfig(id="m", port="p")])
    client, manager = make_env(tmp_path, cfg, lambda c: BytesTransport([DATA_FRAME] * 3))
    try:
        assert client.get("/").status_code == 200
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["meters"][0]["id"] == "m"
    finally:
        manager.stop()


def test_create_update_delete_meter(tmp_path):
    client, manager = make_env(tmp_path, AppConfig(), lambda c: BytesTransport())
    try:
        r = client.post("/api/meters", json={"id": "new", "port": "/dev/ttyUSB0"})
        assert r.status_code == 201
        assert manager.config.get_meter("new") is not None
        # Duplicate id rejected.
        assert client.post("/api/meters", json={"id": "new"}).status_code == 409
        # Update.
        assert client.put("/api/meters/new", json={"name": "Renamed"}).status_code == 200
        assert manager.config.get_meter("new").name == "Renamed"
        # Delete.
        assert client.delete("/api/meters/new").status_code == 200
        assert manager.config.get_meter("new") is None
    finally:
        manager.stop()


def test_discovered_and_mapping_roundtrip(tmp_path):
    cfg = AppConfig(meters=[MeterConfig(id="m", port="p")])
    client, manager = make_env(tmp_path, cfg, lambda c: BytesTransport([DATA_FRAME] * 3))
    try:
        assert wait_until(lambda: manager.discovered("m"))
        r = client.get("/api/meters/m/discovered")
        body = r.json()
        assert body["has_data"] is True
        codes = {v["code"] for v in body["values"]}
        assert "1-0:1.8.0*255" in codes

        r = client.put(
            "/api/meters/m/mappings",
            json={"mappings": [{"obis": "1-0:1.8.0*255", "topic": "power/m/total", "enabled": True}]},
        )
        assert r.status_code == 200
        assert manager.config.get_meter("m").mappings[0].topic == "power/m/total"
        # Persisted to disk.
        assert os.path.exists(os.path.join(tmp_path, "config.yaml"))
    finally:
        manager.stop()


def test_locked_meter_reports_no_data(tmp_path):
    cfg = AppConfig(meters=[MeterConfig(id="lock", port="p")], pin=FAST)
    client, manager = make_env(tmp_path, cfg, lambda c: UnlockableTransport("0003", pin_config=FAST))
    try:
        r = client.get("/api/meters/lock/discovered")
        assert r.json()["has_data"] is False
    finally:
        manager.stop()


def test_web_bruteforce_unlocks_meter(tmp_path):
    cfg = AppConfig(meters=[MeterConfig(id="lock", port="p")], pin=FAST)
    client, manager = make_env(tmp_path, cfg, lambda c: UnlockableTransport("0003", pin_config=FAST))
    try:
        r = client.post("/api/meters/lock/bruteforce", json={"length": 4, "start": 0, "end": 20})
        assert r.status_code == 200

        def done():
            p = client.get("/api/meters/lock/pin").json()["progress"]
            return p and p["finished"]

        assert wait_until(done, timeout=8.0, interval=0.1)
        final = client.get("/api/meters/lock/pin").json()["progress"]
        assert final["found"] == "0003"
    finally:
        manager.stop()


def test_web_send_correct_and_wrong_pin(tmp_path):
    cfg = AppConfig(meters=[MeterConfig(id="lock", port="p")], pin=FAST)
    client, manager = make_env(tmp_path, cfg, lambda c: UnlockableTransport("0007", pin_config=FAST))
    try:
        client.post("/api/meters/lock/pin", json={"pin": "0007"})

        def done():
            p = client.get("/api/meters/lock/pin").json()["progress"]
            return p and p["finished"]

        assert wait_until(done, timeout=6.0, interval=0.1)
        assert client.get("/api/meters/lock/pin").json()["progress"]["found"] == "0007"
    finally:
        manager.stop()


def test_unknown_meter_404(tmp_path):
    client, manager = make_env(tmp_path, AppConfig(), lambda c: BytesTransport())
    try:
        assert client.get("/api/meters/ghost/discovered").status_code == 404
    finally:
        manager.stop()
