"""Configuration model and persistence.

Configuration lives in a single YAML file.  It is intentionally plain
dataclasses (no framework binding) so it is trivial to construct in tests, and
so the web layer can round-trip it without a schema mismatch.  Saving is atomic
(write to a temp file then replace) to avoid a truncated config if the process
dies mid-write.
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("SMLGW_CONFIG", "config.yaml")


@dataclass
class Mapping:
    """Maps one discovered OBIS code to an MQTT topic."""

    obis: str
    topic: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Mapping":
        return cls(
            obis=str(data["obis"]),
            topic=str(data["topic"]),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class MeterConfig:
    id: str
    name: str = ""
    port: str = ""
    baudrate: int = 9600
    enabled: bool = True
    verify_crc: bool = False
    pin: str | None = None
    mappings: list[Mapping] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id

    @classmethod
    def from_dict(cls, data: dict) -> "MeterConfig":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            port=str(data.get("port", "")),
            baudrate=int(data.get("baudrate", 9600)),
            enabled=bool(data.get("enabled", True)),
            verify_crc=bool(data.get("verify_crc", False)),
            pin=(str(data["pin"]) if data.get("pin") not in (None, "") else None),
            mappings=[Mapping.from_dict(m) for m in data.get("mappings", [])],
        )


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "smlgw"
    tls: bool = False
    retain: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "MqttConfig":
        data = data or {}
        return cls(
            host=str(data.get("host", "localhost")),
            port=int(data.get("port", 1883)),
            username=data.get("username") or None,
            password=data.get("password") or None,
            client_id=str(data.get("client_id", "smlgw")),
            tls=bool(data.get("tls", False)),
            retain=bool(data.get("retain", False)),
        )


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_dict(cls, data: dict) -> "WebConfig":
        data = data or {}
        return cls(host=str(data.get("host", "0.0.0.0")), port=int(data.get("port", 8000)))


@dataclass
class PinConfig:
    """Timing/waveform parameters for optical PIN entry (see :mod:`smlgw.pin`)."""

    pulse: str = "00" * 30  # hex bytes written for one "flash" of the IR LED
    digit_gap: float = 1.0  # seconds between pulses of the same digit
    group_gap: float = 3.0  # seconds between digits
    settle: float = 2.0  # seconds after the reset before entering digits
    detect_timeout: float = 20.0  # seconds to wait for the meter to unlock
    detect_obis: str = "1-0:1.8.0*255"  # reading whose presence proves unlock

    @classmethod
    def from_dict(cls, data: dict) -> "PinConfig":
        data = data or {}
        base = cls()
        return cls(
            pulse=str(data.get("pulse", base.pulse)),
            digit_gap=float(data.get("digit_gap", base.digit_gap)),
            group_gap=float(data.get("group_gap", base.group_gap)),
            settle=float(data.get("settle", base.settle)),
            detect_timeout=float(data.get("detect_timeout", base.detect_timeout)),
            detect_obis=str(data.get("detect_obis", base.detect_obis)),
        )


@dataclass
class HistoryConfig:
    """Time-series retention settings for the historical value store."""

    enabled: bool = True
    retention_hours: float = 168.0  # keep 7 days by default
    sample_interval: float = 10.0  # min seconds between stored samples per series
    db_path: str | None = None  # default: history.db next to the config file

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryConfig":
        data = data or {}
        base = cls()
        return cls(
            enabled=bool(data.get("enabled", base.enabled)),
            retention_hours=float(data.get("retention_hours", base.retention_hours)),
            sample_interval=float(data.get("sample_interval", base.sample_interval)),
            db_path=data.get("db_path") or None,
        )


@dataclass
class Series:
    """One line/value in a dashboard panel: a (meter, obis) source."""

    meter: str
    obis: str
    label: str = ""
    color: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Series":
        return cls(
            meter=str(data["meter"]),
            obis=str(data["obis"]),
            label=str(data.get("label", "")),
            color=str(data.get("color", "")),
        )


@dataclass
class Panel:
    """A dashboard panel (Grafana-style): line chart, single stat, or gauge."""

    id: str
    title: str = ""
    type: str = "line"  # line | stat | gauge
    span: int = 1  # grid width, 1 or 2 columns
    unit: str = ""
    time_range: float = 3600.0  # seconds of history shown (line panels)
    gauge_min: float = 0.0
    gauge_max: float = 100.0
    series: list[Series] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Panel":
        base = cls(id=str(data["id"]))
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            type=str(data.get("type", "line")),
            span=int(data.get("span", 1)),
            unit=str(data.get("unit", "")),
            time_range=float(data.get("time_range", base.time_range)),
            gauge_min=float(data.get("gauge_min", base.gauge_min)),
            gauge_max=float(data.get("gauge_max", base.gauge_max)),
            series=[Series.from_dict(s) for s in data.get("series", [])],
        )


@dataclass
class DashboardConfig:
    panels: list[Panel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "DashboardConfig":
        data = data or {}
        return cls(panels=[Panel.from_dict(p) for p in data.get("panels", [])])


@dataclass
class AuthConfig:
    """Optional password protection for the web UI."""

    enabled: bool = False
    password_hash: str | None = None  # "pbkdf2$iterations$salt_hex$hash_hex"
    secret: str | None = None  # session cookie signing secret

    @classmethod
    def from_dict(cls, data: dict) -> "AuthConfig":
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", False)),
            password_hash=data.get("password_hash") or None,
            secret=data.get("secret") or None,
        )


@dataclass
class AppConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    web: WebConfig = field(default_factory=WebConfig)
    pin: PinConfig = field(default_factory=PinConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    meters: list[MeterConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        data = data or {}
        return cls(
            mqtt=MqttConfig.from_dict(data.get("mqtt", {})),
            web=WebConfig.from_dict(data.get("web", {})),
            pin=PinConfig.from_dict(data.get("pin", {})),
            history=HistoryConfig.from_dict(data.get("history", {})),
            dashboard=DashboardConfig.from_dict(data.get("dashboard", {})),
            auth=AuthConfig.from_dict(data.get("auth", {})),
            meters=[MeterConfig.from_dict(m) for m in data.get("meters", [])],
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def get_meter(self, meter_id: str) -> MeterConfig | None:
        return next((m for m in self.meters if m.id == meter_id), None)

    def with_meter(self, meter: MeterConfig) -> "AppConfig":
        """Return a copy with *meter* inserted or replacing one of the same id."""
        meters = [m for m in self.meters if m.id != meter.id]
        meters.append(meter)
        return replace(self, meters=meters)


_SAVE_LOCK = threading.Lock()


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load configuration from *path*; return defaults if it does not exist."""
    if not os.path.exists(path):
        return AppConfig()
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return AppConfig.from_dict(data)


def save_config(config: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    """Atomically persist *config* to *path*."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = yaml.safe_dump(config.to_dict(), sort_keys=False, allow_unicode=True)
    with _SAVE_LOCK:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
