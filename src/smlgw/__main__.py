"""Command line entry point.

    smlgw run                 # gateway + web UI (default)
    smlgw run --simulate      # no hardware: replay synthetic meters
    smlgw bruteforce <meter>  # headless PIN recovery (like the old pin.sh)

Everything is driven from the YAML config (``--config`` / ``$SMLGW_CONFIG``).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from .config import DEFAULT_CONFIG_PATH, MeterConfig, load_config
from .manager import MeterManager
from .publisher import MqttPublisher


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _mqtt_publisher(mqtt_config) -> MqttPublisher:
    return MqttPublisher(
        mqtt_config.host,
        mqtt_config.port,
        username=mqtt_config.username,
        password=mqtt_config.password,
        client_id=mqtt_config.client_id,
        tls=mqtt_config.tls,
        default_retain=mqtt_config.retain,
    )


def _build_history(config, config_path: str, simulate: bool):
    from .history import HistoryStore

    if simulate:
        db_path = ":memory:"
    else:
        db_path = config.history.db_path or os.path.join(
            os.path.dirname(os.path.abspath(config_path)) or ".", "history.db"
        )
    return HistoryStore(
        db_path,
        retention_hours=config.history.retention_hours,
        sample_interval=config.history.sample_interval,
    )


def _build_manager(config, config_path: str, simulate: bool) -> MeterManager:
    history = _build_history(config, config_path, simulate)
    if simulate:
        from .publisher import RecordingPublisher
        from .simulator import demo_transport

        return MeterManager(
            config,
            RecordingPublisher(),
            transport_factory=lambda cfg: demo_transport(),
            history=history,
        )
    return MeterManager(
        config,
        _mqtt_publisher(config.mqtt),
        history=history,
        mqtt_factory=_mqtt_publisher,
    )


def cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    config = load_config(args.config)
    if args.simulate and not config.meters:
        # Ephemeral demo meter; not persisted so simulate needs no writable config.
        config.meters.append(MeterConfig(id="demo", name="Simulated meter", port="sim"))

    manager = _build_manager(config, args.config, args.simulate)
    manager.start()

    app = create_app(manager, args.config)

    def _shutdown(*_):
        manager.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *a: (_shutdown(), sys.exit(0)))
        except (ValueError, OSError):
            pass  # not on main thread / unsupported platform

    host = args.host or config.web.host
    port = args.port or config.web.port
    logging.getLogger(__name__).info("web UI on http://%s:%s", host, port)
    try:
        uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())
    finally:
        manager.stop()
    return 0


def cmd_bruteforce(args: argparse.Namespace) -> int:
    from .pin import BruteforceRunner, PinController
    from .transport import SerialTransport

    config = load_config(args.config)
    meter = config.get_meter(args.meter)
    if meter is None:
        print(f"unknown meter {args.meter!r}", file=sys.stderr)
        return 2

    transport = SerialTransport(meter.port, meter.baudrate)
    transport.open()

    def on_progress(p):
        if p.current:
            print(f"\rtrying {p.current}  ({p.tried}/{p.total})", end="", flush=True)

    controller = PinController(transport, config.pin)
    runner = BruteforceRunner(
        controller, length=args.length, start=args.start, end=args.end, on_progress=on_progress
    )
    try:
        found = runner.run()
    finally:
        transport.close()
    print()
    if found:
        print(f"PIN found: {found}")
        return 0
    print("PIN not found in range")
    return 1


def build_parser() -> argparse.ArgumentParser:
    # Common options usable either before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="path to config.yaml")
    common.add_argument("--log-level", default="info", help="debug|info|warning|error")

    parser = argparse.ArgumentParser(
        prog="smlgw", description="Smart meter to MQTT gateway", parents=[common]
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the gateway and web UI", parents=[common])
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--simulate", action="store_true", help="use synthetic meters, no hardware")
    run.set_defaults(func=cmd_run)

    bf = sub.add_parser("bruteforce", help="headless PIN recovery for one meter", parents=[common])
    bf.add_argument("meter", help="meter id from the config")
    bf.add_argument("--length", type=int, default=4)
    bf.add_argument("--start", type=int, default=0)
    bf.add_argument("--end", type=int, default=None)
    bf.set_defaults(func=cmd_bruteforce)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    if not getattr(args, "func", None):
        args.host = args.port = None
        args.simulate = False
        return cmd_run(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
