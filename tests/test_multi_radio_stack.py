"""Tests for optional multi-radio fabric stack in the repeater."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from repeater.config import (
    NullRadio,
    _apply_fabric_tx_mode,
    _describe_radio_config,
    _merge_radio_entry,
    build_metering_profiles,
    build_radio_stack,
    fabric_selects_by_ingress_radio,
)


def _core_selects_by_ingress() -> bool:
    from openhop_core.rf_fabric import RFFabric

    return fabric_selects_by_ingress_radio(RFFabric())


needs_ingress_core = pytest.mark.skipif(
    not _core_selects_by_ingress(),
    reason="installed openhop_core does not pass a packet's ingress radio to the TX selector",
)


class _FakeRadio:
    def __init__(self, name="r"):
        self.name = name
        self.rx_callback = None
        self.sent = []
        self.frequency = 869618000
        self.spreading_factor = 8
        self.bandwidth = 62500
        self.coding_rate = 8
        self.tx_power = 14

    def set_rx_callback(self, cb):
        self.rx_callback = cb

    async def send(self, data: bytes):
        self.sent.append(data)
        return {"ok": True, "name": self.name}

    def get_frequency(self):
        return self.frequency / 1e6

    def get_spreading_factor(self):
        return self.spreading_factor

    def get_bandwidth(self):
        return self.bandwidth / 1e3

    def get_coding_rate(self):
        return self.coding_rate

    def get_tx_power(self):
        return self.tx_power

    def get_last_rssi(self):
        return -80

    def get_last_snr(self):
        return 5.0


def test_merge_radio_entry_overlays_radio_sections():
    """A partial entry section inherits the top-level keys it omits."""
    global_cfg = {
        "radio_type": "sx1262",
        "radio": {
            "frequency": 869618000,
            "tx_power": 10,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
        },
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "use_dio3_tcxo": True,
            "dio3_tcxo_voltage": 1.8,
            "use_dio2_rf": True,
            "en_pins": [12, 13],
        },
    }
    entry = {
        "id": "local",
        "radio": {
            "frequency": 910525000,
            "tx_power": 22,
        },
        "sx1262": {
            "cs_id": 1,
            "reset_pin": 24,
        },
    }

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["_radio_id"] == "local"
    assert merged["radio_type"] == "sx1262"

    assert merged["radio"]["frequency"] == 910525000
    assert merged["radio"]["tx_power"] == 22
    assert merged["radio"]["bandwidth"] == 62500
    assert merged["radio"]["spreading_factor"] == 8
    assert merged["radio"]["coding_rate"] == 8

    assert merged["sx1262"]["cs_id"] == 1
    assert merged["sx1262"]["reset_pin"] == 24
    assert merged["sx1262"]["bus_id"] == 0
    assert merged["sx1262"]["use_dio3_tcxo"] is True
    assert merged["sx1262"]["dio3_tcxo_voltage"] == 1.8
    assert merged["sx1262"]["use_dio2_rf"] is True
    assert merged["sx1262"]["en_pins"] == [12, 13]


def test_merge_radio_entry_inherits_sx1262_tcxo_and_power_settings():
    """RAK6421/RAK1330x regression: pin-only entry keeps TCXO/RF-switch setup."""
    global_cfg = {
        "radio_type": "sx1262",
        "radio": {
            "frequency": 910525000,
            "tx_power": 22,
        },
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": -1,
            "reset_pin": 16,
            "busy_pin": 24,
            "irq_pin": 22,
            "txen_pin": -1,
            "rxen_pin": -1,
            "en_pins": [12, 13],
            "use_dio2_rf": True,
            "use_dio3_tcxo": True,
            "dio3_tcxo_voltage": 1.8,
        },
    }
    entry = {
        "id": "local",
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": -1,
            "reset_pin": 16,
            "busy_pin": 24,
            "irq_pin": 22,
            "txen_pin": -1,
            "rxen_pin": -1,
        },
    }

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["sx1262"]["use_dio3_tcxo"] is True
    assert merged["sx1262"]["dio3_tcxo_voltage"] == 1.8
    assert merged["sx1262"]["use_dio2_rf"] is True
    assert merged["sx1262"]["en_pins"] == [12, 13]


def test_merge_radio_entry_explicit_false_overrides_inherited_true():
    """Presence, not truthiness, decides whether an entry value overrides."""
    global_cfg = {
        "radio_type": "sx1262",
        "sx1262": {
            "use_dio3_tcxo": True,
            "use_dio2_rf": True,
            "is_waveshare": True,
        },
    }
    entry = {
        "id": "link",
        "sx1262": {
            "use_dio3_tcxo": False,
            "use_dio2_rf": False,
            "is_waveshare": False,
        },
    }

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["sx1262"]["use_dio3_tcxo"] is False
    assert merged["sx1262"]["use_dio2_rf"] is False
    assert merged["sx1262"]["is_waveshare"] is False


def test_merge_radio_entry_can_override_radio_type():
    global_cfg = {
        "radio_type": "sx1262",
        "radio": {
            "frequency": 910525000,
            "bandwidth": 62500,
        },
        "sx1262": {
            "bus_id": 0,
        },
    }
    entry = {
        "id": "backhaul",
        "radio_type": "modem_usb",
        "modem_usb": {
            "port": "/dev/ttyACM0",
        },
    }

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["radio_type"] == "modem_usb"
    assert merged["modem_usb"]["port"] == "/dev/ttyACM0"
    assert merged["radio"]["frequency"] == 910525000
    assert "sx1262" in merged  # inherited leftover ok; factory uses radio_type


def test_merge_radio_entry_non_mapping_section_replaces_base():
    global_cfg = {
        "radio_type": "sx1262",
        "sx1262": {"use_dio3_tcxo": True},
    }
    entry = {
        "id": "local",
        "sx1262": None,
    }

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["sx1262"] is None


def test_build_radio_stack_legacy_single():
    cfg = {"radio_type": "none"}
    radio, meta = build_radio_stack(cfg)
    assert isinstance(radio, NullRadio)
    assert meta["mode"] == "single"
    assert meta["fabric"] is False


def test_build_radio_stack_multi_wraps_fabric():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        # build_radio_stack pops _radio_id before calling get_radio_for_board
        # so distinguish by radio air settings
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "sticky"},
        "radios": [
            {
                "id": "local",
                "radio_type": "sx1262",
                "radio": {"frequency": 111},
                "sx1262": {"bus_id": 0},
            },
            {
                "id": "link",
                "radio_type": "sx1262",
                "radio": {"frequency": 222},
                "sx1262": {"bus_id": 0},
            },
        ],
    }

    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    assert meta["fabric"] is True
    assert meta["mode"] == "multi"
    assert meta["radio_ids"] == ["local", "link"]
    assert meta["default_radio"] == "local"
    # FabricRadio surface
    assert hasattr(radio, "fabric")
    assert list(radio.fabric.radios.keys()) == ["local", "link"]
    assert radio.fabric.default_radio_id == "local"
    # sticky selector installed
    assert radio.fabric._tx_selector is not None


@needs_ingress_core
@pytest.mark.asyncio
async def test_sticky_tx_uses_the_packets_own_ingress_radio():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "sticky"},
        "radios": [
            {"id": "local", "radio_type": "sx1262", "radio": {"frequency": 111}, "sx1262": {}},
            {"id": "link", "radio_type": "sx1262", "radio": {"frequency": 222}, "sx1262": {}},
        ],
    }
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    # A packet heard on link replies on link.
    b.rx_callback(b"hello", -70, 3.0)
    await radio.send(b"reply", rx_radio_id="link")
    assert b.sent == [b"reply"]
    assert a.sent == []

    # Nothing this node originated arrived anywhere, so it leaves by the default
    # radio. It used to leave by whichever radio last heard anything, which is
    # not a property of the packet being sent.
    await radio.send(b"advert")
    assert a.sent == [b"advert"]
    assert b.sent == [b"reply"]

    # And another node's traffic arriving in between does not move it.
    a.rx_callback(b"noise", -70, 3.0)
    await radio.send(b"reply-2", rx_radio_id="link")
    assert b.sent == [b"reply", b"reply-2"]


def test_use_fabric_single_radio():
    fake = _FakeRadio("solo")
    cfg = {"radio_type": "sx1262", "fabric": {"use_fabric": True, "tx_mode": "default"}}
    with patch("repeater.config.get_radio_for_board", return_value=fake):
        radio, meta = build_radio_stack(cfg)
    assert meta["fabric"] is True
    assert meta["mode"] == "single_fabric"
    assert hasattr(radio, "fabric")


def test_tx_mode_all_rejected():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "all"},
        "radios": [
            {"id": "local", "radio_type": "sx1262", "radio": {"frequency": 111}, "sx1262": {}},
            {"id": "link", "radio_type": "sx1262", "radio": {"frequency": 222}, "sx1262": {}},
        ],
    }
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        with pytest.raises(ValueError, match="Unknown fabric.tx_mode"):
            build_radio_stack(cfg)


@needs_ingress_core
@pytest.mark.asyncio
async def test_bridge_tx_crosses_to_other_radio():
    """RX on local -> TX on link; RX on link -> TX on local."""
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "bridge"},
        "radios": [
            {"id": "local", "radio_type": "sx1262", "radio": {"frequency": 111}, "sx1262": {}},
            {"id": "link", "radio_type": "sx1262", "radio": {"frequency": 222}, "sx1262": {}},
        ],
    }
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    assert meta["tx_mode"] == "bridge"

    # Heard on local neighborhood -> forward out link backhaul
    a.rx_callback(b"from-local", -70, 3.0)
    await radio.send(b"fwd-1", rx_radio_id="local")
    assert a.sent == []
    assert b.sent == [b"fwd-1"]

    # Heard on link backhaul -> forward out local
    b.rx_callback(b"from-link", -80, 2.0)
    await radio.send(b"fwd-2", rx_radio_id="link")
    assert a.sent == [b"fwd-2"]
    assert b.sent == [b"fwd-1"]

    # A locally originated packet has no side of the bridge to come from, so it
    # goes out on the default radio rather than on whatever the node last heard.
    await radio.send(b"advert")
    assert a.sent == [b"fwd-2", b"advert"]
    assert b.sent == [b"fwd-1"]


def _fanout_cfg(fabric: dict, radio_ids=("local", "link")) -> dict:
    return {
        "fabric": {"default_radio": radio_ids[0], **fabric},
        "radios": [
            {"id": rid, "radio_type": "sx1262", "radio": {"frequency": 100 + i}, "sx1262": {}}
            for i, rid in enumerate(radio_ids)
        ],
    }


def _build_fanout(cfg):
    """build_radio_stack with fake hardware; returns (radio, meta, factory_mock)."""
    radios = {}

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return radios.setdefault(freq, _FakeRadio(str(freq)))

    with patch("repeater.config.get_radio_for_board", side_effect=fake_get) as factory:
        radio, meta = build_radio_stack(cfg)
    return radio, meta, factory


def test_bridge_without_fanout_options_keeps_defaults():
    _, meta, _ = _build_fanout(_fanout_cfg({"tx_mode": "bridge"}))
    assert meta["tx_mode"] == "bridge"
    assert meta["repeat_on_ingress"] is False
    assert meta["origin_tx"] == "default"


def test_fanout_option_defaults_without_fabric_section():
    with patch("repeater.config.get_radio_for_board", return_value=_FakeRadio()):
        _, meta = build_radio_stack({"radio_type": "sx1262"})
    assert meta["repeat_on_ingress"] is False
    assert meta["origin_tx"] == "default"


def test_repeat_on_ingress_accepted_with_bridge_and_two_radios():
    _, meta, _ = _build_fanout(_fanout_cfg({"tx_mode": "bridge", "repeat_on_ingress": True}))
    assert meta["repeat_on_ingress"] is True
    assert meta["radio_ids"] == ["local", "link"]


@pytest.mark.parametrize(
    "cfg",
    [
        # one radio in a radios: list
        _fanout_cfg({"tx_mode": "bridge", "repeat_on_ingress": True}, radio_ids=("local",)),
        # use_fabric around a single radio
        {
            "radio_type": "sx1262",
            "fabric": {"use_fabric": True, "tx_mode": "bridge", "repeat_on_ingress": True},
        },
        # legacy single radio, no fabric at all
        {"radio_type": "sx1262", "fabric": {"tx_mode": "bridge", "repeat_on_ingress": True}},
    ],
    ids=["radios-list", "use_fabric", "legacy"],
)
def test_repeat_on_ingress_rejected_with_one_radio(cfg):
    with patch("repeater.config.get_radio_for_board", return_value=_FakeRadio()) as factory:
        with pytest.raises(ValueError, match="exactly two radios"):
            build_radio_stack(cfg)
    factory.assert_not_called()  # rejected before any hardware is opened


def test_repeat_on_ingress_rejected_with_three_radios():
    cfg = _fanout_cfg({"tx_mode": "bridge", "repeat_on_ingress": True}, radio_ids=("a", "b", "c"))
    with pytest.raises(ValueError, match="exactly two radios"):
        _build_fanout(cfg)


@pytest.mark.parametrize("tx_mode", ["sticky", "default"])
def test_repeat_on_ingress_rejected_without_bridge(tx_mode):
    cfg = _fanout_cfg({"tx_mode": tx_mode, "repeat_on_ingress": True})
    with pytest.raises(ValueError, match="requires fabric.tx_mode=bridge"):
        _build_fanout(cfg)


def test_repeat_on_ingress_rejects_non_boolean():
    cfg = _fanout_cfg({"tx_mode": "bridge", "repeat_on_ingress": "sometimes"})
    with pytest.raises(ValueError, match="repeat_on_ingress must be true or false"):
        _build_fanout(cfg)


@pytest.mark.parametrize("tx_mode", ["default", "sticky", "bridge"])
def test_origin_tx_all_accepted_with_two_radios(tx_mode):
    _, meta, _ = _build_fanout(_fanout_cfg({"tx_mode": tx_mode, "origin_tx": "ALL"}))
    assert meta["origin_tx"] == "all"


@pytest.mark.parametrize("key", ["origin_tx", "local_tx_mode"])
def test_origin_tx_all_rejected_with_one_radio(key):
    cfg = _fanout_cfg({key: "all"}, radio_ids=("local",))
    with pytest.raises(ValueError, match=f"{key}=all requires exactly two radios"):
        _build_fanout(cfg)


@pytest.mark.parametrize("key", ["origin_tx", "local_tx_mode"])
@pytest.mark.parametrize("value", ["both", "multicast", 3])
def test_invalid_origin_tx_rejected(key, value):
    cfg = _fanout_cfg({"tx_mode": "bridge", key: value})
    with pytest.raises(ValueError, match=f"Unknown fabric.{key}"):
        _build_fanout(cfg)


def test_origin_tx_accepts_its_former_name():
    _, meta, _ = _build_fanout(_fanout_cfg({"local_tx_mode": "all"}))
    assert meta["origin_tx"] == "all"
    assert "local_tx_mode" not in meta


def test_origin_tx_and_former_name_may_agree():
    _, meta, _ = _build_fanout(_fanout_cfg({"origin_tx": "all", "local_tx_mode": " All "}))
    assert meta["origin_tx"] == "all"


def test_origin_tx_conflicting_with_former_name_rejected():
    cfg = _fanout_cfg({"origin_tx": "default", "local_tx_mode": "all"})
    with patch("repeater.config.get_radio_for_board") as factory:
        with pytest.raises(ValueError, match="conflicts with fabric.local_tx_mode"):
            build_radio_stack(cfg)
    factory.assert_not_called()


def test_merge_radio_entry_overlays_per_radio_ch341_selection():
    """Shared adapter parameters stay global; only device identity differs."""
    global_cfg = {
        "radio_type": "sx1262_ch341",
        "ch341": {
            "vid": 0x1A86,
            "pid": 0x5512,
            "bus": 1,
            "address": 5,
        },
        "radio": {"frequency": 869618000},
        "sx1262": {"bus_id": 0},
    }
    entry = {
        "id": "link",
        "ch341": {
            "address": 8,
        },
        "radio": {
            "frequency": 864200000,
        },
    }

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["ch341"]["vid"] == 0x1A86
    assert merged["ch341"]["pid"] == 0x5512
    assert merged["ch341"]["bus"] == 1
    assert merged["ch341"]["address"] == 8

    assert merged["radio"]["frequency"] == 864200000

    assert merged["_ch341_per_instance"] is True


class _LegacyFabric:
    """A fabric from an openhop-core that predates the ingress-radio argument.

    The only difference that matters is the signature of resolve_tx_radio_id,
    which is how the repeater tells the two apart.
    """

    def __init__(self):
        self.radios = {"local": object(), "link": object()}
        self.default_radio_id = "local"
        self._last_rx_radio_id = None
        self.selector = None

    def resolve_tx_radio_id(self, data, radio_id=None):
        return radio_id

    def set_tx_selector(self, selector):
        self.selector = selector


def test_an_older_core_is_given_the_one_argument_selector_it_can_call():
    """The fallback has to be callable by the core that gets it, or the node
    raises TypeError on its first transmission."""
    fabric = _LegacyFabric()
    assert fabric_selects_by_ingress_radio(fabric) is False

    _apply_fabric_tx_mode(fabric, "sticky")
    fabric._last_rx_radio_id = "link"

    assert fabric.selector(b"frame") == "link"
    with pytest.raises(TypeError):
        fabric.selector(b"frame", "local")


def test_an_older_core_keeps_the_behaviour_it_always_had():
    """It reads the node's most recent RX, including for locally originated
    traffic. Unchanged, deliberately: approximating it would move packets on the
    air on nodes that did not update their core."""
    fabric = _LegacyFabric()
    _apply_fabric_tx_mode(fabric, "bridge")

    fabric._last_rx_radio_id = "local"
    assert fabric.selector(b"frame") == "link"
    fabric._last_rx_radio_id = None
    assert fabric.selector(b"frame") == "local"


@needs_ingress_core
def test_a_current_core_is_given_the_selector_that_routes_by_ingress_radio():
    from openhop_core.rf_fabric import RFFabric

    fabric = RFFabric()
    fabric.register_radio(object(), radio_id="local")
    fabric.register_radio(object(), radio_id="link")
    assert fabric_selects_by_ingress_radio(fabric) is True

    _apply_fabric_tx_mode(fabric, "bridge")

    assert fabric.resolve_tx_radio_id(b"frame", rx_radio_id="local") == "link"
    assert fabric.resolve_tx_radio_id(b"frame", rx_radio_id="link") == "local"
    # Originated here, so there is no side of the bridge to come from.
    assert fabric.resolve_tx_radio_id(b"frame") == "local"


def test_build_radio_stack_multi_inherits_top_level_hardware_defaults():
    """End-to-end: a partial radios[] entry reaches the radio factory complete."""
    seen = []

    def fake_get(board):
        seen.append(dict(board))
        return _FakeRadio(str(len(seen)))

    cfg = {
        "radio_type": "sx1262",
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": -1,
            "reset_pin": 16,
            "busy_pin": 24,
            "irq_pin": 22,
            "txen_pin": -1,
            "rxen_pin": -1,
            "en_pins": [12, 13],
            "use_dio3_tcxo": True,
            "dio3_tcxo_voltage": 1.8,
            "use_dio2_rf": True,
        },
        "radios": [
            {
                "id": "local",
                "radio": {"frequency": 910525000},
                "sx1262": {"cs_id": 0, "reset_pin": 16, "busy_pin": 24, "irq_pin": 22},
            },
            {
                "id": "remote",
                "radio_type": "modem_tcp",
                "modem_tcp": {"host": "remote-radio.local"},
            },
        ],
    }

    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        _radio, meta = build_radio_stack(cfg)

    assert meta["radio_ids"] == ["local", "remote"]

    local, remote = seen
    assert local["radio_type"] == "sx1262"
    assert local["sx1262"]["en_pins"] == [12, 13]
    assert local["sx1262"]["use_dio3_tcxo"] is True
    assert local["sx1262"]["dio3_tcxo_voltage"] == 1.8
    assert local["sx1262"]["use_dio2_rf"] is True
    assert local["radio"]["frequency"] == 910525000

    assert remote["radio_type"] == "modem_tcp"
    assert remote["modem_tcp"]["host"] == "remote-radio.local"


def test_merge_radio_entry_does_not_mutate_global_config():
    global_cfg = {
        "radio_type": "sx1262",
        "sx1262": {"bus_id": 0, "use_dio3_tcxo": True},
    }
    entry = {"id": "link", "sx1262": {"cs_id": 1}}

    _merge_radio_entry(global_cfg, entry)

    assert global_cfg["sx1262"] == {"bus_id": 0, "use_dio3_tcxo": True}
    assert entry["sx1262"] == {"cs_id": 1}


def test_merge_radio_entry_entry_en_pin_displaces_inherited_en_pins():
    """en_pins beats en_pin in SX1262Radio, so an inherited en_pins must not
    shadow the single pin this entry asked for."""
    global_cfg = {
        "radio_type": "sx1262",
        "sx1262": {"bus_id": 0, "en_pins": [12, 13]},
    }
    entry = {"id": "link", "sx1262": {"en_pin": 26}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["sx1262"]["en_pin"] == 26
    assert "en_pins" not in merged["sx1262"]
    assert merged["sx1262"]["bus_id"] == 0  # unrelated keys still inherited


def test_merge_radio_entry_entry_en_pins_displaces_inherited_en_pin():
    global_cfg = {
        "radio_type": "sx1262",
        "sx1262": {"bus_id": 0, "en_pin": 26},
    }
    entry = {"id": "link", "sx1262": {"en_pins": [12, 13]}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["sx1262"]["en_pins"] == [12, 13]
    assert "en_pin" not in merged["sx1262"]


@pytest.mark.parametrize(
    "inherited_key, entry_key",
    [
        ("address", "device_address"),
        ("device_address", "address"),
    ],
)
def test_merge_radio_entry_ch341_address_spelling_displaces_the_other(inherited_key, entry_key):
    """get_radio_for_board reads address before device_address, so whichever
    spelling the entry uses has to displace the inherited one -- otherwise an
    inherited address silently selects the wrong USB adapter."""
    global_cfg = {
        "radio_type": "sx1262_ch341",
        "ch341": {"vid": 0x1A86, "pid": 0x5512, "bus": 1, inherited_key: 5},
    }
    entry = {"id": "link", "ch341": {entry_key: 8}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["ch341"][entry_key] == 8
    assert inherited_key not in merged["ch341"]
    assert merged["ch341"]["bus"] == 1  # unrelated keys still inherited


@pytest.mark.parametrize(
    "inherited_key, entry_key",
    [
        ("serial_number", "serial"),
        ("serial", "serial_number"),
    ],
)
def test_merge_radio_entry_ch341_serial_spelling_displaces_the_other(inherited_key, entry_key):
    """A truthy serial_number wins over serial downstream, so an inherited one
    would outrank the entry's choice whichever name the entry used."""
    global_cfg = {
        "radio_type": "sx1262_ch341",
        "ch341": {"vid": 0x1A86, inherited_key: "AAA111"},
    }
    entry = {"id": "link", "ch341": {entry_key: "BBB222"}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["ch341"][entry_key] == "BBB222"
    assert inherited_key not in merged["ch341"]


@pytest.mark.parametrize("cleared", [0, None, ""])
def test_merge_radio_entry_ch341_falsy_entry_value_still_displaces(cleared):
    """Displacement is presence-based. An entry clearing its adapter selector
    must not have the inherited one reinstated under the other spelling."""
    global_cfg = {
        "radio_type": "sx1262_ch341",
        "ch341": {"vid": 0x1A86, "address": 5, "serial_number": "AAA111"},
    }
    entry = {"id": "link", "ch341": {"device_address": cleared}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["ch341"]["device_address"] == cleared
    assert "address" not in merged["ch341"]
    # A different alias group is untouched by this entry, so it still inherits.
    assert merged["ch341"]["serial_number"] == "AAA111"


def test_merge_radio_entry_ch341_entry_naming_both_spellings_is_left_alone():
    """Both spellings in one entry is a pre-existing accepted configuration.
    The merge must not rewrite or reject it -- get_radio_for_board's own
    precedence (address before device_address) still decides."""
    global_cfg = {
        "radio_type": "sx1262_ch341",
        "ch341": {"vid": 0x1A86, "address": 5, "device_address": 6},
    }
    entry = {"id": "link", "ch341": {"address": 8, "device_address": 9}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["ch341"]["address"] == 8
    assert merged["ch341"]["device_address"] == 9
    assert merged["ch341"]["vid"] == 0x1A86


def test_merge_radio_entry_keeps_inherited_alias_when_entry_names_neither():
    """The alias guard only fires when the entry actually picks a spelling."""
    global_cfg = {
        "radio_type": "sx1262",
        "sx1262": {"en_pins": [12, 13], "bus_id": 0},
    }
    entry = {"id": "link", "sx1262": {"cs_id": 1}}

    merged = _merge_radio_entry(global_cfg, entry)

    assert merged["sx1262"]["en_pins"] == [12, 13]


def test_metering_profile_inherits_top_level_air_settings():
    """A partial radios[] air block used to meter on library defaults rather
    than on the node's own top-level radio settings."""
    cfg = {
        "radio_type": "sx1262",
        "radio": {
            "frequency": 906875000,
            "bandwidth": 250000,
            "spreading_factor": 10,
            "coding_rate": 5,
            "preamble_length": 16,
            "tx_power": 22,
        },
        "sx1262": {"bus_id": 0},
        "radios": [{"id": "local", "radio": {"frequency": 910525000}}],
    }

    (profile,) = build_metering_profiles(cfg)

    assert profile["radio_id"] == "local"
    assert profile["frequency_hz"] == 910525000
    assert profile["bandwidth_hz"] == 250000
    assert profile["spreading_factor"] == 10
    assert profile["coding_rate"] == 5
    assert profile["tx_power"] == 22


def test_describe_radio_config_redacts_modem_token():
    board = {
        "radio_type": "modem_tcp",
        "radio": {"frequency": 910525000},
        "modem_tcp": {"host": "remote.local", "token": "s3cret-value"},
    }

    described = _describe_radio_config(board)

    assert "s3cret-value" not in described
    assert "***" in described
    assert "remote.local" in described
    assert "type='modem_tcp'" in described
    # The live config keeps its token; only the log copy is redacted.
    assert board["modem_tcp"]["token"] == "s3cret-value"


def test_ch341_alias_displacement_reaches_the_usb_transport():
    """The merge-level tests above pin the dict; this pins the consequence.

    get_radio_for_board selects the adapter with
    ``ch341.get("address", ch341.get("device_address"))``, so without
    displacement an inherited ``address`` would open a different physical
    adapter than the entry asked for, on a node with two CH341 sticks.
    """
    ch341_module = types.ModuleType("openhop_core.hardware.transports.ch341_spi_transport")
    ch341_module.CH341SPITransport = MagicMock(name="CH341SPITransport")

    config = {
        "radio_type": "sx1262_ch341",
        "ch341": {"vid": 0x1A86, "pid": 0x5512, "bus": 1, "address": 5},
        "radio": {
            "frequency": 869618000,
            "tx_power": 14,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
            "preamble_length": 32,
        },
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": 0,
            "reset_pin": 1,
            "busy_pin": 2,
            "irq_pin": 3,
            "txen_pin": -1,
            "rxen_pin": -1,
        },
        "radios": [{"id": "link", "ch341": {"device_address": 8}}],
    }

    with patch.dict(
        sys.modules,
        {"openhop_core.hardware.transports.ch341_spi_transport": ch341_module},
    ):
        with patch("openhop_core.hardware.sx1262_wrapper.SX1262Radio"):
            with patch("openhop_core.hardware.lora.LoRaRF.SX126x.set_spi_transport") as set_spi:
                build_radio_stack(config)

    (call,) = ch341_module.CH341SPITransport.call_args_list
    assert call.kwargs["address"] == 8  # the entry's adapter, not the inherited 5
    assert call.kwargs["bus"] == 1  # still inherited
    assert call.kwargs["vid"] == 0x1A86
    # Multi-radio must not install the process-global SPI transport.
    set_spi.assert_not_called()
    assert call.kwargs["set_as_global_gpio"] is False
