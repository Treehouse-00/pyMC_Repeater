"""Coverage for the stats the MQTT status message carries.

``_get_live_stats`` is what a LetsMesh-style observer reads on every heartbeat,
so the figures it reports have to be the node's own: both directions of airtime,
the default radio's noise floor rather than whichever radio sampled last, and a
receive-error count rather than a literal zero. The last tests cover the other
half of that -- publish_status must let those figures through, and on a bridge
each radio must report its own beside the node's.
"""

import json
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
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
        get_crc_error_count=lambda: 47,
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


def test_noise_floor_is_the_default_radios_not_the_newest_sample():
    # Every radio's sample lands in the table and the default is sampled first,
    # so the newest row on a Fabric node belongs to the other radio. Reading it
    # would report the backhaul's noise floor as the node's.
    collector = _make_collector()
    collector.repeater_handler = _handler(get_cached_noise_floor=lambda: -118.0)
    collector.sqlite_handler.get_noise_floor_history.return_value = [
        {"timestamp": 2.0, "noise_floor_dbm": -92.0, "radio_id": "link"}
    ]

    stats = collector._get_live_stats()

    assert stats["noise_floor"] == -118.0
    collector.sqlite_handler.get_noise_floor_history.assert_not_called()


def test_noise_floor_is_omitted_until_a_radio_has_been_sampled():
    collector = _make_collector()
    collector.repeater_handler = _handler(get_cached_noise_floor=lambda: None)

    assert "noise_floor" not in collector._get_live_stats()


def test_noise_floor_is_omitted_when_the_handler_cannot_report_one():
    collector = _make_collector()
    handler = _handler()
    del handler.get_cached_noise_floor

    collector.repeater_handler = handler

    assert "noise_floor" not in collector._get_live_stats()


def test_errors_reports_the_radios_crc_failures():
    # Published as a literal 0 for as long as the field existed, which made a
    # deaf node look like a quiet one.
    collector = _make_collector()
    collector.repeater_handler = _handler()

    assert collector._get_live_stats()["errors"] == 47


def test_errors_falls_back_to_zero_when_the_handler_cannot_count_them():
    collector = _make_collector()
    handler = _handler()
    del handler.get_crc_error_count

    collector.repeater_handler = handler

    assert collector._get_live_stats()["errors"] == 0


class _FakeIdentity:
    def __init__(self, public_key_hex: str):
        self._pk = bytes.fromhex(public_key_hex)

    def get_public_key(self) -> bytes:
        return self._pk


LOCAL_RADIO = {
    "id": "local",
    "radio_type": "sx1262",
    "radio": {
        "frequency": 869618000,
        "bandwidth": 62500,
        "spreading_factor": 8,
        "coding_rate": 8,
        "preamble_length": 32,
    },
}
LINK_RADIO = {
    "id": "link",
    "radio_type": "sx1262",
    "radio": {
        "frequency": 864200000,
        "bandwidth": 62500,
        "spreading_factor": 11,
        "coding_rate": 8,
        "preamble_length": 32,
    },
}


def _publish_status(stats_provider, radios=None, radio_stats_provider=None) -> dict:
    config = {
        "repeater": {"node_name": "test-node", "mode": "forward"},
        "radio": {
            "frequency": 869618000,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
        },
        "mqtt_brokers": {
            "iata_code": "LAX",
            "status_interval": 0,
            "brokers": [
                {
                    "name": "test-broker",
                    "enabled": True,
                    "host": "broker.example",
                    "port": 1883,
                    "transport": "tcp",
                    "format": "letsmesh",
                    "use_jwt_auth": False,
                    "tls": {"enabled": False, "insecure": False},
                }
            ],
        },
    }
    if radios:
        config["radios"] = radios
        config["fabric"] = {"tx_mode": "bridge", "default_radio": "local"}
    pusher = MeshCoreToMqttPusher(
        local_identity=_FakeIdentity("AB" * 32),
        config=config,
        stats_provider=stats_provider,
        radio_stats_provider=radio_stats_provider,
    )
    conn = pusher.connections[0]
    captured = []
    conn._running = True
    conn.client = MagicMock()
    conn.client.publish = lambda topic, payload, retain=False, qos=0: captured.append(payload)

    pusher.publish_status(state="online")

    assert len(captured) == 1
    return json.loads(captured[0])


def test_publish_status_does_not_overwrite_a_real_error_count():
    # The defaults used to be spread *after* live_stats, so a node that counted
    # errors still published 0.
    status = _publish_status(lambda: {"uptime_secs": 9, "errors": 47, "queue_len": 2})

    assert status["stats"]["errors"] == 47
    assert status["stats"]["queue_len"] == 2


def test_publish_status_still_defaults_the_fields_a_provider_omits():
    status = _publish_status(lambda: {"uptime_secs": 9})

    assert status["stats"]["errors"] == 0
    assert status["stats"]["queue_len"] == 0


PER_RADIO_AIRTIME = [
    {
        "radio_id": "local",
        "current_airtime_ms": 388.0,
        "max_airtime_ms": 3600,
        "utilization_percent": 10.8,
        "total_airtime_ms": 2_947_200.0,
        "total_rx_airtime_ms": 8_610_400.0,
    },
    {
        "radio_id": "link",
        "current_airtime_ms": 24.0,
        "max_airtime_ms": 3600,
        "utilization_percent": 0.7,
        "total_airtime_ms": 235_400.0,
        "total_rx_airtime_ms": 433_700.0,
    },
]


def _bridge_handler(**overrides) -> SimpleNamespace:
    per_radio = {
        "airtime_stats_by_radio": lambda: PER_RADIO_AIRTIME,
        "get_cached_noise_floor_by_radio": lambda: {"local": -118.5, "link": -126.0},
        "get_crc_error_count_by_radio": lambda: {"local": 2401, "link": 82},
    }
    return _handler(**{**per_radio, **overrides})


def test_radio_stats_carry_each_radios_own_figures():
    collector = _make_collector()
    collector.repeater_handler = _bridge_handler()

    assert collector._get_radio_stats() == {
        "local": {
            "tx_air_secs": 2947,
            "rx_air_secs": 8610,
            "current_airtime_ms": 388.0,
            "utilization_percent": 10.8,
            "noise_floor": -118.5,
            "errors": 2401,
        },
        "link": {
            "tx_air_secs": 235,
            "rx_air_secs": 433,
            "current_airtime_ms": 24.0,
            "utilization_percent": 0.7,
            "noise_floor": -126.0,
            "errors": 82,
        },
    }


def test_radio_stats_are_empty_on_a_single_radio_node():
    # Every figure in ``stats`` already describes that one radio.
    collector = _make_collector()
    collector.repeater_handler = _handler()

    assert collector._get_radio_stats() == {}


def test_radio_stats_report_the_sources_a_node_does_have():
    # A radio the fabric cannot hand back has no CRC count, which must not cost
    # the radios that do have one their airtime and noise floor.
    collector = _make_collector()
    collector.repeater_handler = _bridge_handler(
        get_crc_error_count_by_radio=lambda: {"local": 2401}
    )

    radios = collector._get_radio_stats()

    assert radios["local"]["errors"] == 2401
    assert "errors" not in radios["link"]
    assert radios["link"]["tx_air_secs"] == 235
    assert radios["link"]["noise_floor"] == -126.0


def test_one_unreadable_source_does_not_cost_the_others():
    def boom():
        raise RuntimeError("fabric went away")

    collector = _make_collector()
    collector.repeater_handler = _bridge_handler(get_cached_noise_floor_by_radio=boom)

    radios = collector._get_radio_stats()

    assert "noise_floor" not in radios["local"]
    assert radios["local"]["tx_air_secs"] == 2947
    assert radios["local"]["errors"] == 2401


def test_status_radio_map_carries_telemetry_beside_the_air_settings():
    status = _publish_status(
        lambda: {"uptime_secs": 9},
        radios=[LOCAL_RADIO, LINK_RADIO],
        radio_stats_provider=lambda: {
            "local": {"tx_air_secs": 2947, "noise_floor": -118.5, "errors": 2401},
            "link": {"tx_air_secs": 235, "noise_floor": -126.0, "errors": 82},
        },
    )

    assert status["radios"] == [
        {
            "id": "local",
            "radio": "869.618,62.5,8,8",
            "tx_air_secs": 2947,
            "noise_floor": -118.5,
            "errors": 2401,
        },
        {
            "id": "link",
            "radio": "864.2,62.5,11,8",
            "tx_air_secs": 235,
            "noise_floor": -126.0,
            "errors": 82,
        },
    ]


def test_a_radio_with_no_telemetry_still_attributes_its_packets():
    # The map's first job is saying which band an id is. Telemetry is merged in,
    # never a precondition for the entry.
    status = _publish_status(
        lambda: {"uptime_secs": 9},
        radios=[LOCAL_RADIO, LINK_RADIO],
        radio_stats_provider=lambda: {"local": {"errors": 2401}},
    )

    assert status["radios"] == [
        {"id": "local", "radio": "869.618,62.5,8,8", "errors": 2401},
        {"id": "link", "radio": "864.2,62.5,11,8"},
    ]


def test_an_unreadable_provider_leaves_the_map_intact():
    def boom():
        raise RuntimeError("no handler")

    status = _publish_status(
        lambda: {"uptime_secs": 9}, radios=[LOCAL_RADIO, LINK_RADIO], radio_stats_provider=boom
    )

    assert status["radios"] == [
        {"id": "local", "radio": "869.618,62.5,8,8"},
        {"id": "link", "radio": "864.2,62.5,11,8"},
    ]


def test_a_single_radio_node_publishes_no_radio_map():
    status = _publish_status(
        lambda: {"uptime_secs": 9},
        radio_stats_provider=lambda: {"local": {"errors": 2401}},
    )

    assert "radios" not in status
