"""Low-overhead Linux physical-network counter monitoring.

This module never changes interface settings.  It records kernel counters so
transport retries cannot hide a host NIC that is continuously dropping data.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
from typing import Any


COUNTERS = ("rx_dropped", "rx_errors", "rx_fifo_errors", "rx_missed_errors")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def read_physical_interface_counters(
    sys_class_net: Path = Path("/sys/class/net"),
) -> dict[str, dict[str, int]]:
    """Read available RX counters for active Linux physical interfaces."""

    if not sys.platform.startswith("linux") or not sys_class_net.is_dir():
        return {}
    result: dict[str, dict[str, int]] = {}
    for interface in sorted(sys_class_net.iterdir(), key=lambda path: path.name):
        # Physical PCI/USB interfaces expose a device symlink.  Excluding TUN,
        # loopback and bridges keeps warnings focused on the actual host link.
        if not (interface / "device").exists():
            continue
        try:
            state = (interface / "operstate").read_text(encoding="ascii").strip()
        except OSError:
            state = "unknown"
        if state not in {"up", "unknown"}:
            continue
        values: dict[str, int] = {}
        for name in COUNTERS:
            value = _read_int(interface / "statistics" / name)
            if value is not None:
                values[name] = value
        if values:
            result[interface.name] = values
    return result


class NetworkHealthMonitor:
    """Track physical RX-counter deltas without requiring root privileges."""

    def __init__(self, reader=read_physical_interface_counters):
        self._reader = reader
        self._lock = threading.Lock()
        self._baseline = reader()
        self._last_warned: dict[str, dict[str, int]] = {}

    def report(self) -> dict[str, Any]:
        with self._lock:
            current = self._reader()
            delta: dict[str, dict[str, int]] = {}
            for interface, values in current.items():
                baseline = self._baseline.get(interface, {})
                changes = {
                    name: max(0, value - int(baseline.get(name, value)))
                    for name, value in values.items()
                }
                delta[interface] = changes
            unhealthy = any(
                value > 0
                for changes in delta.values()
                for value in changes.values()
            )
            return {
                "supported": bool(self._baseline or current),
                "baseline": self._baseline,
                "current": current,
                "delta": delta,
                "rx_counters_increased": unhealthy,
            }

    def consume_warning(self) -> dict[str, Any] | None:
        """Return only newly increased counter deltas since the last warning."""

        report = self.report()
        if not report["rx_counters_increased"]:
            return None
        with self._lock:
            newly_increased: dict[str, dict[str, int]] = {}
            for interface, changes in report["delta"].items():
                previous = self._last_warned.get(interface, {})
                new_values = {
                    name: value
                    for name, value in changes.items()
                    if value > int(previous.get(name, 0))
                }
                if new_values:
                    newly_increased[interface] = new_values
            if not newly_increased:
                return None
            self._last_warned = {
                interface: dict(changes)
                for interface, changes in report["delta"].items()
            }
        return {
            "interfaces": newly_increased,
            "message": (
                "Physical-interface RX error/drop counters increased during the run; "
                "retries preserve progress but do not repair the host network path."
            ),
        }
