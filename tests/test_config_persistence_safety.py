import copy
import ctypes
import ctypes.util
import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
import yaml

from repeater.config_manager import ConfigManager


@pytest.mark.parametrize("failure", ["dump", "fsync", "replace"])
def test_failed_update_preserves_file_and_shared_config(tmp_path, monkeypatch, failure):
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before"}, "radio_type": "pymc_tcp"}
    before = copy.deepcopy(config)
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    section = config["repeater"]
    manager = ConfigManager(str(path), config)
    live = Mock()
    monkeypatch.setattr(manager, "live_update_daemon", live)

    def fail(*args, **kwargs):
        if failure == "dump":
            args[1].write("partial: ")
        assert config == before
        raise OSError("synthetic write failure")

    target = "yaml.safe_dump" if failure == "dump" else f"os.{failure}"
    monkeypatch.setattr(f"repeater.config_manager.{target}", fail)
    result = manager.update_and_save({"repeater": {"node_name": "after"}})
    assert result["success"] is False
    assert result["saved"] is False
    assert result["live_updated"] is False
    assert config == before
    assert config["repeater"] is section
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    live.assert_not_called()


def test_failed_direct_save_does_not_normalize_shared_config(tmp_path, monkeypatch):
    config = {"radio_type": "pymc_tcp", "pymc_tcp": {"host": "synthetic"}}
    before = copy.deepcopy(config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    monkeypatch.setattr("repeater.config_manager.yaml.safe_dump", Mock(side_effect=OSError()))
    assert not ConfigManager(str(path), config).save_to_file()
    assert config == before
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("mode", [None, 0o600, 0o640])
def test_atomic_save_permissions_and_publication(tmp_path, monkeypatch, mode):
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before", "other": 1}}
    section = config["repeater"]
    if mode is not None:
        path.write_text(yaml.safe_dump(config))
        path.chmod(mode)
    replace = os.replace
    observed = []

    def checked_replace(source, destination):
        assert config["repeater"]["node_name"] == "before"
        assert stat.S_IMODE(os.stat(source).st_mode) == (mode or 0o600)
        assert os.stat(source).st_uid == os.getuid()
        observed.append(True)
        replace(source, destination)

    monkeypatch.setattr("repeater.config_manager.os.replace", checked_replace)
    assert ConfigManager(str(path), config).update_and_save(
        {"repeater": {"node_name": "after"}}, live_update=False
    )["saved"]
    assert observed == [True]
    assert config["repeater"] is section
    assert yaml.safe_load(path.read_text()) == config
    assert stat.S_IMODE(path.stat().st_mode) == (mode or 0o600)
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_save_preserves_posix_access_acl(tmp_path):
    if not hasattr(os, "getxattr") or not hasattr(os, "listxattr"):
        pytest.skip("extended attributes unavailable")
    library = ctypes.util.find_library("acl")
    if not library:
        pytest.skip("libacl unavailable")
    acl = ctypes.CDLL(library, use_errno=True)
    acl.acl_from_text.argtypes = [ctypes.c_char_p]
    acl.acl_from_text.restype = ctypes.c_void_p
    acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    acl.acl_set_file.restype = ctypes.c_int
    acl.acl_free.argtypes = [ctypes.c_void_p]
    acl.acl_free.restype = ctypes.c_int
    path = tmp_path / "config.yaml"
    path.write_text("repeater: {node_name: before}\n")
    path.chmod(0o640)
    # An extra named user makes this a real extended POSIX ACL, not mode bits.
    acl_ptr = acl.acl_from_text(b"u::rw-,u:12345:r--,g::r--,m::r--,o::---")
    assert acl_ptr
    try:
        if acl.acl_set_file(os.fsencode(path), 0x8000, acl_ptr) != 0:
            error = ctypes.get_errno()
            if error in (errno.ENOTSUP, errno.EOPNOTSUPP):
                pytest.skip("filesystem does not support POSIX ACLs")
            raise OSError(error, os.strerror(error))
    finally:
        acl.acl_free(acl_ptr)
    before_acl = os.getxattr(path, "system.posix_acl_access")
    before_mode = stat.S_IMODE(path.stat().st_mode)
    config = {"repeater": {"node_name": "before"}}

    assert ConfigManager(str(path), config).update_and_save(
        {"repeater": {"node_name": "after"}}, live_update=False
    )["saved"]
    assert os.getxattr(path, "system.posix_acl_access") == before_acl
    assert stat.S_IMODE(path.stat().st_mode) == before_mode
    assert yaml.safe_load(path.read_text()) == config


def _setup_directory_default_acl(tmp_path):
    if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("extended attributes unavailable")
    library = ctypes.util.find_library("acl")
    if not library:
        pytest.skip("libacl unavailable")
    acl = ctypes.CDLL(library, use_errno=True)
    acl.acl_from_text.argtypes = [ctypes.c_char_p]
    acl.acl_from_text.restype = ctypes.c_void_p
    acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    acl.acl_set_file.restype = ctypes.c_int
    acl.acl_free.argtypes = [ctypes.c_void_p]
    acl.acl_free.restype = ctypes.c_int

    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before"}}
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o640)
    original_acl_names = set(os.listxattr(path))
    assert "system.posix_acl_access" not in original_acl_names
    original_mode = stat.S_IMODE(path.stat().st_mode)

    # A default ACL is installed only after the original file exists. A new
    # staging inode inherits the named reader, but the original never had one.
    acl_ptr = acl.acl_from_text(b"u::rwx,u:12345:r-x,g::r-x,m::r-x,o::---")
    assert acl_ptr
    try:
        if acl.acl_set_file(os.fsencode(tmp_path), 0x4000, acl_ptr) != 0:
            error = ctypes.get_errno()
            if error in (errno.ENOTSUP, errno.EOPNOTSUPP):
                pytest.skip("filesystem does not support POSIX default ACLs")
            raise OSError(error, os.strerror(error))
    finally:
        acl.acl_free(acl_ptr)
    return path, config, original_acl_names, original_mode


def test_atomic_save_does_not_inherit_directory_default_acl(tmp_path):
    path, config, original_acl_names, original_mode = _setup_directory_default_acl(tmp_path)
    assert ConfigManager(str(path), config).update_and_save(
        {"repeater": {"node_name": "after"}}, live_update=False
    )["saved"]
    assert set(os.listxattr(path)) == original_acl_names
    assert "system.posix_acl_access" not in os.listxattr(path)
    assert stat.S_IMODE(path.stat().st_mode) == original_mode
    assert yaml.safe_load(path.read_text()) == config


def test_inherited_acl_removal_failure_preserves_file_and_shared_config(tmp_path, monkeypatch):
    path, config, original_acl_names, _ = _setup_directory_default_acl(tmp_path)
    previous_bytes = path.read_bytes()
    previous_inode = path.stat().st_ino
    section = config["repeater"]
    manager = ConfigManager(str(path), config)
    live = Mock()
    monkeypatch.setattr(manager, "live_update_daemon", live)
    attempted = []

    def fail_removal(target, name):
        assert isinstance(target, int)
        assert name == "system.posix_acl_access"
        attempted.append(True)
        raise PermissionError(errno.EPERM, "inherited ACL removal denied")

    monkeypatch.setattr("repeater.config_manager.os.removexattr", fail_removal)
    result = manager.update_and_save({"repeater": {"node_name": "after"}})
    assert attempted == [True]
    assert result == {
        "success": False,
        "saved": False,
        "live_updated": False,
        "error": "Failed to save config to file",
    }
    assert path.read_bytes() == previous_bytes
    assert path.stat().st_ino == previous_inode
    assert set(os.listxattr(path)) == original_acl_names
    assert config == {"repeater": {"node_name": "before"}}
    assert config["repeater"] is section
    assert list(tmp_path.iterdir()) == [path]
    live.assert_not_called()


def test_xattr_copy_failure_does_not_commit_or_publish(tmp_path, monkeypatch):
    if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("extended attributes unavailable")
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before"}}
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o640)
    try:
        os.setxattr(path, "user.review_marker", b"original")
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            pytest.skip("filesystem does not support user extended attributes")
        raise
    previous_bytes = path.read_bytes()
    previous_inode = path.stat().st_ino
    section = config["repeater"]
    manager = ConfigManager(str(path), config)
    live = Mock()
    monkeypatch.setattr(manager, "live_update_daemon", live)
    attempted = []

    def fail_staged_copy(target, name, value, *args, **kwargs):
        assert isinstance(target, int)  # A staging inode, never the original path.
        assert name == "user.review_marker"
        assert value == b"original"
        assert path.read_bytes() == previous_bytes
        attempted.append(True)
        raise PermissionError(errno.EPERM, "metadata copy denied")

    monkeypatch.setattr("repeater.config_manager.os.setxattr", fail_staged_copy)
    result = manager.update_and_save({"repeater": {"node_name": "after"}})
    assert attempted == [True]
    assert result == {
        "success": False,
        "saved": False,
        "live_updated": False,
        "error": "Failed to save config to file",
    }
    assert path.read_bytes() == previous_bytes
    assert path.stat().st_ino == previous_inode
    assert config == {"repeater": {"node_name": "before"}}
    assert config["repeater"] is section
    assert os.getxattr(path, "user.review_marker") == b"original"
    assert list(tmp_path.iterdir()) == [path]
    live.assert_not_called()


def test_concurrent_managers_serialize_staging_and_saving(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before"}}
    first = ConfigManager(str(path), config)
    second = ConfigManager(str(path), config)
    entered, release, second_started, second_dump = Event(), Event(), Event(), Event()
    dump = yaml.safe_dump

    def paused_dump(data, *args, **kwargs):
        if data["repeater"].get("first") and not data["repeater"].get("second"):
            entered.set()
            assert release.wait(5)
        else:
            second_dump.set()
        return dump(data, *args, **kwargs)

    def second_update():
        second_started.set()
        return second.update_and_save({"repeater": {"second": True}}, live_update=False)

    monkeypatch.setattr("repeater.config_manager.yaml.safe_dump", paused_dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.update_and_save, {"repeater": {"first": True}}, False)
        try:
            assert entered.wait(5)
            b = pool.submit(second_update)
            assert second_started.wait(5)
            assert not second_dump.wait(0.1)
            assert config == {"repeater": {"node_name": "before"}}
        finally:
            release.set()
        assert a.result()["saved"]
        assert b.result()["saved"]
    assert config["repeater"] == {"node_name": "before", "first": True, "second": True}
    assert yaml.safe_load(path.read_text()) == config
