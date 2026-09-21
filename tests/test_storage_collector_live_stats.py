"""Coverage for the stats the MQTT status message carries.

``_get_live_stats`` is what a LetsMesh-style observer reads on every heartbeat,
so the figures it reports have to be the node's own.
"""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from repeater.data_acquisition.storage_collector import StorageCollector

sys.modules.setdefault("psutil", types.ModuleType("psutil"))

nacl_module = types.ModuleType("nacl")
nacl_signing_module = types.ModuleType("nacl.signing")


class _SigningKeyStub:
    pass


nacl_signing_module.SigningKey = _SigningKeyStub
nacl_module.signing = nacl_signing_module

sys.modules.setdefault("nacl", nacl_module)
sys.modules.setdefault("nacl.signing", nacl_signing_module)


def _make_collector() -> StorageCollector:
    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch("repeater.data_acquisition.storage_collector.RRDToolHandler"),
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(
            config={"storage": {"storage_dir": "/tmp/openhop_repeater_test"}}
        )

    collector._stats_stop_event.set()
    if collector._stats_thread is not None:
        collector._stats_thread.join(timeout=1)
    collector._stats_stop_event = threading.Event()
    collector._stats_thread = None

    collector.sqlite_handler = MagicMock()
    return collector


def _handler(**overrides) -> SimpleNamespace:
    handler = SimpleNamespace(
        start_time=0.0,
        forwarded_count=3,
        rx_count=11,
        airtime_stats=lambda: {
            "current_airtime_ms": 120.0,
            "max_airtime_ms": 3600,
            "utilization_percent": 3.3,
            "total_airtime_ms": 61_500.0,
            "total_rx_airtime_ms": 240_900.0,
        },
        get_cached_noise_floor=lambda: -118.0,
    )
    for key, value in overrides.items():
        setattr(handler, key, value)
    return handler


def test_reports_both_directions_of_airtime_in_whole_seconds():
    # An observer derives channel utilisation from the change in tx+rx airtime
    # between heartbeats, so RX has to be there; firmware counts both in whole
    # seconds and the other two stats paths already match that.
    collector = _make_collector()
    collector.repeater_handler = _handler()

    stats = collector._get_live_stats()

    assert stats["tx_air_secs"] == 61
    assert stats["rx_air_secs"] == 240
    assert isinstance(stats["tx_air_secs"], int)
    assert isinstance(stats["rx_air_secs"], int)


def test_airtime_survives_a_handler_that_reports_no_rx():
    collector = _make_collector()
    collector.repeater_handler = _handler(
        airtime_stats=lambda: {
            "current_airtime_ms": 0.0,
            "utilization_percent": 0.0,
            "total_airtime_ms": 2_000.0,
        }
    )

    stats = collector._get_live_stats()

    assert stats["tx_air_secs"] == 2
    assert stats["rx_air_secs"] == 0
