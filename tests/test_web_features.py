import os

from starlette.testclient import TestClient

from smlgw.config import AppConfig, MeterConfig
from smlgw.history import HistoryStore
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.sml.builder import build_meter_frame
from smlgw.transport import BytesTransport
from smlgw.web import create_app

from conftest import wait_until

FRAME = build_meter_frame(
    b"\x01\x02",
    {
        "1-0:1.8.0*255": dict(value=734512, scaler=-1, unit=30),
        "1-0:16.7.0*255": dict(value=42, scaler=0, unit=27),
    },
)


def make_env(tmp_path, config=None, *, history=None, follow_redirects=True):
    config = config or AppConfig(meters=[MeterConfig(id="m", port="p")])
    manager = MeterManager(
        config,
        RecordingPublisher(),
        transport_factory=lambda c: BytesTransport([FRAME] * 3),
        history=history,
    )
    manager.start()
    app = create_app(manager, os.path.join(tmp_path, "config.yaml"))
    client = TestClient(app, follow_redirects=follow_redirects)
    return client, manager


def test_pages_render(tmp_path):
    client, manager = make_env(tmp_path)
    try:
        assert client.get("/").status_code == 200          # dashboard
        assert client.get("/settings").status_code == 200
        assert client.get("/meter/m").status_code == 200
    finally:
        manager.stop()


def test_history_records_and_api_returns_points(tmp_path):
    store = HistoryStore(":memory:", sample_interval=0)
    client, manager = make_env(tmp_path, history=store)
    try:
        assert wait_until(lambda: store.count() >= 2)
        r = client.get("/api/history?meter=m&obis=1-0:16.7.0*255&since=100000&points=100")
        body = r.json()
        assert body["points"], "expected recorded points"
        assert body["latest"] == 42.0
    finally:
        manager.stop()


def test_sources_lists_meter_obis(tmp_path):
    client, manager = make_env(tmp_path)
    try:
        assert wait_until(lambda: manager.discovered("m"))
        srcs = client.get("/api/sources").json()["sources"]
        keys = {(s["meter"], s["obis"]) for s in srcs}
        assert ("m", "1-0:16.7.0*255") in keys
    finally:
        manager.stop()


def test_unit_options_and_override(tmp_path):
    cfg = AppConfig(meters=[MeterConfig(id="m", port="p")])
    client, manager = make_env(tmp_path, config=cfg)
    try:
        assert wait_until(lambda: manager.discovered("m"))
        body = client.get("/api/meters/m/discovered").json()
        energy = next(v for v in body["values"] if v["code"] == "1-0:1.8.0*255")
        assert energy["unit"] == "kWh"
        assert energy["unit_options"] == ["kWh", "Wh", "MWh"]

        # Map it with an explicit Wh output unit.
        r = client.put(
            "/api/meters/m/mappings",
            json={"mappings": [{"obis": "1-0:1.8.0*255", "topic": "power/m/total", "enabled": True, "unit": "Wh"}]},
        )
        assert r.status_code == 200
        assert manager.config.get_meter("m").mappings[0].unit == "Wh"

        # After the worker restarts with the new mapping, the value is shown in Wh.
        def shown_in_wh():
            b = client.get("/api/meters/m/discovered").json()
            e = next((v for v in b["values"] if v["code"] == "1-0:1.8.0*255"), None)
            return e is not None and e["unit"] == "Wh"

        assert wait_until(shown_in_wh, timeout=3.0)
    finally:
        manager.stop()


def test_dashboard_crud(tmp_path):
    client, manager = make_env(tmp_path)
    try:
        panel = {
            "id": "p1", "title": "Power", "type": "line", "span": 2,
            "series": [{"meter": "m", "obis": "1-0:16.7.0*255"}],
        }
        assert client.put("/api/dashboard", json={"panels": [panel]}).status_code == 200
        got = client.get("/api/dashboard").json()["panels"]
        assert len(got) == 1 and got[0]["id"] == "p1"
        assert manager.config.dashboard.panels[0].title == "Power"
    finally:
        manager.stop()


def test_settings_mqtt_and_history(tmp_path):
    store = HistoryStore(":memory:")
    client, manager = make_env(tmp_path, history=store)
    try:
        assert client.put("/api/settings/mqtt", json={"host": "newhost", "port": 1884}).status_code == 200
        assert manager.config.mqtt.host == "newhost"
        assert manager.config.mqtt.port == 1884

        r = client.put("/api/settings/history", json={"enabled": True, "retention_hours": 48, "sample_interval": 5})
        assert r.status_code == 200
        assert manager.config.history.retention_hours == 48
        assert store.retention_hours == 48

        settings = client.get("/api/settings").json()
        assert settings["mqtt"]["host"] == "newhost"
        assert settings["history"]["retention_hours"] == 48
    finally:
        manager.stop()


def test_password_protection_flow(tmp_path):
    client, manager = make_env(tmp_path, follow_redirects=False)
    try:
        # Initially open.
        assert client.get("/api/status").status_code == 200
        # Enable a password.
        assert client.post("/api/settings/password", json={"password": "letmein"}).status_code == 200
        assert manager.config.auth.enabled is True
        # A fresh (unauthenticated) client is now locked out of the API.
        anon = TestClient(create_app(manager, os.path.join(tmp_path, "config.yaml")), follow_redirects=False)
        assert anon.get("/api/status").status_code == 401
        assert anon.get("/").status_code == 302  # redirected to /login
        # Wrong password rejected.
        assert anon.post("/api/login", data={"password": "nope"}).status_code == 401
        # Correct password authenticates and unlocks the session.
        login = anon.post("/api/login", data={"password": "letmein"})
        assert login.status_code == 302
        assert anon.get("/api/status").status_code == 200
    finally:
        manager.stop()


def test_config_export_and_import(tmp_path):
    import yaml

    client, manager = make_env(tmp_path)
    try:
        # Export the live config as YAML.
        r = client.get("/api/config/export")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        data = yaml.safe_load(r.text)
        assert any(m["id"] == "m" for m in data["meters"])

        # Modify and restore it.
        data["mqtt"]["host"] = "imported-host"
        data["meters"].append({"id": "m2", "port": "p2"})
        res = client.post(
            "/api/config/import",
            content=yaml.safe_dump(data),
            headers={"Content-Type": "application/x-yaml"},
        )
        assert res.status_code == 200
        assert res.json()["meters"] == 2
        assert manager.config.mqtt.host == "imported-host"
        assert manager.config.get_meter("m2") is not None
    finally:
        manager.stop()


def test_config_import_rejects_invalid(tmp_path):
    client, manager = make_env(tmp_path)
    try:
        res = client.post(
            "/api/config/import", content="not: [valid: yaml", headers={"Content-Type": "application/x-yaml"}
        )
        assert res.status_code == 400
    finally:
        manager.stop()


def test_config_import_preserves_session_secret(tmp_path):
    import yaml

    client, manager = make_env(tmp_path)
    try:
        secret_before = manager.config.auth.secret
        data = yaml.safe_load(client.get("/api/config/export").text)
        data["auth"]["secret"] = "some-other-secret"  # should be ignored on import
        client.post("/api/config/import", content=yaml.safe_dump(data), headers={"Content-Type": "application/x-yaml"})
        # The running signing secret is kept so the admin isn't logged out.
        assert manager.config.auth.secret == secret_before
    finally:
        manager.stop()


def test_disable_password_reopens(tmp_path):
    client, manager = make_env(tmp_path, follow_redirects=False)
    try:
        client.post("/api/settings/password", json={"password": "pw"})
        # Authenticate this client.
        client.post("/api/login", data={"password": "pw"})
        assert client.get("/api/status").status_code == 200
        # Disable protection.
        assert client.delete("/api/settings/password").status_code == 200
        assert manager.config.auth.enabled is False
        anon = TestClient(create_app(manager, os.path.join(tmp_path, "config.yaml")), follow_redirects=False)
        assert anon.get("/api/status").status_code == 200
    finally:
        manager.stop()
