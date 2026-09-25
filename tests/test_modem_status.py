from threading import Event
from types import SimpleNamespace

import pytest

from repeater.config import (
    BaselineCrcCounterRadio,
    NullRadio,
    disconnected_modems,
    get_radio_for_board,
)
from repeater.main import RepeaterDaemon


def test_tcp_deferred_connect_disconnect_and_recovery():
    config = {"radio_type": "modem_tcp"}
    transport = SimpleNamespace(_sock=None, crc_error_count=0)
    radio = BaselineCrcCounterRadio(transport)
    daemon = RepeaterDaemon(config, radio=radio)
    assert daemon.get_stats()["modem_disconnected"] == ["modem_tcp"]
    transport._sock = object()
    assert daemon.get_stats()["modem_disconnected"] == []
    transport._sock = None
    assert daemon.get_stats()["modem_disconnected"] == ["modem_tcp"]


def test_usb_disconnection_recovery_and_failed_init():
    event = Event()
    radio = SimpleNamespace(_connected_event=event)
    config = {"radio_type": "modem_usb"}
    assert disconnected_modems(config, radio) == ["modem_usb"]
    event.set()
    assert disconnected_modems(config, radio) == []
    event.clear()
    assert disconnected_modems(config, radio) == ["modem_usb"]
    assert disconnected_modems(config, NullRadio()) == ["modem_usb"]


def test_fabric_only_reports_disconnected_modems():
    config = {
        "radios": [
            {"id": "local", "radio_type": "sx1262_ch341"},
            {"id": "backhaul", "radio_type": "modem_tcp"},
            {"id": "usb", "radio_type": "pymc_usb"},
        ]
    }
    fabric = SimpleNamespace(
        radios={
            "local": object(),
            "backhaul": SimpleNamespace(_sock=None),
            "usb": SimpleNamespace(_connected_event=Event()),
        }
    )
    assert disconnected_modems(config, SimpleNamespace(fabric=fabric)) == [
        "backhaul: modem_tcp",
        "usb: modem_usb",
    ]
    fabric.radios["backhaul"]._sock = object()
    fabric.radios["usb"]._connected_event.set()
    assert disconnected_modems(config, SimpleNamespace(fabric=fabric)) == []
    assert disconnected_modems({"radio_type": "sx1262_ch341"}, NullRadio()) == []
    assert disconnected_modems(config, NullRadio()) == [
        "backhaul: modem_tcp",
        "usb: modem_usb",
    ]


def test_usb_begin_false_is_not_reported_as_initialized(monkeypatch):
    pytest.importorskip("openhop_core.hardware.usb_radio")

    class UnavailableUSB:
        def __init__(self, **kwargs):
            pass

        def begin(self):
            return False

    monkeypatch.setattr("openhop_core.hardware.usb_radio.USBLoRaRadio", UnavailableUSB)
    with pytest.raises(RuntimeError, match="USB modem did not connect"):
        get_radio_for_board({"radio_type": "modem_usb", "modem_usb": {"port": "/dev/missing"}})
