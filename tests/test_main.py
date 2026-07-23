"""Smoke tests for the CLI entry point wiring.

These exercise the non-``--simulate`` code paths (which the container smoke
tests skip), so missing imports or bad wiring in __main__ are caught here.
"""

import os

from smlgw import __main__ as cli
from smlgw.config import AppConfig


def test_build_history_non_simulate_uses_db_file(tmp_path):
    store = cli._build_history(AppConfig(), os.path.join(tmp_path, "config.yaml"), simulate=False)
    try:
        assert store.db_path.endswith("history.db")
        assert os.path.dirname(store.db_path) == str(tmp_path)
    finally:
        store.close()


def test_build_history_simulate_is_in_memory():
    store = cli._build_history(AppConfig(), "whatever.yaml", simulate=True)
    try:
        assert store.db_path == ":memory:"
    finally:
        store.close()


def test_build_manager_non_simulate_wires_history_and_mqtt(tmp_path):
    manager = cli._build_manager(AppConfig(), os.path.join(tmp_path, "config.yaml"), simulate=False)
    try:
        assert manager.history is not None
        assert manager.mqtt_factory is not None  # enables live broker changes
    finally:
        manager.history.close()


def test_parser_parses_run_and_bruteforce():
    parser = cli.build_parser()
    run = parser.parse_args(["run", "--simulate", "--port", "9000"])
    assert run.command == "run" and run.simulate is True and run.port == 9000
    bf = parser.parse_args(["bruteforce", "heating", "--length", "4"])
    assert bf.command == "bruteforce" and bf.meter == "heating"
