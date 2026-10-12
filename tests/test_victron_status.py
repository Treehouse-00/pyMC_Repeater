"""Synthetic status fixtures from VE.Direct protocol 3.34, pp. 11, 15–16."""

import pytest

from repeater.sensors.victron_vedirect import VictronVEDirectSensor, normalize

ERRORS = {
    0: "No error",
    2: "Battery voltage too high",
    17: "Charger temperature too high",
    18: "Charger over current",
    19: "Charger current reversed",
    20: "Bulk time limit exceeded",
    21: "Current sensor issue (sensor bias/sensor broken)",
    26: "Terminals overheated",
    28: "Converter issue (dual converter models only)",
    33: "Input voltage too high (solar panel)",
    34: "Input current too high (solar panel)",
    38: "Input shutdown (due to excessive battery voltage)",
    39: "Input shutdown (due to current flow during off mode)",
    65: "Lost communication with one of devices",
    66: "Synchronised charging device configuration issue",
    67: "BMS connection lost",
    68: "Network misconfigured",
    116: "Factory calibration data lost",
    117: "Invalid/incompatible firmware",
    119: "User settings invalid",
    999: "Unknown (999)",
}
TRACKERS = {
    0: "Off",
    1: "Voltage or current limited",
    2: "MPP Tracker active",
    999: "Unknown (999)",
}
MONITORS = dict(
    zip(
        range(-9, 9),
        [
            "Solar charger",
            "Wind turbine",
            "Shaft generator",
            "Alternator",
            "Fuel cell",
            "Water generator",
            "DC/DC charger",
            "AC charger",
            "Generic source",
            "Battery monitor (BMV)",
            "Generic load",
            "Electric drive",
            "Fridge",
            "Water pump",
            "Bilge pump",
            "DC system",
            "Inverter",
            "Water heater",
        ],
    )
)
MONITORS[999] = "Unknown (999)"


def reading(monkeypatch, fields):
    sensor = VictronVEDirectSensor("Synthetic", {"settings": {"serial_device": "/dev/synthetic"}})

    def acquire():
        sensor._seen.update(fields)
        return normalize(fields)

    monkeypatch.setattr(sensor, "_read_serial", acquire)
    return sensor, sensor.read()


@pytest.mark.parametrize(
    "field,code,label",
    [
        (field, code, label)
        for field, table in [("ERR", ERRORS), ("MPPT", TRACKERS), ("MON", MONITORS)]
        for code, label in table.items()
    ],
)
def test_enum_descriptor_preserves_numeric_data(monkeypatch, field, code, label):
    _, result = reading(monkeypatch, {field: str(code)})
    descriptor = result["metrics"][0]
    assert descriptor["display_value"] == label
    assert result["data"] == {descriptor["data_key"]: code}
    assert descriptor["category"] == "status"
    assert descriptor["kind"] == "number"


@pytest.mark.parametrize("field", ["CS", "ERR", "MPPT", "OR", "AR", "MON"])
@pytest.mark.parametrize("failure", ["invalid", "missing", "read_failed"])
def test_unavailable_status_never_keeps_display_value(monkeypatch, field, failure):
    sensor, initial = reading(monkeypatch, {field: "0"})
    assert initial["metrics"][0]["display_value"]

    def acquire():
        if failure == "read_failed":
            raise OSError("synthetic disconnect")
        return normalize({field: "---"}) if failure == "invalid" else {}

    monkeypatch.setattr(sensor, "_read_serial", acquire)
    result = sensor.read()
    descriptor = result["metrics"][0]
    assert not descriptor["available"]
    assert descriptor["reason"] == failure
    assert "display_value" not in descriptor
    assert "charge_state" not in result["data"]


@pytest.mark.parametrize("field", ["CS", "ERR", "MPPT", "AR"])
def test_negative_unsigned_status_is_invalid(monkeypatch, field):
    _, result = reading(monkeypatch, {field: "-1"})
    descriptor = result["metrics"][0]
    assert result["data"][descriptor["data_key"]] is None
    assert not descriptor["available"]
    assert "display_value" not in descriptor


def test_day_sequence_and_flags_are_not_display_strings(monkeypatch):
    _, result = reading(monkeypatch, {"HSDS": "34", "LOAD": "ON", "Relay": "OFF", "Alarm": "OFF"})
    assert result["data"] == {
        "day_sequence": 34,
        "load_output": True,
        "relay": False,
        "alarm": False,
    }
    assert all("display_value" not in metric for metric in result["metrics"])


@pytest.mark.parametrize(
    "field,raw,code,label",
    [
        ("OR", "0x00000000", 0, "None"),
        ("OR", "0x00000001", 1, "No input power"),
        ("OR", "0x00000002", 2, "Switched off (power switch)"),
        ("OR", "0x00000004", 4, "Switched off (device mode register)"),
        ("OR", "0x00000008", 8, "Remote input"),
        ("OR", "0x00000010", 16, "Protection active"),
        ("OR", "0x00000020", 32, "Paygo"),
        ("OR", "0x00000040", 64, "BMS"),
        ("OR", "0x00000080", 128, "Engine shutdown detection"),
        ("OR", "0x00000100", 256, "Analysing input voltage"),
        ("OR", "0x80000009", 2147483657, "No input power; Remote input; Unknown bits (0x80000000)"),
        ("OR", "0x00000200", 512, "Unknown bits (0x200)"),
        ("AR", "0", 0, "None"),
        ("AR", "1", 1, "Low Voltage"),
        ("AR", "2", 2, "High Voltage"),
        ("AR", "4", 4, "Low SOC"),
        ("AR", "8", 8, "Low Starter Voltage"),
        ("AR", "16", 16, "High Starter Voltage"),
        ("AR", "32", 32, "Low Temperature"),
        ("AR", "64", 64, "High Temperature"),
        ("AR", "128", 128, "Mid Voltage"),
        ("AR", "256", 256, "Overload"),
        ("AR", "512", 512, "DC-ripple"),
        ("AR", "1024", 1024, "Low V AC out"),
        ("AR", "2048", 2048, "High V AC out"),
        ("AR", "4096", 4096, "Short Circuit"),
        ("AR", "8192", 8192, "BMS Lockout"),
        ("AR", "16389", 16389, "Low Voltage; Low SOC; Unknown bits (0x4000)"),
    ],
)
def test_masks_preserve_every_active_reason_and_unknown_bits(monkeypatch, field, raw, code, label):
    _, result = reading(monkeypatch, {field: raw})
    descriptor = result["metrics"][0]
    assert descriptor["display_value"] == label
    assert result["data"] == {descriptor["data_key"]: code}
