from smlgw.config import AppConfig, MeterConfig
from smlgw.manager import MeterManager
from smlgw.publisher import RecordingPublisher
from smlgw.sml.builder import build_meter_frame
from smlgw.transport import BytesTransport

from conftest import wait_until

FRAME = build_meter_frame(
    b"\xaa\xbb",
    {
        "1-0:1.8.0*255": dict(value=100, scaler=0, unit=30),
        "1-0:16.7.0*255": dict(value=50, scaler=0, unit=27),
    },
)


class OfflineTransport(BytesTransport):
    def open(self):
        raise OSError("device /dev/ttyUSB9 not found")


def test_offline_meter_does_not_stop_others():
    """The central fix: one dead meter must not take the system down."""
    cfg = AppConfig(
        meters=[
            MeterConfig(id="online", port="p0"),
            MeterConfig(id="offline", port="p9"),
        ]
    )

    def factory(meter):
        return OfflineTransport() if meter.id == "offline" else BytesTransport([FRAME, FRAME])

    manager = MeterManager(cfg, RecordingPublisher(), transport_factory=factory)
    manager.start()
    try:
        # The healthy meter discovers values...
        assert wait_until(lambda: manager.discovered("online"))
        # ...while the broken one merely reports an error.
        assert wait_until(lambda: manager.get_worker("offline").reader.state == "error")
        status = {s["id"]: s for s in manager.status()}
        assert status["online"]["has_data"] is True
        assert status["offline"]["has_data"] is False
        assert "not found" in (status["offline"]["last_error"] or "")
    finally:
        manager.stop()


def test_discovered_values_are_tracked_with_counts():
    cfg = AppConfig(meters=[MeterConfig(id="m", port="p")])
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport([FRAME, FRAME])
    )
    manager.start()
    try:
        assert wait_until(lambda: len(manager.discovered("m")) == 2)
        # Read while the workers still exist (stop() clears them).
        snapshot = manager.discovered("m")
        codes = {d.code for d in snapshot}
        assert codes == {"1-0:1.8.0*255", "1-0:16.7.0*255"}
        assert all(d.count >= 1 for d in snapshot)
    finally:
        manager.stop()


def test_apply_config_starts_and_stops_workers():
    cfg = AppConfig(meters=[MeterConfig(id="a", port="p")])
    manager = MeterManager(
        cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport([FRAME], repeat=True)
    )
    manager.start()
    try:
        assert wait_until(lambda: manager.get_worker("a") is not None)
        # Add a second meter and disable the first.
        new_cfg = AppConfig(
            meters=[MeterConfig(id="a", port="p", enabled=False), MeterConfig(id="b", port="p2")]
        )
        manager.apply_config(new_cfg)
        assert manager.get_worker("a") is None
        assert wait_until(lambda: manager.get_worker("b") is not None)
    finally:
        manager.stop()


def test_disabled_meter_not_started():
    cfg = AppConfig(meters=[MeterConfig(id="a", port="p", enabled=False)])
    manager = MeterManager(cfg, RecordingPublisher(), transport_factory=lambda c: BytesTransport())
    manager.start()
    try:
        assert manager.get_worker("a") is None
        status = {s["id"]: s for s in manager.status()}
        assert status["a"]["running"] is False
    finally:
        manager.stop()
