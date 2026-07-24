"""Orchestration: run one independent, resilient worker per meter.

This module fixes the central weakness of the legacy gateway.  There, both
meters were initialised eagerly in the main thread and a single offline port
took the whole process down.  Here every meter runs in its own thread with its
own reconnect loop, so a missing or faulty meter only affects itself.  The
manager also records every OBIS code each meter emits (\"discovered values\"),
which is what the web UI offers up for MQTT mapping.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from .config import AppConfig, MeterConfig
from .obis import obis_name
from .publisher import Publisher
from .reader import MeterReader
from .sml import SmlGetListResult
from .transport import SerialTransport, Transport

log = logging.getLogger(__name__)

TransportFactory = Callable[[MeterConfig], Transport]


def default_transport_factory(meter: MeterConfig) -> Transport:
    return SerialTransport(meter.port, meter.baudrate)


@dataclass
class DiscoveredValue:
    code: str
    name: str
    value: str
    unit: str
    last_seen: float
    count: int = 0
    unit_options: list = None  # selectable output-unit labels

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "last_seen": self.last_seen,
            "count": self.count,
            "unit_options": self.unit_options or [],
        }


class MeterWorker:
    """Owns the transport, reader thread and discovered-value cache for a meter."""

    def __init__(
        self,
        manager: "MeterManager",
        config: MeterConfig,
        transport: Transport,
    ) -> None:
        self.manager = manager
        self.config = config
        self.transport = transport
        self.discovered: dict[str, DiscoveredValue] = {}
        self.server_id: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.reader = MeterReader(
            transport,
            self._on_result,
            name=config.id,
            verify_crc=config.verify_crc,
        )

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self.reader.run, args=(self._stop,), name=f"meter-{self.config.id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            # Only forget the thread if it actually finished; otherwise keep the
            # reference so a later start() won't spawn a second reader on the
            # same transport.
            if not thread.is_alive():
                self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- result handling -------------------------------------------------- #
    def _on_result(self, result: SmlGetListResult) -> None:
        now = self.manager.clock()
        if result.server_id is not None:
            self.server_id = result.server_id.hex()
        # The chosen output unit is a per-OBIS presentation setting; it applies to
        # display, publishing and history alike so they never disagree.
        unit_by_obis = {m.obis: m.unit for m in self.config.mappings}
        enabled = {m.obis: m for m in self.config.mappings if m.enabled}
        with self._lock:
            for value in result.values:
                unit_label = unit_by_obis.get(value.code)
                existing = self.discovered.get(value.code)
                count = (existing.count + 1) if existing else 1
                self.discovered[value.code] = DiscoveredValue(
                    code=value.code,
                    name=obis_name(value.code),
                    value=value.value_to_string(unit_label),
                    unit=value.unit_for(unit_label),
                    last_seen=now,
                    count=count,
                    unit_options=value.unit_options(),
                )
                mapping = enabled.get(value.code)
                if mapping is not None:
                    self.manager.publish(mapping.topic, value.value_to_string(mapping.unit))
                numeric = value.numeric_value(unit_label)
                if numeric is not None:
                    self.manager.record_history(self.config.id, value.code, numeric, now)

    def snapshot(self) -> list[DiscoveredValue]:
        with self._lock:
            return sorted(self.discovered.values(), key=lambda d: d.code)

    def status(self) -> dict:
        snap = self.snapshot()
        last_seen = max((d.last_seen for d in snap), default=None)
        return {
            "id": self.config.id,
            "name": self.config.name,
            "port": self.config.port,
            "enabled": self.config.enabled,
            "running": self.running,
            "state": self.reader.state,
            "last_error": self.reader.last_error,
            "server_id": self.server_id,
            "discovered_count": len(snap),
            "last_seen": last_seen,
            "has_data": bool(snap),
        }


class MeterManager:
    def __init__(
        self,
        config: AppConfig,
        publisher: Publisher,
        *,
        transport_factory: TransportFactory | None = None,
        clock: Callable[[], float] = time.time,
        history=None,
        mqtt_factory=None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self.transport_factory = transport_factory or default_transport_factory
        self.clock = clock
        self.history = history  # optional HistoryStore
        # Optional Callable[[MqttConfig], Publisher] enabling live broker changes.
        self.mqtt_factory = mqtt_factory
        self._workers: dict[str, MeterWorker] = {}
        self._reserved: set[str] = set()  # meters temporarily held for PIN entry
        self._stopped = False
        self._lock = threading.Lock()

    # -- publishing ------------------------------------------------------- #
    def publish(self, topic: str, payload: str) -> None:
        try:
            self.publisher.publish(topic, payload, retain=self.config.mqtt.retain)
        except Exception:  # a publish failure must not break decoding
            log.exception("failed to publish %s", topic)

    def reconfigure_mqtt(self) -> None:
        """Rebuild the publisher from the current config (live broker change).

        No-op when no ``mqtt_factory`` was supplied (e.g. in tests); the config
        change then simply takes effect on the next restart.
        """
        if self.mqtt_factory is None:
            return
        old = self.publisher
        try:
            old.disconnect()
        except Exception:
            log.exception("failed to disconnect old MQTT publisher")
        self.publisher = self.mqtt_factory(self.config.mqtt)
        try:
            self.publisher.connect()
        except Exception:
            log.exception("failed to connect new MQTT publisher")

    def record_history(self, meter_id: str, obis: str, value: float, ts: float) -> None:
        if self.history is None or not self.config.history.enabled:
            return
        try:
            self.history.record(meter_id, obis, value, ts)
        except Exception:  # history is best-effort; never break decoding
            log.exception("failed to record history for %s/%s", meter_id, obis)

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        try:
            self.publisher.connect()
        except Exception:
            log.exception("mqtt connect failed; will keep retrying in background")
        with self._lock:
            for meter in self.config.meters:
                if meter.enabled:
                    self._start_worker(meter)

    def _start_worker(self, meter: MeterConfig) -> MeterWorker:
        transport = self.transport_factory(meter)
        worker = MeterWorker(self, meter, transport)
        self._workers[meter.id] = worker
        worker.start()
        log.info("started meter worker %s (%s)", meter.id, meter.port)
        return worker

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for worker in self._workers.values():
                worker.stop()
            self._workers.clear()
        try:
            self.publisher.disconnect()
        except Exception:
            log.exception("mqtt disconnect failed")

    def apply_config(self, new_config: AppConfig) -> None:
        """Reconcile running workers with *new_config* (add/remove/restart)."""
        with self._lock:
            self.config = new_config
            wanted = {m.id: m for m in new_config.meters if m.enabled}
            # Remove workers that are gone or changed. Meters reserved for a PIN
            # operation are left alone; they are reconciled when released.
            for meter_id in list(self._workers.keys()):
                if meter_id in self._reserved:
                    continue
                worker = self._workers[meter_id]
                new = wanted.get(meter_id)
                if new is None or new != worker.config:
                    worker.stop()
                    del self._workers[meter_id]
            # (Re)start anything wanted that is not currently running.
            for meter_id, meter in wanted.items():
                if meter_id in self._reserved:
                    continue
                if meter_id not in self._workers:
                    self._start_worker(meter)

    # -- introspection ---------------------------------------------------- #
    def get_worker(self, meter_id: str) -> MeterWorker | None:
        return self._workers.get(meter_id)

    def status(self) -> list[dict]:
        result = []
        for meter in self.config.meters:
            worker = self._workers.get(meter.id)
            if worker is not None:
                result.append(worker.status())
            else:
                result.append(
                    {
                        "id": meter.id,
                        "name": meter.name,
                        "port": meter.port,
                        "enabled": meter.enabled,
                        "running": False,
                        "state": "stopped",
                        "last_error": None,
                        "server_id": None,
                        "discovered_count": 0,
                        "last_seen": None,
                        "has_data": False,
                    }
                )
        return result

    def discovered(self, meter_id: str) -> list[DiscoveredValue]:
        worker = self._workers.get(meter_id)
        return worker.snapshot() if worker else []

    @contextmanager
    def exclusive_transport(self, meter_id: str) -> Iterator[Transport]:
        """Pause the meter's reader and yield its transport for exclusive use.

        Used by PIN entry, which must both write to and read from the port
        without the background reader competing for bytes.  The meter is
        *reserved* under the manager lock so a concurrent ``apply_config`` or
        another PIN job cannot spin up a second reader on the same port; on exit
        the worker is reconciled against the *current* config and resumed.
        """
        with self._lock:
            if meter_id in self._reserved:
                raise RuntimeError(f"meter {meter_id!r} is busy with another PIN operation")
            meter = self.config.get_meter(meter_id)
            if meter is None:
                raise KeyError(f"unknown meter {meter_id!r}")
            self._reserved.add(meter_id)
            worker = self._workers.pop(meter_id, None)
        was_running = worker is not None and worker.running
        if worker is not None:
            worker.stop()
        transport = worker.transport if worker is not None else self.transport_factory(meter)
        try:
            if not transport.is_open:
                transport.open()
            yield transport
        finally:
            try:
                transport.close()
            except Exception:
                pass
            with self._lock:
                self._reserved.discard(meter_id)
                current = self.config.get_meter(meter_id)
                if not self._stopped and was_running and current is not None and current.enabled:
                    self._start_worker(current)
