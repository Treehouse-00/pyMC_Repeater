"""GET /api/companion/messages reads the mailbox by cursor.

Every client — a frame client that syncs, a REST client that polls — sees the
same received messages with the same ascending ids. ``since`` is the last id a
client has applied; ``delivered`` says whether a frame client already synced
the row.
"""

from __future__ import annotations

from types import SimpleNamespace

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.companion_endpoints import CompanionAPIEndpoints

_HASH = "0x01"


def _push(handler, text, packet_hash, is_channel=False):
    assert handler.companion_push_message(
        _HASH,
        {
            "sender_key": b"\x11" * 32,
            "text": text,
            "timestamp": 1000,
            "txt_type": 0,
            "is_channel": is_channel,
            "channel_idx": 2,
            "path_len": 0xFF,
            "packet_hash": packet_hash,
            "sender_prefix": b"\xaa\xbb\xcc\xdd",
        },
        100,
    )


def _endpoints(handler):
    bridge = object()
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep.daemon_instance = SimpleNamespace(
        companion_bridges={1: bridge},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )
    ep._get_bridge = lambda **kw: bridge
    return ep


def _messages(ep, **kwargs):
    """Call past the @require_auth wrapper (no auth context in tests)."""
    return CompanionAPIEndpoints.messages.__wrapped__(ep, **kwargs)


def test_history_is_read_by_cursor_and_marks_delivery(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "one", "p1")
    _push(handler, "two", "p2", is_channel=True)
    _push(handler, "three", "p3")
    assert handler.companion_pop_message(_HASH)["text"] == "one"
    ep = _endpoints(handler)

    page = _messages(ep)
    assert page["success"] is True
    items = page["data"]
    assert [m["text"] for m in items] == ["one", "two", "three"]
    assert [m["id"] for m in items] == [1, 2, 3]
    assert [m["delivered"] for m in items] == [True, False, False]
    assert items[0]["sender_key"] == "11" * 32
    assert items[0]["sender_prefix"] == "aabbccdd"
    assert items[1]["is_channel"] is True and items[1]["channel_idx"] == 2

    assert [m["text"] for m in _messages(ep, since=2)["data"]] == ["three"]
    assert [m["text"] for m in _messages(ep, since=3)["data"]] == []
    assert len(_messages(ep, limit=1)["data"]) == 1


def test_bad_cursor_values_are_rejected(tmp_path):
    ep = _endpoints(SQLiteHandler(tmp_path))
    with pytest.raises(cherrypy.HTTPError):
        _messages(ep, since="abc")
    with pytest.raises(cherrypy.HTTPError):
        _messages(ep, since=-1)
    with pytest.raises(cherrypy.HTTPError):
        _messages(ep, limit=0)


def test_history_scopes_to_the_resolved_companion(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "mine", "p1")
    assert handler.companion_push_message(
        "0x02", {"text": "theirs", "timestamp": 1, "packet_hash": "p9"}, 100
    )
    ep = _endpoints(handler)
    assert [m["text"] for m in _messages(ep)["data"]] == ["mine"]
