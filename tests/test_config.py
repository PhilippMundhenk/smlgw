import os

from smlgw.config import AppConfig, Mapping, MeterConfig, load_config, save_config


def test_roundtrip(tmp_path):
    cfg = AppConfig()
    cfg.mqtt.host = "broker.example"
    cfg.mqtt.port = 8883
    cfg.mqtt.retain = True
    cfg.meters.append(
        MeterConfig(
            id="heating",
            name="Heating",
            port="/dev/ttyUSB0",
            mappings=[Mapping("1-0:1.8.0*255", "power/heating/total")],
        )
    )
    path = os.path.join(tmp_path, "config.yaml")
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.mqtt.host == "broker.example"
    assert loaded.mqtt.port == 8883
    assert loaded.mqtt.retain is True
    meter = loaded.get_meter("heating")
    assert meter is not None
    assert meter.mappings[0].topic == "power/heating/total"
    assert meter.mappings[0].obis == "1-0:1.8.0*255"


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(os.path.join(tmp_path, "nope.yaml"))
    assert cfg.meters == []
    assert cfg.mqtt.host == "localhost"


def test_meter_name_defaults_to_id():
    assert MeterConfig(id="x").name == "x"


def test_with_meter_replaces_by_id():
    cfg = AppConfig(meters=[MeterConfig(id="a", port="p1")])
    updated = cfg.with_meter(MeterConfig(id="a", port="p2"))
    assert len(updated.meters) == 1
    assert updated.get_meter("a").port == "p2"


def test_save_is_atomic_no_partial_file_left(tmp_path):
    path = os.path.join(tmp_path, "config.yaml")
    save_config(AppConfig(), path)
    # No leftover temp files from the atomic write.
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".config-")]
    assert leftovers == []
