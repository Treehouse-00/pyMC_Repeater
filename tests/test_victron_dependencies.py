"""Initialization-only dependency handling; serial ports remain poll-scoped."""

from unittest.mock import MagicMock

import pytest

from repeater.sensors.base import SensorBase
from tests.test_victron_vedirect import manager


def test_dependency_check_runs_once_at_initialization_and_blocks_reads_when_missing(monkeypatch):
    import serial

    check = MagicMock(return_value=False)
    monkeypatch.setattr(SensorBase, "ensure_python_modules", check)
    open_port = MagicMock()
    monkeypatch.setattr(serial, "Serial", open_port)
    sm = manager()
    assert len(sm.sensors) == 1
    check.assert_called_once_with([("serial", "pyserial")])
    for _ in range(2):
        reading = sm.read_all()[0]
        assert not reading["ok"] and reading["data"] == {}
        assert "pyserial unavailable" in reading["error"]
    assert check.call_count == 1
    open_port.assert_not_called()


@pytest.mark.parametrize(
    "auto_install,install_succeeds", [(False, False), (True, False), (True, True)]
)
def test_standard_helper_honors_auto_install_only_during_initialization(
    monkeypatch, auto_install, install_succeeds
):
    import sys
    import serial
    from repeater.sensors import base
    from repeater.sensors.manager import SensorManager
    from tests.test_victron_vedirect import frame

    real_find_spec = base.importlib.util.find_spec
    installed = False

    def find_spec(name):
        if name == "serial" and not installed:
            return None
        return real_find_spec(name)

    def install(*args, **kwargs):
        nonlocal installed
        installed = install_succeeds
        return MagicMock(
            returncode=0 if install_succeeds else 1, stderr="synthetic pip failure", stdout=""
        )

    monkeypatch.setattr(base.importlib.util, "find_spec", find_spec)
    pip = MagicMock(side_effect=install)
    monkeypatch.setattr(base.subprocess, "run", pip)
    port = MagicMock()
    port.__enter__.return_value = port
    port.read.return_value = frame({"V": 13490, "IL": 200})
    open_port = MagicMock(return_value=port)
    monkeypatch.setattr(serial, "Serial", open_port)
    sm = SensorManager(
        {
            "sensors": {
                "definitions": [
                    {
                        "type": "victron_vedirect",
                        "name": "solar",
                        "auto_install_packages": auto_install,
                        "settings": {
                            "serial_device": "/dev/synthetic",
                            "read_timeout_seconds": 0.05,
                        },
                    }
                ]
            }
        }
    )
    assert len(sm.sensors) == 1
    open_port.assert_not_called()
    expected_pip_calls = int(auto_install)
    assert pip.call_count == expected_pip_calls
    if auto_install:
        assert pip.call_args.args[0] == [sys.executable, "-m", "pip", "install", "pyserial"]
    for _ in range(2):
        reading = sm.read_all()[0]
        assert reading["ok"] == (auto_install and install_succeeds)
        assert pip.call_count == expected_pip_calls
    if auto_install and install_succeeds:
        assert reading["data"]["load_power_w"] == 2.698
        assert open_port.call_count == 2
        assert port.__exit__.call_count == 2
    else:
        assert reading["data"] == {}
        open_port.assert_not_called()
