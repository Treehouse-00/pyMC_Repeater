"""Sensor manager discovery and secret round-trip contracts."""

import copy

import cherrypy
import yaml

from repeater.sensors import SensorRegistry
from repeater.web.api_endpoints import APIEndpoints


def _api(tmp_path):
    config = {
        "sensors": {
            "enabled": True,
            "definitions": [
                {
                    "name": "modem",
                    "type": "openhop_modem",
                    "enabled": True,
                    "settings": {"host": "modem.local", "password": "private-value"},
                },
                {"name": "other", "type": "bme280", "settings": {"bus_number": 1}},
            ],
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return APIEndpoints(config=copy.deepcopy(config), config_path=str(path)), path


def test_sensor_config_masks_password_without_mutating_runtime(tmp_path):
    api, _ = _api(tmp_path)
    result = api.sensors_config()
    assert result["success"] is True
    definitions = result["data"]["definitions"]
    assert definitions[0]["settings"]["password"] == "*****"
    assert definitions[1]["settings"]["bus_number"] == 1
    assert api.config["sensors"]["definitions"][0]["settings"]["password"] == "private-value"


def test_masked_password_round_trip_preserves_secret_and_other_edits(tmp_path):
    api, path = _api(tmp_path)
    body = api.sensors_config()["data"]
    body["definitions"][0]["settings"]["host"] = "new-modem.local"
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is True
    saved = yaml.safe_load(path.read_text())["sensors"]["definitions"][0]["settings"]
    assert saved["password"] == "private-value"
    assert saved["host"] == "new-modem.local"
    assert "private-value" not in str(body)


def test_renaming_unique_sensor_keeps_its_masked_password(tmp_path):
    api, path = _api(tmp_path)
    body = api.sensors_config()["data"]
    body["definitions"][0]["name"] = "renamed-modem"
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is True
    saved = yaml.safe_load(path.read_text())["sensors"]["definitions"][0]
    assert saved["name"] == "renamed-modem"
    assert saved["settings"]["password"] == "private-value"


def test_rename_existing_modem_while_adding_second_preserves_correct_password(tmp_path):
    api, path = _api(tmp_path)
    api.config["sensors"]["definitions"].reverse()
    path.write_text(yaml.safe_dump(api.config))
    body = api.sensors_config()["data"]
    body["definitions"][1]["name"] = "renamed-modem"
    body["definitions"].append(
        {
            "name": "new-modem",
            "type": "openhop_modem",
            "enabled": True,
            "settings": {"host": "second.local", "password": "second-private-value"},
        }
    )
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    result = api.sensors_config_update()
    assert result["success"] is True, result.get("error")
    saved = yaml.safe_load(path.read_text())["sensors"]["definitions"]
    assert saved[1]["settings"]["password"] == "private-value"
    assert saved[2]["settings"]["password"] == "second-private-value"
    assert all("_original_name" not in d for d in saved)


def test_swapping_modem_names_does_not_swap_their_passwords(tmp_path):
    api, path = _api(tmp_path)
    first = api.config["sensors"]["definitions"][0]
    api.config["sensors"]["definitions"] = [
        first,
        {
            "name": "second",
            "type": "openhop_modem",
            "enabled": True,
            "settings": {"host": "second.local", "password": "second-secret"},
        },
    ]
    path.write_text(yaml.safe_dump(api.config))
    body = api.sensors_config()["data"]
    body["definitions"][0]["name"] = "second"
    body["definitions"][1]["name"] = "modem"
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is True
    saved = yaml.safe_load(path.read_text())["sensors"]["definitions"]
    assert [d["settings"]["password"] for d in saved] == ["private-value", "second-secret"]


def test_duplicate_origin_cannot_copy_another_modems_masked_password(tmp_path):
    api, path = _api(tmp_path)
    original = path.read_text()
    body = api.sensors_config()["data"]
    duplicate = copy.deepcopy(body["definitions"][0])
    duplicate["name"] = "new-modem"
    duplicate["settings"]["host"] = "new.local"
    body["definitions"].append(duplicate)
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is False
    assert path.read_text() == original


def test_new_password_and_explicit_clear(tmp_path):
    api, path = _api(tmp_path)
    cherrypy.request.method = "POST"
    body = api.sensors_config()["data"]
    body["definitions"][0]["settings"]["password"] = "replacement"
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is True
    assert (
        yaml.safe_load(path.read_text())["sensors"]["definitions"][0]["settings"]["password"]
        == "replacement"
    )
    body["definitions"][0]["settings"]["password"] = ""
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is True
    assert (
        yaml.safe_load(path.read_text())["sensors"]["definitions"][0]["settings"]["password"] == ""
    )


def test_masked_password_on_new_sensor_is_rejected_without_writing(tmp_path):
    api, path = _api(tmp_path)
    before = path.read_text()
    body = api.sensors_config()["data"]
    body["definitions"].append(
        {"name": "new-modem", "type": "openhop_modem", "settings": {"password": "*****"}}
    )
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    assert api.sensors_config_update()["success"] is False
    assert path.read_text() == before


def test_duplicate_sensor_names_are_rejected_without_writing(tmp_path):
    api, path = _api(tmp_path)
    original = path.read_text()
    body = api.sensors_config()["data"]
    body["definitions"].append(
        {"name": "modem", "type": "openhop_modem", "settings": {"host": "other.local"}}
    )
    cherrypy.request.method = "POST"
    cherrypy.request.json = body
    result = api.sensors_config_update()
    assert result["success"] is False
    assert path.read_text() == original


def test_two_modems_and_legacy_alias_remain_loadable(tmp_path):
    from repeater.sensors import SensorManager

    api, path = _api(tmp_path)
    section = api.config["sensors"]
    section["definitions"] = [
        {
            "name": "north",
            "type": "openhop_modem",
            "enabled": True,
            "settings": {"host": "north.local", "password": "north-secret"},
        },
        {
            "name": "south",
            "type": "openhop_modem",
            "enabled": True,
            "settings": {"host": "south.local", "password": "south-secret"},
        },
        {
            "name": "legacy",
            "type": "pymc_modem",
            "enabled": True,
            "settings": {"host": "legacy.local", "password": "legacy-secret"},
        },
    ]
    path.write_text(yaml.safe_dump(api.config))
    masked = api.sensors_config()["data"]
    assert [d["settings"]["password"] for d in masked["definitions"]] == ["*****"] * 3
    masked["definitions"].reverse()
    cherrypy.request.method = "POST"
    cherrypy.request.json = masked
    assert api.sensors_config_update()["success"] is True
    saved = yaml.safe_load(path.read_text())["sensors"]["definitions"]
    assert [(d["name"], d["type"]) for d in saved] == [
        ("legacy", "pymc_modem"),
        ("south", "openhop_modem"),
        ("north", "openhop_modem"),
    ]
    assert [d["settings"]["password"] for d in saved] == [
        "legacy-secret",
        "south-secret",
        "north-secret",
    ]
    manager = SensorManager(api.config)
    assert [sensor.name for sensor in manager.sensors] == ["legacy", "south", "north"]
    assert manager.get_summary()["loaded"] == 3


def test_types_endpoint_discovers_new_installed_sensor_module(tmp_path, monkeypatch):
    from repeater import sensors

    folder = tmp_path / "extra-sensors"
    folder.mkdir()
    (folder / "future_sensor.py").write_text(
        "from repeater.sensors.registry import SensorRegistry\n"
        "from repeater.sensors.base import SensorBase\n"
        "@SensorRegistry.register('future_sensor')\n"
        "class FutureSensor(SensorBase):\n"
        "    sensor_type = 'future_sensor'\n"
        "    _settings_schema = [{'key': 'pin', 'type': 'integer', 'label': 'Pin', 'default': 1}]\n"
        "    def _read(self): return {'value': 1}\n"
    )
    monkeypatch.setattr(sensors, "__path__", list(sensors.__path__) + [str(folder)])
    try:
        api, _ = _api(tmp_path)
        result = api.sensors_types()
        assert result["success"] is True
        assert "pymc_modem" not in {entry["type"] for entry in result["data"]["types"]}
        future = next(t for t in result["data"]["types"] if t["type"] == "future_sensor")
        assert future["settings"][0]["key"] == "pin"
    finally:
        SensorRegistry._factories.pop("future_sensor", None)
