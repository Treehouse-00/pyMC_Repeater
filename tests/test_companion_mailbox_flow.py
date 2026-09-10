"""The mailbox loop a client runs against the API.

A received message is persisted; the bridge's ``message_received`` event is
the wake signal an SSE client sees; the client then reads by cursor from
``/api/companion/messages``; a frame client that syncs the same row marks it
delivered on its next command; the cursor read reflects that, and a further
read from the last id returns nothing new. Frame and API clients read one
mailbox.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openhop_core.companion import CompanionBridge
from openhop_core.companion.constants import CMD_SYNC_NEXT_MESSAGE, RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.models import MessageEvent
from openhop_core.protocol import LocalIdentity, Packet

from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.companion_endpoints import CompanionAPIEndpoints

_HASH = "0x01"


class _Injector:
    async def __call__(self, pkt: Packet, **kwargs) -> bool:
        return True


def _endpoints(bridge, handler):
    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._sse_callbacks = []
    ep._get_bridge = lambda **kw: bridge
    ep.daemon_instance = SimpleNamespace(
        companion_bridges={1: bridge},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )
    ep.broadcasts = []
    ep._broadcast_sse = ep.broadcasts.append
    return ep


def _frame_client(handler, bridge):
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = handler
    fs.companion_hash = _HASH
    fs.bridge = bridge
    fs._app_target_ver = 3
    fs._client_writer = object()
    fs.port = 5000
    fs._pending_delivery = None
    fs._cmd_handlers = {CMD_SYNC_NEXT_MESSAGE: fs._cmd_sync_next_message}
    fs.frames = []
    fs._write_frame = lambda data: fs.frames.append(bytes(data))
    return fs


def _messages(ep, **kwargs):
    return CompanionAPIEndpoints.messages.__wrapped__(ep, **kwargs)["data"]


@pytest.mark.asyncio
async def test_api_and_frame_clients_read_one_mailbox(tmp_path):
    handler = SQLiteHandler(tmp_path)
    bridge = CompanionBridge(LocalIdentity(), _Injector())
    ep = _endpoints(bridge, handler)
    ep._ensure_callbacks()

    # A message arrives: persisted for sync, and the event stream is woken.
    assert handler.companion_push_message(
        _HASH,
        {"sender_key": b"\x11" * 32, "text": "hello", "timestamp": 1, "packet_hash": "p1"},
        100,
    )
    await bridge._fire_callbacks(
        "message_event",
        MessageEvent(sender_key=b"\x11" * 32, text="hello", timestamp=1, txt_type=0),
    )
    assert [b["event"] for b in ep.broadcasts] == ["message_received"]

    # The API client reads by cursor and sees the row, not yet delivered.
    page = _messages(ep, since=0)
    assert [(m["id"], m["text"], m["delivered"]) for m in page] == [(1, "hello", False)]
    cursor = page[-1]["id"]

    # A frame client syncs the same row; its next command is the receipt.
    fs = _frame_client(handler, bridge)
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert _messages(ep, since=0)[0]["delivered"] is False
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] == RESP_CODE_NO_MORE_MESSAGES
    assert _messages(ep, since=0)[0]["delivered"] is True

    # Nothing new after the cursor; the next message appears after it.
    assert _messages(ep, since=cursor) == []
    assert handler.companion_push_message(
        _HASH,
        {"sender_key": b"\x11" * 32, "text": "again", "timestamp": 2, "packet_hash": "p2"},
        100,
    )
    assert [m["text"] for m in _messages(ep, since=cursor)] == ["again"]
