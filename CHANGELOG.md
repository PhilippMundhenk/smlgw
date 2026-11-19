# Changelog

All notable changes to smlgw are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-11-19

### Added
- Pin a meter to its reader instead of a kernel name that can change between
  boots: `port: usb:serial=…`, `usb:location=…` (USB socket) or
  `usb:vid=…,pid=…`, combinable, re-resolved on every reconnect. No match or
  several matches is an error naming the candidates, never a guess. (#1)
- `/dev/serial/by-id/…` and `/dev/serial/by-path/…` paths are suggested and
  documented as stable alternatives.
- Web UI: a *Detected serial adapters* table (serial number, VID:PID, USB
  location, suggested stable port, *use* button) on the add-meter form and the
  meter page; meters on `/dev/ttyUSB0`-style names are marked **⚠ unstable**;
  the device actually opened and the last error are shown.
- `smlgw ports`: lists serial adapters with their stable port value and
  resolves every configured meter (exit code 1 if one does not resolve).
- Several meters on one reader (e.g. two topic sets or units for one meter):
  allowed after a confirmation; at runtime they share one open port and each
  receives every frame. Marked **⇄ shares reader with …** in the UI and noted
  by `smlgw ports`. (#3, #5)
- API: `/api/ports` returns serial number, VID/PID, USB location, by-id/by-path
  links and a suggested `selector`; `/api/status` adds `device`,
  `unstable_port` and `shared_with`; `/api/meters/{id}/discovered` adds `device`
  and `last_error`; meter create/update accept `allow_shared_reader`; config
  import returns `warnings`.
- Docs: Docker options for stable readers (map by id, or pass all readers / bind
  `/dev` for hot-plug); field tables for mappings and dashboard series;
  `GET /api/obis/{code}`.

### Changed
- Local serial ports are opened exclusively (a lock on Linux/macOS), so another
  program cannot read the same reader at the same time. `smlgw bruteforce` for
  a reader the running service uses now fails with "already in use"; stop the
  service first or use the PIN tools in the web UI.
- Malformed `usb:` selectors are rejected when a meter is saved (`400`) or a
  configuration is imported.
- CI: the Python 3.9 job runs on Ubuntu 24.04 (`ubuntu-latest`, now 26.04, has
  no Python 3.9); a consistency check of docs, changelog and screenshots runs
  on every push and before image releases.

## [1.0.0] - 2026-07-24

### Added
- First release: SML/OBIS smart meter to MQTT gateway with a web UI —
  per-meter workers with reconnect, auto-discovery, OBIS→topic mappings with
  per-value output units, dashboard (line/stat/gauge panels) with SQLite
  history, optional UI password, config backup/restore, PIN entry and
  bruteforce, networked meters via pyserial URLs (`socket://`, `rfc2217://`),
  byte-for-byte compatibility with the legacy gateway's MQTT output.
- Docker image on GHCR (amd64/arm64), docker compose and systemd deployment;
  pure-Python dependencies for Python 3.9+ on ARMv6 boards.

[Unreleased]: https://github.com/PhilippMundhenk/smlgw/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/PhilippMundhenk/smlgw/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/PhilippMundhenk/smlgw/releases/tag/v1.0.0
