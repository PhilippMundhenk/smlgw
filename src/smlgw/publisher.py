"""MQTT publishing.

The payload format matches the legacy Node gateway exactly: the plain scaled
numeric value is published as the message body (e.g. topic
``power/heating/total`` with payload ``12345.6``), so existing subscribers,
dashboards and Home Assistant sensors keep working unchanged.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Publisher(Protocol):
    """Anything the gateway can publish readings through."""

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    @property
    def connected(self) -> bool: ...


class RecordingPublisher:
    """In-memory publisher for tests: records every published message."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, bool]] = []
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self.messages.append((topic, payload, retain))

    @property
    def connected(self) -> bool:
        return self._connected

    def payloads_for(self, topic: str) -> list[str]:
        return [p for t, p, _ in self.messages if t == topic]

    @property
    def topics(self) -> dict[str, str]:
        """Latest payload seen per topic."""
        latest: dict[str, str] = {}
        for topic, payload, _ in self.messages:
            latest[topic] = payload
        return latest


class MqttPublisher:
    """A :class:`Publisher` backed by ``paho-mqtt`` with auto-reconnect."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "smlgw",
        keepalive: int = 60,
        tls: bool = False,
        default_retain: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.keepalive = keepalive
        self.tls = tls
        self.default_retain = default_retain
        self._client = None

    def _build_client(self):
        import paho.mqtt.client as mqtt

        try:  # paho-mqtt 2.x requires an explicit callback API version
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id
            )
        except (AttributeError, TypeError):  # paho-mqtt 1.x fallback
            client = mqtt.Client(client_id=self.client_id)
        if self.username:
            client.password_pw_set = getattr(client, "username_pw_set")
            client.username_pw_set(self.username, self.password)
        if self.tls:
            client.tls_set()
        return client

    def connect(self) -> None:
        if self._client is not None:
            return
        self._client = self._build_client()
        # loop_start manages reconnects on its own background thread.
        self._client.connect_async(self.host, self.port, self.keepalive)
        self._client.loop_start()
        log.info("mqtt: connecting to %s:%s", self.host, self.port)

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            finally:
                self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def publish(self, topic: str, payload: str, *, retain: bool | None = None) -> None:
        if self._client is None:
            self.connect()
        if retain is None:
            retain = self.default_retain
        result = self._client.publish(topic, payload, qos=0, retain=retain)
        log.debug("mqtt publish %s = %s (rc=%s)", topic, payload, getattr(result, "rc", "?"))
