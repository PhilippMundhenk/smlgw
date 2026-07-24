"""Proves the new gateway publishes byte-for-byte what gateway.js did.

The legacy Node gateway published, per meter, the numeric part of each OBIS
value (``valueToString().split(" ")[0]``) to fixed topics:

    heating: power/heating/{total,ht,nt,current}  <- 1.8.0, 1.8.1, 1.8.2, 16.7.0
    house:   power/house/{total,current}          <- 1.8.0, 16.7.0

We simulate both meters over the SML protocol and assert the exact topics and
payloads.
"""

from smlgw.config import AppConfig, Mapping, MeterConfig, MqttConfig
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.sml.builder import build_meter_frame
from smlgw.transport import BytesTransport

from conftest import wait_until

HEATING = build_meter_frame(
    bytes.fromhex("0a01484541540001"),
    {
        "1-0:1.8.0*255": dict(value=734512, scaler=-1, unit=30),
        "1-0:1.8.1*255": dict(value=500000, scaler=-1, unit=30),
        "1-0:1.8.2*255": dict(value=234512, scaler=-1, unit=30),
        "1-0:16.7.0*255": dict(value=4123, scaler=-1, unit=27),
    },
)
HOUSE = build_meter_frame(
    bytes.fromhex("0a01484f55530002"),
    {
        "1-0:1.8.0*255": dict(value=812345, scaler=-1, unit=30),
        "1-0:16.7.0*255": dict(value=2100, scaler=-1, unit=27),
    },
)

FRAMES = {"heating": HEATING, "house": HOUSE}


def _config() -> AppConfig:
    return AppConfig(
        mqtt=MqttConfig(host="test", retain=False),
        meters=[
            MeterConfig(
                id="heating",
                port="sim-heating",
                mappings=[
                    Mapping("1-0:1.8.0*255", "power/heating/total"),
                    Mapping("1-0:1.8.1*255", "power/heating/ht"),
                    Mapping("1-0:1.8.2*255", "power/heating/nt"),
                    Mapping("1-0:16.7.0*255", "power/heating/current"),
                ],
            ),
            MeterConfig(
                id="house",
                port="sim-house",
                mappings=[
                    Mapping("1-0:1.8.0*255", "power/house/total"),
                    Mapping("1-0:16.7.0*255", "power/house/current"),
                ],
            ),
        ],
    )


def test_publishes_identical_topics_and_payloads():
    publisher = RecordingPublisher()
    manager = MeterManager(
        _config(),
        publisher,
        transport_factory=lambda cfg: BytesTransport([FRAMES[cfg.id]]),
    )
    manager.start()
    try:
        assert wait_until(lambda: "power/house/current" in publisher.topics)
        assert wait_until(lambda: "power/heating/current" in publisher.topics)
    finally:
        manager.stop()

    topics = publisher.topics
    # Energy registers (unit 30, Wh) are divided by 1000 -> kWh, exactly as the
    # legacy smartmeter-obis library did. Power (unit 27, W) is unchanged.
    assert topics["power/heating/total"] == "73.4512"
    assert topics["power/heating/ht"] == "50"
    assert topics["power/heating/nt"] == "23.4512"
    assert topics["power/heating/current"] == "412.3"
    assert topics["power/house/total"] == "81.2345"
    assert topics["power/house/current"] == "210"


def test_disabled_mapping_is_not_published():
    cfg = _config()
    cfg.get_meter("heating").mappings[1].enabled = False  # disable ht
    publisher = RecordingPublisher()
    manager = MeterManager(
        cfg, publisher, transport_factory=lambda c: BytesTransport([FRAMES[c.id]])
    )
    manager.start()
    try:
        assert wait_until(lambda: "power/heating/total" in publisher.topics)
    finally:
        manager.stop()
    assert "power/heating/ht" not in publisher.topics


def test_mapping_unit_override_changes_published_payload():
    # Same meter, but the total is mapped with an explicit "Wh" output unit.
    cfg = AppConfig(
        mqtt=MqttConfig(host="test"),
        meters=[
            MeterConfig(
                id="heating",
                port="sim-heating",
                mappings=[Mapping("1-0:1.8.0*255", "power/heating/total_wh", unit="Wh")],
            )
        ],
    )
    publisher = RecordingPublisher()
    manager = MeterManager(
        cfg, publisher, transport_factory=lambda c: BytesTransport([HEATING])
    )
    manager.start()
    try:
        assert wait_until(lambda: "power/heating/total_wh" in publisher.topics)
    finally:
        manager.stop()
    # 734512 * 10^-1 = 73451.2 Wh (no /1000), instead of the default 73.4512 kWh.
    assert publisher.topics["power/heating/total_wh"] == "73451.2"


def test_retain_flag_follows_mqtt_config():
    cfg = _config()
    cfg.mqtt.retain = True
    publisher = RecordingPublisher()
    manager = MeterManager(
        cfg, publisher, transport_factory=lambda c: BytesTransport([FRAMES[c.id]])
    )
    manager.start()
    try:
        assert wait_until(lambda: publisher.messages)
    finally:
        manager.stop()
    assert all(retain is True for _, _, retain in publisher.messages)
