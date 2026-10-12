"""Synthetic load-power tests through the real sensor manager; no live hardware."""

from unittest.mock import MagicMock

import pytest

from tests.test_victron_vedirect import frame, manager


def serial_port(monkeypatch, payload):
    import serial

    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = payload
    monkeypatch.setattr(serial, "Serial", MagicMock(return_value=port))
    return port


@pytest.mark.parametrize(
    "voltage,current", [(12800, 1500), (12800, 0), (0, 1500), (0, 0), (12800, -500)]
)
def test_load_power_is_calculated_in_watts_and_grouped_as_measurement(
    monkeypatch, voltage, current
):
    port = serial_port(monkeypatch, frame({"V": voltage, "IL": current}))
    result = manager().read_all()[0]
    assert result["ok"]
    assert result["data"]["load_power_w"] == pytest.approx((voltage / 1000) * (current / 1000))
    descriptor = next(
        metric for metric in result["metrics"] if metric["data_key"] == "load_power_w"
    )
    assert descriptor == {
        "id": "/derived/load_power_w",
        "source_path": "/derived/load_power_w",
        "data_key": "load_power_w",
        "label": "Load power (calculated)",
        "unit": "W",
        "kind": "number",
        "category": "measurement",
        "available": True,
    }
    port.write.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"V": 12800},
        {"IL": 1500},
        {"V": "---", "IL": 1500},
        {"V": 12800, "IL": "---"},
        {"V": "NaN", "IL": 1500},
        {"V": 12800, "IL": "inf"},
    ],
)
def test_load_power_is_not_fabricated_without_valid_inputs(monkeypatch, fields):
    serial_port(monkeypatch, frame(fields))
    result = manager().read_all()[0]
    assert result["data"].get("load_power_w") is None
    assert all(
        not metric["available"]
        for metric in result["metrics"]
        if metric["data_key"] == "load_power_w"
    )


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"V": 12800}, "missing"),
        ({"IL": 1500}, "missing"),
        ({"V": 12800, "IL": "---"}, "invalid"),
        ({"V": "---", "IL": 1500}, "invalid"),
        (None, "read_failed"),
    ],
)
def test_derived_card_becomes_unavailable_without_stale_power(monkeypatch, fields, reason):
    port = serial_port(monkeypatch, frame({"V": 12800, "IL": 1500}))
    sensor_manager = manager()
    first = sensor_manager.read_all()[0]
    assert first["data"]["load_power_w"] == pytest.approx(12.8 * 1.5)
    if fields is None:
        port.read.side_effect = OSError("synthetic disconnect")
    else:
        port.read.return_value = frame(fields)
    second = sensor_manager.read_all()[0]
    assert second["data"].get("load_power_w") is None
    descriptor = next(
        metric for metric in second["metrics"] if metric["data_key"] == "load_power_w"
    )
    assert not descriptor["available"]
    assert descriptor["reason"] == reason


def test_load_power_uses_separate_validated_blocks_of_same_poll(monkeypatch):
    serial_port(monkeypatch, frame({"V": 12800}) + frame({"IL": 1500}))
    result = manager().read_all()[0]
    assert result["data"]["load_power_w"] == pytest.approx(12.8 * 1.5)


def test_load_power_never_combines_inputs_from_different_polls(monkeypatch):
    port = serial_port(monkeypatch, frame({"V": 12800}))
    sensor_manager = manager()
    for fields in ({"V": 12800}, {"IL": 1500}, {"V": 12800}):
        port.read.return_value = frame(fields)
        result = sensor_manager.read_all()[0]
        assert "load_power_w" not in result["data"]
        assert all(metric["data_key"] != "load_power_w" for metric in result["metrics"])


def test_load_power_does_not_use_value_invalidated_later_in_same_poll(monkeypatch):
    serial_port(monkeypatch, frame({"V": 12800, "IL": 1500}) + frame({"IL": "---"}))
    result = manager().read_all()[0]
    assert result["data"]["load_power_w"] is None
