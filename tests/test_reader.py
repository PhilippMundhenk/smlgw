import threading

from smlgw.reader import MeterReader
from smlgw.sml.builder import build_meter_frame
from smlgw.transport import BytesTransport

from conftest import wait_until

# Unit 27 (W) so the value is not divided by 1000, keeping the assertion simple.
FRAME = build_meter_frame(b"\x01\x02", {"1-0:16.7.0*255": dict(value=42, scaler=0, unit=27)})


def test_process_bytes_dispatches_results():
    seen = []
    reader = MeterReader(BytesTransport(), seen.append)
    results = reader.process_bytes(FRAME)
    assert len(results) == 1
    assert seen[0].as_dict()["1-0:16.7.0*255"].value_to_string() == "42"


def test_poll_once_empty_transport_returns_nothing():
    reader = MeterReader(BytesTransport([]), lambda r: None)
    assert reader.poll_once() == []


def test_callback_exception_does_not_propagate():
    def boom(_):
        raise RuntimeError("bad subscriber")

    reader = MeterReader(BytesTransport(), boom)
    # Must not raise despite the failing callback.
    assert len(reader.process_bytes(FRAME)) == 1


def test_run_reports_error_state_when_open_fails():
    class FailingTransport(BytesTransport):
        def open(self):
            raise OSError("port missing")

    reader = MeterReader(FailingTransport(), lambda r: None, reconnect_delay=10)
    stop = threading.Event()
    t = threading.Thread(target=reader.run, args=(stop,), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: reader.state == "error")
        assert "port missing" in (reader.last_error or "")
    finally:
        stop.set()
        t.join(2)


def test_run_reads_until_stopped():
    seen = []
    reader = MeterReader(BytesTransport([FRAME, FRAME]), seen.append, idle_delay=0.01)
    stop = threading.Event()
    t = threading.Thread(target=reader.run, args=(stop,), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: len(seen) >= 2)
    finally:
        stop.set()
        t.join(2)
    assert reader.state in ("reading", "connected", "stopped")
