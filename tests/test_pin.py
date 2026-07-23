from smlgw.config import PinConfig
from smlgw.pin import BruteforceRunner, PinController
from smlgw.simulator import UnlockableTransport
from smlgw.transport import BytesTransport

# Fast, zero-delay timings so tests run instantly.
FAST = PinConfig(pulse="00", digit_gap=0.0, group_gap=0.0, settle=0.0, detect_timeout=0.2)


def _controller_over(transport):
    return PinController(transport, FAST, sleep=lambda *_: None)


def test_enter_pin_writes_expected_pulse_count():
    t = BytesTransport()
    ctrl = _controller_over(t)
    ctrl.enter_pin("1234")
    # reset() = 2 pulses; digits add 1+2+3+4 = 10; total 12 single-byte pulses.
    assert len(t.written) == 12


def test_enter_pin_rejects_non_digits():
    ctrl = _controller_over(BytesTransport())
    try:
        ctrl.enter_pin("12a4")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_detect_unlock_true_when_target_present():
    from smlgw.sml.builder import build_meter_frame

    frame = build_meter_frame(b"\x01", {"1-0:1.8.0*255": dict(value=1, scaler=0, unit=30)})
    ctrl = _controller_over(BytesTransport([frame]))
    assert ctrl.detect_unlock(timeout=0.5) is True


def test_detect_unlock_false_when_absent():
    from smlgw.sml.builder import build_meter_frame

    # Only an unrelated code -> target 1.8.0 never appears.
    frame = build_meter_frame(b"\x01", {"1-0:16.7.0*255": dict(value=1, scaler=0, unit=27)})
    ctrl = _controller_over(BytesTransport([frame], repeat=True))
    assert ctrl.detect_unlock(timeout=0.2) is False


def test_end_to_end_correct_pin_unlocks_meter():
    transport = UnlockableTransport("0003", pin_config=FAST)
    transport.open()
    ctrl = _controller_over(transport)
    assert ctrl.try_pin("0003") is True


def test_end_to_end_wrong_pin_does_not_unlock():
    transport = UnlockableTransport("0003", pin_config=FAST)
    transport.open()
    ctrl = _controller_over(transport)
    assert ctrl.try_pin("0009") is False


def test_bruteforce_finds_pin_via_sweep():
    transport = UnlockableTransport("0003", pin_config=FAST)
    transport.open()
    ctrl = _controller_over(transport)
    runner = BruteforceRunner(ctrl, length=4, start=0, end=20)
    found = runner.run()
    assert found == "0003"
    assert runner.progress.finished is True
    assert runner.progress.found == "0003"


def test_bruteforce_reports_not_found_in_range():
    # Correct pin's digit-sum is unreachable by any candidate in 0000-0005,
    # so the sweep genuinely finds nothing.
    transport = UnlockableTransport("0009", pin_config=FAST)
    transport.open()
    ctrl = _controller_over(transport)
    runner = BruteforceRunner(ctrl, length=4, start=0, end=5)
    assert runner.run() is None
    assert runner.progress.finished is True


def test_bruteforce_progress_and_cancel_with_stub():
    class StubController:
        def __init__(self):
            self.tried = []

        def try_pin(self, pin, stop=None):
            self.tried.append(pin)
            return False

    stub = StubController()
    seen = []
    runner = BruteforceRunner(stub, length=4, start=0, end=9999, on_progress=lambda p: seen.append(p.tried))
    # Cancel almost immediately from the progress callback.
    runner.on_progress = lambda p: runner.cancel()
    runner.run()
    assert runner.progress.cancelled is True
    assert len(stub.tried) < 9999  # stopped early
