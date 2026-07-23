"""Starlette web application: Grafana-style dashboard + settings + JSON API.

The web layer is built directly on **Starlette** (not FastAPI) so the whole
install stays pure-Python — no ``pydantic-core``/``uvloop`` Rust builds. This
matters on small boards (e.g. a Raspberry Pi 1, ARMv6, Python 3.9) where those
wheels are unavailable and compile for a very long time. Request bodies are
parsed and validated by hand via small helpers.

Layout:
* ``/``          -- the configurable dashboard of history panels (default home)
* ``/settings``  -- meters, MQTT, history retention and UI password
* ``/meter/{id}``-- per-meter discovered values, MQTT mapping and PIN tools
* ``/login``     -- shown only when a UI password is set

All configuration changes go through a single locked read-modify-write
(``mutate``) so concurrent edits can't clobber each other, are persisted
atomically, and are applied to the running manager without a restart.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path

import yaml
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..auth import generate_secret, hash_password, verify_password
from ..config import (
    AppConfig,
    DashboardConfig,
    HistoryConfig,
    Mapping,
    MeterConfig,
    MqttConfig,
    Panel,
    Series,
    save_config,
)
from ..manager import MeterManager
from ..obis import obis_name
from .jobs import PinJobManager

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_OPEN_PATHS = {"/login", "/api/login", "/logout", "/health"}


# --------------------------------------------------------------------------- #
# Small request-parsing / validation helpers
# --------------------------------------------------------------------------- #
def _bad(detail: str):
    raise HTTPException(status_code=400, detail=detail)


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        _bad("invalid JSON body")
    if not isinstance(data, dict):
        _bad("expected a JSON object")
    return data


def _req_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        _bad(f"'{key}' is required")
    return value


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    return bool(value) if isinstance(value, bool) else default


def list_serial_ports() -> list:
    try:
        from serial.tools import list_ports

        return [{"device": p.device, "description": p.description} for p in list_ports.comports()]
    except Exception:
        return []


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect/deny unauthenticated requests when a UI password is configured."""

    def __init__(self, app, manager: MeterManager) -> None:
        super().__init__(app)
        self.manager = manager

    async def dispatch(self, request: Request, call_next):
        auth = self.manager.config.auth
        path = request.url.path
        if not auth.enabled or path.startswith("/static") or path in _OPEN_PATHS:
            return await call_next(request)
        if request.session.get("authenticated"):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=302)


def create_app(manager: MeterManager, config_path: str) -> Starlette:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    jobs = PinJobManager(manager)
    config_lock = threading.Lock()

    # Ensure a session-signing secret exists (generate + persist on first run).
    if not manager.config.auth.secret:
        new_auth = replace(manager.config.auth, secret=generate_secret())
        manager.config = replace(manager.config, auth=new_auth)
        try:
            save_config(manager.config, config_path)
        except Exception:
            log.warning("could not persist generated session secret (read-only config?)")

    def mutate(fn):
        """Serialised read-modify-write of the config: apply + persist atomically."""
        with config_lock:
            new_config = fn(manager.config)
            manager.apply_config(new_config)
            save_config(new_config, config_path)
            return new_config

    def require_meter(meter_id: str) -> MeterConfig:
        meter = manager.config.get_meter(meter_id)
        if meter is None:
            raise HTTPException(status_code=404, detail=f"unknown meter {meter_id!r}")
        return meter

    # ---- HTML views --------------------------------------------------- #
    def dashboard(request: Request):
        return templates.TemplateResponse(
            request, "dashboard.html", {"auth_enabled": manager.config.auth.enabled}
        )

    def settings_view(request: Request):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "status": manager.status(),
                "mqtt": manager.config.mqtt,
                "history": manager.config.history,
                "auth": manager.config.auth,
                "ports": list_serial_ports(),
            },
        )

    def meter_view(request: Request):
        meter = require_meter(request.path_params["meter_id"])
        return templates.TemplateResponse(
            request, "meter.html", {"meter": meter, "ports": list_serial_ports()}
        )

    def login_view(request: Request):
        if not manager.config.auth.enabled:
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    async def api_login(request: Request):
        form = await request.form()
        password = form.get("password") or ""
        if verify_password(password, manager.config.auth.password_hash):
            request.session["authenticated"] = True
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password"}, status_code=401
        )

    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    def health(request: Request):
        return JSONResponse({"ok": True})

    # ---- status / discovery API --------------------------------------- #
    def api_status(request: Request):
        return JSONResponse(
            {"meters": manager.status(), "mqtt": {"connected": manager.publisher.connected}}
        )

    def api_ports(request: Request):
        return JSONResponse({"ports": list_serial_ports()})

    def api_sources(request: Request):
        sources = []
        for meter in manager.config.meters:
            for value in manager.discovered(meter.id):
                sources.append(
                    {
                        "meter": meter.id,
                        "meter_name": meter.name,
                        "obis": value.code,
                        "name": value.name,
                        "unit": value.unit,
                        "value": value.value,
                    }
                )
        return JSONResponse({"sources": sources})

    def api_history(request: Request):
        q = request.query_params
        meter = q.get("meter", "")
        obis = q.get("obis", "")
        since = _as_float(q.get("since"), 3600.0)
        points = _as_int(q.get("points"), 500)
        if manager.history is None:
            return JSONResponse({"meter": meter, "obis": obis, "points": [], "latest": None})
        samples = manager.history.query(meter, obis, since_seconds=since, max_points=points)
        return JSONResponse(
            {
                "meter": meter,
                "obis": obis,
                "points": [[s.ts, s.value] for s in samples],
                "latest": samples[-1].value if samples else None,
            }
        )

    # ---- meter management --------------------------------------------- #
    async def api_create_meter(request: Request):
        body = await _json_body(request)
        meter_id = _req_str(body, "id")

        def build(cfg: AppConfig) -> AppConfig:
            if cfg.get_meter(meter_id) is not None:
                raise HTTPException(status_code=409, detail="meter id already exists")
            meter = MeterConfig(
                id=meter_id,
                name=body.get("name") or meter_id,
                port=body.get("port", ""),
                baudrate=_as_int(body.get("baudrate"), 9600),
                enabled=_as_bool(body.get("enabled"), True),
                verify_crc=_as_bool(body.get("verify_crc"), False),
                pin=body.get("pin") or None,
            )
            return cfg.with_meter(meter)

        mutate(build)
        return JSONResponse({"ok": True, "meter": meter_id}, status_code=201)

    async def api_update_meter(request: Request):
        meter_id = request.path_params["meter_id"]
        body = await _json_body(request)

        def build(cfg: AppConfig) -> AppConfig:
            meter = cfg.get_meter(meter_id)
            if meter is None:
                raise HTTPException(status_code=404, detail="unknown meter")
            updated = replace(
                meter,
                name=body["name"] if "name" in body else meter.name,
                port=body["port"] if "port" in body else meter.port,
                baudrate=_as_int(body["baudrate"], meter.baudrate) if "baudrate" in body else meter.baudrate,
                enabled=_as_bool(body["enabled"], meter.enabled) if "enabled" in body else meter.enabled,
                verify_crc=_as_bool(body["verify_crc"], meter.verify_crc) if "verify_crc" in body else meter.verify_crc,
                pin=body["pin"] if "pin" in body else meter.pin,
            )
            return cfg.with_meter(updated)

        mutate(build)
        return JSONResponse({"ok": True})

    def api_delete_meter(request: Request):
        meter_id = request.path_params["meter_id"]
        require_meter(meter_id)
        mutate(lambda cfg: replace(cfg, meters=[m for m in cfg.meters if m.id != meter_id]))
        return JSONResponse({"ok": True})

    def api_discovered(request: Request):
        meter_id = request.path_params["meter_id"]
        meter = require_meter(meter_id)
        mapped = {m.obis: m for m in meter.mappings}
        values = [
            {
                "code": d.code,
                "name": d.name,
                "value": d.value,
                "unit": d.unit,
                "last_seen": d.last_seen,
                "count": d.count,
                "mapped_topic": mapped[d.code].topic if d.code in mapped else None,
                "mapped_enabled": mapped[d.code].enabled if d.code in mapped else False,
            }
            for d in manager.discovered(meter_id)
        ]
        worker = manager.get_worker(meter_id)
        return JSONResponse(
            {
                "meter": meter_id,
                "has_data": bool(values),
                "state": worker.reader.state if worker else "stopped",
                "values": values,
                "mappings": [
                    {"obis": m.obis, "topic": m.topic, "enabled": m.enabled} for m in meter.mappings
                ],
            }
        )

    async def api_set_mappings(request: Request):
        meter_id = request.path_params["meter_id"]
        body = await _json_body(request)
        raw_mappings = body.get("mappings")
        if not isinstance(raw_mappings, list):
            _bad("'mappings' must be a list")
        mappings = []
        for m in raw_mappings:
            if not isinstance(m, dict) or "obis" not in m or "topic" not in m:
                _bad("each mapping needs 'obis' and 'topic'")
            mappings.append(Mapping(obis=str(m["obis"]), topic=str(m["topic"]), enabled=_as_bool(m.get("enabled"), True)))

        def build(cfg: AppConfig) -> AppConfig:
            meter = cfg.get_meter(meter_id)
            if meter is None:
                raise HTTPException(status_code=404, detail="unknown meter")
            return cfg.with_meter(replace(meter, mappings=mappings))

        mutate(build)
        return JSONResponse({"ok": True, "count": len(mappings)})

    # ---- settings ----------------------------------------------------- #
    def api_get_settings(request: Request):
        c = manager.config
        return JSONResponse(
            {
                "mqtt": {
                    "host": c.mqtt.host, "port": c.mqtt.port, "username": c.mqtt.username,
                    "client_id": c.mqtt.client_id, "tls": c.mqtt.tls, "retain": c.mqtt.retain,
                },
                "history": {
                    "enabled": c.history.enabled,
                    "retention_hours": c.history.retention_hours,
                    "sample_interval": c.history.sample_interval,
                    "samples": manager.history.count() if manager.history else 0,
                },
                "auth": {"enabled": c.auth.enabled},
            }
        )

    async def api_set_mqtt(request: Request):
        body = await _json_body(request)
        host = _req_str(body, "host")

        def build(cfg: AppConfig) -> AppConfig:
            return replace(cfg, mqtt=MqttConfig(
                host=host, port=_as_int(body.get("port"), 1883),
                username=body.get("username") or None, password=body.get("password") or None,
                client_id=body.get("client_id", "smlgw"),
                tls=_as_bool(body.get("tls"), False), retain=_as_bool(body.get("retain"), False),
            ))

        mutate(build)
        manager.reconfigure_mqtt()
        return JSONResponse({"ok": True})

    async def api_set_history(request: Request):
        body = await _json_body(request)
        retention = _as_float(body.get("retention_hours"), 168.0)
        interval = _as_float(body.get("sample_interval"), 10.0)
        enabled = _as_bool(body.get("enabled"), True)

        def build(cfg: AppConfig) -> AppConfig:
            return replace(cfg, history=HistoryConfig(
                enabled=enabled, retention_hours=retention,
                sample_interval=interval, db_path=cfg.history.db_path,
            ))

        mutate(build)
        if manager.history is not None:
            manager.history.update_settings(retention_hours=retention, sample_interval=interval)
        return JSONResponse({"ok": True})

    async def api_set_password(request: Request):
        body = await _json_body(request)
        password = _req_str(body, "password")

        def build(cfg: AppConfig) -> AppConfig:
            new_auth = replace(
                cfg.auth, enabled=True, password_hash=hash_password(password),
                secret=cfg.auth.secret or generate_secret(),
            )
            return replace(cfg, auth=new_auth)

        mutate(build)
        return JSONResponse({"ok": True, "enabled": True})

    def api_clear_password(request: Request):
        mutate(lambda cfg: replace(cfg, auth=replace(cfg.auth, enabled=False, password_hash=None)))
        request.session.clear()
        return JSONResponse({"ok": True, "enabled": False})

    # ---- backup / restore -------------------------------------------- #
    def api_export_config(request: Request):
        payload = yaml.safe_dump(manager.config.to_dict(), sort_keys=False, allow_unicode=True)
        return Response(
            content=payload, media_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="smlgw-config.yaml"'},
        )

    async def api_import_config(request: Request):
        raw = await request.body()
        try:
            data = yaml.safe_load(raw.decode("utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError("configuration must be a mapping")
            new_config = AppConfig.from_dict(data)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            raise HTTPException(status_code=400, detail=f"invalid configuration: {exc}")
        current_secret = manager.config.auth.secret
        new_config = replace(
            new_config,
            auth=replace(new_config.auth, secret=current_secret or new_config.auth.secret or generate_secret()),
        )
        mutate(lambda _cfg: new_config)
        manager.reconfigure_mqtt()
        if manager.history is not None:
            manager.history.update_settings(
                retention_hours=new_config.history.retention_hours,
                sample_interval=new_config.history.sample_interval,
            )
        return JSONResponse({"ok": True, "meters": len(new_config.meters), "panels": len(new_config.dashboard.panels)})

    # ---- dashboard ---------------------------------------------------- #
    def api_get_dashboard(request: Request):
        from dataclasses import asdict

        return JSONResponse({"panels": [asdict(p) for p in manager.config.dashboard.panels]})

    async def api_set_dashboard(request: Request):
        body = await _json_body(request)
        raw_panels = body.get("panels")
        if not isinstance(raw_panels, list):
            _bad("'panels' must be a list")
        try:
            panels = [Panel.from_dict(p) for p in raw_panels]
        except (KeyError, TypeError, ValueError) as exc:
            _bad(f"invalid panel: {exc}")
        mutate(lambda cfg: replace(cfg, dashboard=DashboardConfig(panels=panels)))
        return JSONResponse({"ok": True, "count": len(panels)})

    def api_obis_name(request: Request):
        code = request.path_params["code"]
        return JSONResponse({"code": code, "name": obis_name(code)})

    # ---- PIN operations ---------------------------------------------- #
    async def api_send_pin(request: Request):
        meter_id = request.path_params["meter_id"]
        require_meter(meter_id)
        body = await _json_body(request)
        pin = _req_str(body, "pin")
        if not pin.isdigit():
            _bad("pin must be digits only")
        try:
            job = jobs.send(meter_id, pin)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return JSONResponse({"ok": True, "job": job.kind, "progress": job.progress.to_dict()})

    async def api_bruteforce(request: Request):
        meter_id = request.path_params["meter_id"]
        require_meter(meter_id)
        body = await _json_body(request)
        length = _as_int(body.get("length"), 4)
        start = _as_int(body.get("start"), 0)
        end = _as_int(body.get("end"), None) if body.get("end") is not None else None
        try:
            job = jobs.bruteforce(meter_id, length, start, end)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return JSONResponse({"ok": True, "job": job.kind, "progress": job.progress.to_dict()})

    def api_pin_status(request: Request):
        meter_id = request.path_params["meter_id"]
        require_meter(meter_id)
        job = jobs.get(meter_id)
        if job is None:
            return JSONResponse({"active": False, "progress": None})
        return JSONResponse({"active": job.running, "kind": job.kind, "progress": job.progress.to_dict()})

    def api_cancel_pin(request: Request):
        meter_id = request.path_params["meter_id"]
        require_meter(meter_id)
        return JSONResponse({"ok": jobs.cancel(meter_id)})

    async def on_http_exception(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    routes = [
        Route("/", dashboard),
        Route("/settings", settings_view),
        Route("/meter/{meter_id}", meter_view),
        Route("/login", login_view),
        Route("/api/login", api_login, methods=["POST"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route("/health", health),
        Route("/api/status", api_status),
        Route("/api/ports", api_ports),
        Route("/api/sources", api_sources),
        Route("/api/history", api_history),
        Route("/api/meters", api_create_meter, methods=["POST"]),
        Route("/api/meters/{meter_id}", api_update_meter, methods=["PUT"]),
        Route("/api/meters/{meter_id}", api_delete_meter, methods=["DELETE"]),
        Route("/api/meters/{meter_id}/discovered", api_discovered),
        Route("/api/meters/{meter_id}/mappings", api_set_mappings, methods=["PUT"]),
        Route("/api/settings", api_get_settings),
        Route("/api/settings/mqtt", api_set_mqtt, methods=["PUT"]),
        Route("/api/settings/history", api_set_history, methods=["PUT"]),
        Route("/api/settings/password", api_set_password, methods=["POST"]),
        Route("/api/settings/password", api_clear_password, methods=["DELETE"]),
        Route("/api/config/export", api_export_config),
        Route("/api/config/import", api_import_config, methods=["POST"]),
        Route("/api/dashboard", api_get_dashboard),
        Route("/api/dashboard", api_set_dashboard, methods=["PUT"]),
        Route("/api/obis/{code:path}", api_obis_name),
        Route("/api/meters/{meter_id}/pin", api_send_pin, methods=["POST"]),
        Route("/api/meters/{meter_id}/bruteforce", api_bruteforce, methods=["POST"]),
        Route("/api/meters/{meter_id}/pin", api_pin_status, methods=["GET"]),
        Route("/api/meters/{meter_id}/pin", api_cancel_pin, methods=["DELETE"]),
    ]
    if _STATIC_DIR.exists():
        routes.append(Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"))

    # SessionMiddleware must wrap AuthMiddleware so the session is available.
    middleware = [
        Middleware(SessionMiddleware, secret_key=manager.config.auth.secret, same_site="lax"),
        Middleware(AuthMiddleware, manager=manager),
    ]

    app = Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={HTTPException: on_http_exception},
    )
    app.state.manager = manager
    app.state.jobs = jobs
    app.state.config_path = config_path
    return app
