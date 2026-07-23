"""Synthetic meters for tests, demos and the ``--simulate`` run mode.

These transports let the whole pipeline (decode -> discover -> publish, and PIN
entry -> unlock detection) run without any physical hardware.
"""

from __future__ import annotations

import time
from typing import Callable

from .config import PinConfig
from .sml.builder import build_meter_frame
from .transport import BytesTransport, Transport


def make_meter_transport(
    server_id: bytes,
    readings: dict[str, dict],
    *,
    repeat: bool = True,
) -> BytesTransport:
    """A transport that endlessly replays one meter frame built from *readings*."""
    frame = build_meter_frame(server_id, readings)
    return BytesTransport([frame], repeat=repeat)


class PacedFrameTransport(Transport):
    """Emits a frame at most once per *interval* seconds, ``b""`` in between.

    This mimics a real meter's cadence so ``--simulate`` behaves like hardware
    (a reading every second or so) instead of flooding MQTT.
    """

    def __init__(
        self,
        frame: bytes,
        *,
        interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frame = frame
        self._interval = interval
        self._clock = clock
        self._last = 0.0
        self._open = False

    def open(self) -> None:
        self._open = True

    def read(self, size: int) -> bytes:
        now = self._clock()
        if now - self._last >= self._interval:
            self._last = now
            return self._frame
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


# A representative residential three-phase meter.
DEMO_SERVER_ID = bytes.fromhex("0a01484c5900010203")
DEMO_READINGS: dict[str, dict] = {
    "1-0:96.1.0*255": dict(value=bytes.fromhex("0102030405060708")),
    "1-0:1.8.0*255": dict(value=734512, scaler=-1, unit=30),
    "1-0:1.8.1*255": dict(value=500000, scaler=-1, unit=30),
    "1-0:1.8.2*255": dict(value=234512, scaler=-1, unit=30),
    "1-0:2.8.0*255": dict(value=12000, scaler=-1, unit=30),
    "1-0:16.7.0*255": dict(value=4123, scaler=-1, unit=27),
    "1-0:32.7.0*255": dict(value=2301, scaler=-1, unit=35),
}


def demo_transport() -> PacedFrameTransport:
    return PacedFrameTransport(build_meter_frame(DEMO_SERVER_ID, DEMO_READINGS))


class UnlockableTransport(Transport):
    """A meter that stays silent until the correct PIN pulses are entered.

    It models the optical lock: it emits empty frames until the PIN is entered,
    after which it replays a full data frame.  The unlock heuristic counts the
    pulses written between reads (one PIN attempt) and compares against the
    pulse count the correct PIN produces -- realistic enough to exercise the
    real :class:`~smlgw.pin.PinController` end to end.
    """

    def __init__(
        self,
        correct_pin: str,
        *,
        pin_config: PinConfig | None = None,
        server_id: bytes = DEMO_SERVER_ID,
        readings: dict[str, dict] | None = None,
    ) -> None:
        cfg = pin_config or PinConfig()
        self._pulse = bytes.fromhex(cfg.pulse)
        # reset() emits 2 pulses, then each digit emits its value in pulses.
        self._expected_pulses = 2 + sum(int(d) for d in correct_pin)
        self._frame = build_meter_frame(server_id, readings or DEMO_READINGS)
        self._pulses_this_attempt = 0
        self.unlocked = False
        self._open = False
        self.written = bytearray()

    def open(self) -> None:
        self._open = True

    def write(self, data: bytes) -> int:
        self.written += data
        if self._pulse and len(data) == len(self._pulse):
            self._pulses_this_attempt += 1
        return len(data)

    def read(self, size: int) -> bytes:
        if self._pulses_this_attempt:
            if self._pulses_this_attempt == self._expected_pulses:
                self.unlocked = True
            self._pulses_this_attempt = 0
        return self._frame if self.unlocked else b""

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open
