"""Stable serial device selection.

Kernel names like ``/dev/ttyUSB0`` are handed out in plug-in order, so after a
reboot, an unplug or behind a USB hub two IR readers can swap names and each
meter's values end up on the other meter's topics.  A meter's ``port`` can
therefore pin the *adapter* instead of the kernel name:

``usb:serial=A50285BI``
    the adapter's USB serial number (FTDI, CP210x and most branded IR heads
    have a unique one)
``usb:location=1-1.2``
    the physical USB port the adapter is plugged into (for cheap CH340 heads
    without a serial number); stays the same as long as the head stays in the
    same socket of the same hub
``usb:vid=10c4,pid=ea60``
    vendor/product id; only unique if a single adapter of that model is present

Keys can be combined (``usb:vid=0403,pid=6001,serial=A50285BI``); all of them
must match.  The selector is resolved to the current ``/dev/tty*`` name every
time the port is opened, so a reconnect after an unplug finds the reader again
under its new name.  Zero or several matches are errors, never a guess.

``/dev/serial/by-id/...`` and ``/dev/serial/by-path/...`` symlinks (created by
udev) are equally stable and work as plain paths; see :func:`list_devices` for
what is available on this host.
"""

from __future__ import annotations

import glob
import os
import re

SELECTOR_PREFIX = "usb:"
SELECTOR_KEYS = ("serial", "vid", "pid", "location")

# Kernel-assigned names whose number depends on plug-in order.
_UNSTABLE_RE = re.compile(r"^/dev/tty(USB|ACM|AMA|S|XRUSB)\d+$")
_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]{1,4}$")


class DeviceSelectorError(ValueError):
    """A ``usb:`` selector is malformed or matches no / several devices."""


def is_selector(port: str) -> bool:
    return port.strip().lower().startswith(SELECTOR_PREFIX)


def is_unstable_path(port: str) -> bool:
    """True for kernel names (``/dev/ttyUSB0``) that may change between boots."""
    return bool(_UNSTABLE_RE.match(port.strip()))


def parse_selector(port: str) -> dict[str, str | int]:
    """Parse ``usb:key=value,...`` into a dict; raise on anything unexpected."""
    if not is_selector(port):
        raise DeviceSelectorError(f"not a usb: selector: {port!r}")
    body = port.strip()[len(SELECTOR_PREFIX) :]
    if not body.strip():
        raise DeviceSelectorError(
            "empty usb: selector; use e.g. usb:serial=<serial> or usb:location=<usb path>"
        )
    result: dict[str, str | int] = {}
    for part in body.split(","):
        key, sep, value = part.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or not key or not value:
            raise DeviceSelectorError(
                f"expected key=value in usb: selector, got {part.strip()!r}"
            )
        if key not in SELECTOR_KEYS:
            raise DeviceSelectorError(
                f"unknown usb: selector key {key!r} (allowed: {', '.join(SELECTOR_KEYS)})"
            )
        if key in result:
            raise DeviceSelectorError(f"duplicate usb: selector key {key!r}")
        if key in ("vid", "pid"):
            if not _HEX_RE.match(value):
                raise DeviceSelectorError(
                    f"{key} must be a 4-digit hex id, got {value!r}"
                )
            result[key] = int(value, 16)
        else:
            result[key] = value
    return result


def validate_port(port: str) -> None:
    """Raise :class:`DeviceSelectorError` if *port* is a malformed selector."""
    if is_selector(port):
        parse_selector(port)


def _location_matches(actual: str | None, wanted: str) -> bool:
    if not actual:
        return False
    # pyserial reports "1-1.2:1.0" (bus-ports:config.interface); accept the
    # port path alone, which is what users read off `lsusb -t` / by-path.
    return actual == wanted or actual.split(":", 1)[0] == wanted


def _matches(info, criteria: dict) -> bool:
    checks = {
        "serial": lambda want: (info.serial_number or "").strip() == want,
        "vid": lambda want: info.vid == want,
        "pid": lambda want: info.pid == want,
        "location": lambda want: _location_matches(info.location, str(want)),
    }
    return all(checks[key](want) for key, want in criteria.items())


def _comports() -> list:
    from serial.tools import list_ports  # lazy: pyserial only needed for hardware

    return list(list_ports.comports())


def resolve_port(port: str, ports: list | None = None) -> str:
    """Return the device path to open for *port*.

    Plain paths and URLs are returned unchanged.  A ``usb:`` selector is matched
    against the currently attached serial adapters (*ports* defaults to
    ``serial.tools.list_ports.comports()``) and must match exactly one.
    """
    if not is_selector(port):
        return port
    criteria = parse_selector(port)
    candidates = _comports() if ports is None else ports
    found = [p for p in candidates if _matches(p, criteria)]
    if not found:
        raise DeviceSelectorError(
            f"no serial device matches {port!r} (is the reader plugged in?)"
        )
    if len(found) > 1:
        names = ", ".join(sorted(p.device for p in found))
        raise DeviceSelectorError(
            f"{port!r} is ambiguous, it matches {names}; add serial= or location= to pin one"
        )
    return found[0].device


def _links(directory: str) -> dict[str, list[str]]:
    """Map resolved device path -> symlinks in *directory* pointing at it."""
    result: dict[str, list[str]] = {}
    for link in sorted(glob.glob(os.path.join(directory, "*"))):
        try:
            target = os.path.realpath(link)
        except OSError:  # pragma: no cover - racing unplug
            continue
        result.setdefault(target, []).append(link)
    return result


def _hex(value: int | None) -> str | None:
    return f"{value:04x}" if value is not None else None


def stable_selector(info, all_ports: list) -> str | None:
    """Suggest the most specific selector that matches only *info*.

    Prefers the serial number (follows the reader to any socket), falls back to
    the USB location (for adapters without a unique serial). ``None`` for
    non-USB ports.
    """
    if info.vid is None:
        return None
    serial = (info.serial_number or "").strip()
    if serial:
        sel = f"usb:serial={serial}"
        if sum(1 for p in all_ports if _matches(p, {"serial": serial})) == 1:
            return sel
        sel = f"usb:vid={_hex(info.vid)},pid={_hex(info.pid)},serial={serial}"
        crit = parse_selector(sel)
        if sum(1 for p in all_ports if _matches(p, crit)) == 1:
            return sel
    if info.location:
        sel = f"usb:location={info.location.split(':', 1)[0]}"
        crit = parse_selector(sel)
        if sum(1 for p in all_ports if _matches(p, crit)) == 1:
            return sel
        sel = f"usb:location={info.location}"
        if sum(1 for p in all_ports if _matches(p, {"location": info.location})) == 1:
            return sel
    return None


def list_devices(
    ports: list | None = None,
    by_id_dir: str = "/dev/serial/by-id",
    by_path_dir: str = "/dev/serial/by-path",
) -> list[dict]:
    """Describe attached serial adapters with every stable way to address them."""
    try:
        infos = _comports() if ports is None else list(ports)
    except Exception:  # noqa: BLE001 - no pyserial / no sysfs: nothing to list
        return []
    by_id, by_path = _links(by_id_dir), _links(by_path_dir)
    result = []
    for info in sorted(infos, key=lambda p: p.device):
        real = os.path.realpath(info.device)
        result.append(
            {
                "device": info.device,
                "description": info.description,
                "manufacturer": getattr(info, "manufacturer", None),
                "product": getattr(info, "product", None),
                "serial_number": info.serial_number,
                "vid": _hex(info.vid),
                "pid": _hex(info.pid),
                "location": info.location,
                "by_id": by_id.get(real, []),
                "by_path": by_path.get(real, []),
                "selector": stable_selector(info, infos),
            }
        )
    return result


def reader_key(port: str, ports: list | None = None) -> tuple | None:
    """Identify the physical reader *port* refers to (to find shared readers).

    Two ports with the same key are the same reader: a ``usb:`` selector, a
    ``/dev/serial/by-id`` symlink and the ``/dev/ttyUSB*`` name of one adapter
    all resolve to the same device.  A selector that matches nothing right now
    (reader unplugged) is compared by its parsed criteria instead.  Empty ports
    return ``None`` and are never shared.
    """
    port = (port or "").strip()
    if not port:
        return None
    if "://" in port:
        return ("url", port.lower())
    if is_selector(port):
        try:
            criteria = parse_selector(port)
        except DeviceSelectorError:
            return ("raw", port)
        try:
            return ("dev", os.path.realpath(resolve_port(port, ports)))
        except DeviceSelectorError:
            return ("selector", tuple(sorted(criteria.items())))
    return ("dev", os.path.realpath(port))


def find_shared_readers(meters, ports: list | None = None) -> dict[str, list[str]]:
    """Map meter id -> ids of other *enabled* meters using the same reader.

    *meters* are objects with ``id``, ``port`` and ``enabled``.  Disabled
    meters are ignored (they keep a spare configuration, nothing opens it).
    Adapters are enumerated once (*ports* defaults to the attached ones) so
    selectors and paths are compared against the same snapshot.
    """
    enabled = [m for m in meters if m.enabled]
    if ports is None and any(is_selector(m.port or "") for m in enabled):
        try:
            ports = _comports()
        except Exception:  # noqa: BLE001 - no pyserial/sysfs: compare criteria only
            ports = []
    groups: dict[tuple, list[str]] = {}
    for meter in enabled:
        key = reader_key(meter.port, ports or [])
        if key is not None:
            groups.setdefault(key, []).append(meter.id)
    conflicts: dict[str, list[str]] = {}
    for ids in groups.values():
        if len(ids) > 1:
            for meter_id in ids:
                conflicts[meter_id] = [other for other in ids if other != meter_id]
    return conflicts
