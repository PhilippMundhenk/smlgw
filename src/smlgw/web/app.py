"""FastAPI application: Grafana-style dashboard (home) + settings + JSON API.

Layout:
* ``/``          -- the configurable dashboard of history panels (default home)
* ``/settings``  -- meters, MQTT, history retention and UI password
* ``/meter/{id}``-- per-meter discovered values, MQTT mapping and PIN tools
* ``/login``     -- shown only when a UI password is set

All configuration changes go through a single locked read-modify-write
(:func:`mutate`) so concurrent edits can't clobber each other, are persisted
atomically, and are applied to the running manager without a restart.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from ..auth import generate_secret, hash_password, verify_password
from ..config import (
    AppConfig,
    DashboardConfig,
    Mapping,
    MeterConfig,
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
# Request bodies
# --------------------------------------------------------------------------- #
class MeterBody(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = ""
    port: str = ""
    baudrate: int = 9600
    enabled: bool = True
    verify_crc: bool = False
    pin: str | None = None


class MeterUpdateBody(BaseModel):
    name: str | None = None
    port: str | None = None
    baudrate: int | None = None
    enabled: bool | None = None
    verify_crc: bool | None = None
    pin: str | None = None


class MappingBody(BaseModel):
    obis: str
    topic: str
    enabled: bool = True


class MappingsBody(BaseModel):
    mappings: list[MappingBody]


class PinBody(BaseModel):
    pin: str = Field(..., pattern=r"^\d+$")


class BruteforceBody(BaseModel):
    length: int = 4
    start: int = 0
    end: int | None = None


class MqttBody(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "smlgw"
    tls: bool = False
    retain: bool = False


class HistoryBody(BaseModel):
    enabled: bool = True
    retention_hours: float = 168.0
    sample_interval: float = 10.0


class PasswordBody(BaseModel):
    password: str = Field(..., min_length=1)


class SeriesBody(BaseModel):
    meter: str
    obis: str
    label: str = ""
    color: str = ""


class PanelBody(BaseModel):
    id: str
    title: str = ""
    type: str = "line"
    span: int = 1
    unit: str = ""
    time_range: float = 3600.0
    gauge_min: float = 0.0
    gauge_max: float = 100.0
    series: list[SeriesBody] = Field(default_factory=list)


class DashboardBody(BaseModel):
    panels: list[PanelBody]


def list_serial_ports() -> list[dict]:
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


def create_app(manager: MeterManager, config_path: str) -> FastAPI:
    app = FastAPI(title="smlgw", description="Smart meter to MQTT gateway")
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

    app.state.manager = manager
    app.state.jobs = jobs
    app.state.config_path = config_path

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Middleware: AuthMiddleware inner, SessionMiddleware outer (added last).
    app.add_middleware(AuthMiddleware, manager=manager)
    app.add_middleware(SessionMiddleware, secret_key=manager.config.auth.secret, same_site="lax")

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
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(
            request, "dashboard.html", {"auth_enabled": manager.config.auth.enabled}
        )

    @app.get("/settings", response_class=HTMLResponse)
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

    @app.get("/meter/{meter_id}", response_class=HTMLResponse)
    def meter_view(request: Request, meter_id: str):
        meter = require_meter(meter_id)
        return templates.TemplateResponse(
            request, "meter.html", {"meter": meter, "ports": list_serial_ports()}
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_view(request: Request):
        if not manager.config.auth.enabled:
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/api/login")
    async def api_login(request: Request, password: str = Form(...)):
        if verify_password(password, manager.config.auth.password_hash):
            request.session["authenticated"] = True
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password"}, status_code=401
        )

    @app.get("/logout")
    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    @app.get("/health")
    def health():
        return {"ok": True}

    # ---- status / discovery API --------------------------------------- #
    @app.get("/api/status")
    def api_status():
        return {"meters": manager.status(), "mqtt": {"connected": manager.publisher.connected}}

    @app.get("/api/ports")
    def api_ports():
        return {"ports": list_serial_ports()}

    @app.get("/api/sources")
    def api_sources():
        """Every (meter, obis) currently available, for the panel editor."""
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
        return {"sources": sources}

    @app.get("/api/history")
    def api_history(meter: str, obis: str, since: float = 3600.0, points: int = 500):
        if manager.history is None:
            return {"meter": meter, "obis": obis, "points": []}
        samples = manager.history.query(meter, obis, since_seconds=since, max_points=points)
        return {
            "meter": meter,
            "obis": obis,
            "points": [[s.ts, s.value] for s in samples],
            "latest": samples[-1].value if samples else None,
        }

    # ---- meter management --------------------------------------------- #
    @app.post("/api/meters", status_code=201)
    def api_create_meter(body: MeterBody):
        def build(cfg: AppConfig) -> AppConfig:
            if cfg.get_meter(body.id) is not None:
                raise HTTPException(status_code=409, detail="meter id already exists")
            meter = MeterConfig(
                id=body.id,
                name=body.name or body.id,
                port=body.port,
                baudrate=body.baudrate,
                enabled=body.enabled,
                verify_crc=body.verify_crc,
                pin=body.pin or None,
            )
            return cfg.with_meter(meter)

        mutate(build)
        return {"ok": True, "meter": body.id}

    @app.put("/api/meters/{meter_id}")
    def api_update_meter(meter_id: str, body: MeterUpdateBody):
        def build(cfg: AppConfig) -> AppConfig:
            meter = cfg.get_meter(meter_id)
            if meter is None:
                raise HTTPException(status_code=404, detail="unknown meter")
            updated = replace(
                meter,
                name=body.name if body.name is not None else meter.name,
                port=body.port if body.port is not None else meter.port,
                baudrate=body.baudrate if body.baudrate is not None else meter.baudrate,
                enabled=body.enabled if body.enabled is not None else meter.enabled,
                verify_crc=body.verify_crc if body.verify_crc is not None else meter.verify_crc,
                pin=body.pin if body.pin is not None else meter.pin,
            )
            return cfg.with_meter(updated)

        mutate(build)
        return {"ok": True}

    @app.delete("/api/meters/{meter_id}")
    def api_delete_meter(meter_id: str):
        require_meter(meter_id)
        mutate(lambda cfg: replace(cfg, meters=[m for m in cfg.meters if m.id != meter_id]))
        return {"ok": True}

    @app.get("/api/meters/{meter_id}/discovered")
    def api_discovered(meter_id: str):
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
        return {
            "meter": meter_id,
            "has_data": bool(values),
            "state": worker.reader.state if worker else "stopped",
            "values": values,
            "mappings": [
                {"obis": m.obis, "topic": m.topic, "enabled": m.enabled} for m in meter.mappings
            ],
        }

    @app.put("/api/meters/{meter_id}/mappings")
    def api_set_mappings(meter_id: str, body: MappingsBody):
        def build(cfg: AppConfig) -> AppConfig:
            meter = cfg.get_meter(meter_id)
            if meter is None:
                raise HTTPException(status_code=404, detail="unknown meter")
            mappings = [Mapping(obis=m.obis, topic=m.topic, enabled=m.enabled) for m in body.mappings]
            return cfg.with_meter(replace(meter, mappings=mappings))

        mutate(build)
        return {"ok": True, "count": len(body.mappings)}

    # ---- settings ----------------------------------------------------- #
    @app.get("/api/settings")
    def api_get_settings():
        c = manager.config
        return {
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

    @app.put("/api/settings/mqtt")
    def api_set_mqtt(body: MqttBody):
        from ..config import MqttConfig

        def build(cfg: AppConfig) -> AppConfig:
            return replace(cfg, mqtt=MqttConfig(
                host=body.host, port=body.port, username=body.username or None,
                password=body.password or None, client_id=body.client_id,
                tls=body.tls, retain=body.retain,
            ))

        mutate(build)
        manager.reconfigure_mqtt()
        return {"ok": True}

    @app.put("/api/settings/history")
    def api_set_history(body: HistoryBody):
        from ..config import HistoryConfig

        def build(cfg: AppConfig) -> AppConfig:
            return replace(cfg, history=HistoryConfig(
                enabled=body.enabled,
                retention_hours=body.retention_hours,
                sample_interval=body.sample_interval,
                db_path=cfg.history.db_path,
            ))

        mutate(build)
        if manager.history is not None:
            manager.history.update_settings(
                retention_hours=body.retention_hours, sample_interval=body.sample_interval
            )
        return {"ok": True}

    @app.post("/api/settings/password")
    def api_set_password(body: PasswordBody):
        def build(cfg: AppConfig) -> AppConfig:
            new_auth = replace(
                cfg.auth,
                enabled=True,
                password_hash=hash_password(body.password),
                secret=cfg.auth.secret or generate_secret(),
            )
            return replace(cfg, auth=new_auth)

        mutate(build)
        return {"ok": True, "enabled": True}

    @app.delete("/api/settings/password")
    def api_clear_password(request: Request):
        def build(cfg: AppConfig) -> AppConfig:
            return replace(cfg, auth=replace(cfg.auth, enabled=False, password_hash=None))

        mutate(build)
        request.session.clear()
        return {"ok": True, "enabled": False}

    # ---- dashboard ---------------------------------------------------- #
    @app.get("/api/dashboard")
    def api_get_dashboard():
        from dataclasses import asdict

        return {"panels": [asdict(p) for p in manager.config.dashboard.panels]}

    @app.put("/api/dashboard")
    def api_set_dashboard(body: DashboardBody):
        panels = [
            Panel(
                id=p.id, title=p.title, type=p.type, span=p.span, unit=p.unit,
                time_range=p.time_range, gauge_min=p.gauge_min, gauge_max=p.gauge_max,
                series=[Series(meter=s.meter, obis=s.obis, label=s.label, color=s.color) for s in p.series],
            )
            for p in body.panels
        ]
        mutate(lambda cfg: replace(cfg, dashboard=DashboardConfig(panels=panels)))
        return {"ok": True, "count": len(panels)}

    @app.get("/api/obis/{code:path}")
    def api_obis_name(code: str):
        return {"code": code, "name": obis_name(code)}

    # ---- PIN operations ---------------------------------------------- #
    @app.post("/api/meters/{meter_id}/pin")
    def api_send_pin(meter_id: str, body: PinBody):
        require_meter(meter_id)
        try:
            job = jobs.send(meter_id, body.pin)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "job": job.kind, "progress": job.progress.to_dict()}

    @app.post("/api/meters/{meter_id}/bruteforce")
    def api_bruteforce(meter_id: str, body: BruteforceBody):
        require_meter(meter_id)
        try:
            job = jobs.bruteforce(meter_id, body.length, body.start, body.end)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "job": job.kind, "progress": job.progress.to_dict()}

    @app.get("/api/meters/{meter_id}/pin")
    def api_pin_status(meter_id: str):
        require_meter(meter_id)
        job = jobs.get(meter_id)
        if job is None:
            return {"active": False, "progress": None}
        return {"active": job.running, "kind": job.kind, "progress": job.progress.to_dict()}

    @app.delete("/api/meters/{meter_id}/pin")
    def api_cancel_pin(meter_id: str):
        require_meter(meter_id)
        return {"ok": jobs.cancel(meter_id)}

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app
