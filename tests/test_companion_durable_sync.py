"""A synced companion message is not lost when the client drops.

``SYNC_NEXT_MESSAGE`` used to delete the SQLite row before the frame reached
the client.  Now the row is marked delivered only when the client's next
command arrives; a client that disconnects (or is evicted) first sees the
same message again.  Delivered rows stay as history until retention prunes
them, and the offline-queue cap counts undelivered rows only.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from openhop_core.companion.constants import CMD_SYNC_NEXT_MESSAGE, RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.message_queue import MessageQueue

from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x01"


class _Bridge:
    def __init__(self):
        self.message_queue = MessageQueue(max_size=100)

    def sync_next_message(self):
        return self.message_queue.pop()


def _server(handler, writer=None):
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = handler
    fs.companion_hash = _HASH
    fs.bridge = _Bridge()
    fs._app_target_ver = 3
    fs._client_writer = writer or object()
    fs.port = 5000
    fs._pending_delivery = None
    fs._cmd_handlers = {CMD_SYNC_NEXT_MESSAGE: fs._cmd_sync_next_message}
    fs.frames = []
    fs._write_frame = lambda data: fs.frames.append(bytes(data))
    return fs


def _push(handler, text, packet_hash, is_channel=False):
    assert handler.companion_push_message(
        _HASH,
        {
            "sender_key": b"\x11" * 32,
            "text": text,
            "timestamp": 1000,
            "txt_type": 0,
            "is_channel": is_channel,
            "channel_idx": 0,
            "path_len": 0xFF,
            "packet_hash": packet_hash,
        },
        100,
    )


@pytest.mark.asyncio
async def test_message_stays_queued_until_the_next_command(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "first", "p1")
    fs = _server(handler)

    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert len(fs.frames) == 1
    assert fs.frames[0][0] != RESP_CODE_NO_MORE_MESSAGES
    # Not yet acknowledged: still counted as undelivered.
    assert handler.companion_count_messages(_HASH) == 1

    # The next command is the acknowledgement.
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert handler.companion_count_messages(_HASH) == 0
    assert fs.frames[1][0] == RESP_CODE_NO_MORE_MESSAGES


@pytest.mark.asyncio
async def test_dropped_client_sees_the_message_again(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "only", "p1")
    writer = object()
    fs = _server(handler, writer)

    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs._pending_delivery == (writer, 1)

    # Disconnect before any further command: the delivery is abandoned, not
    # acknowledged, and the row is still queued for the next session.
    fs._write_queue = None
    fs._writer_task = None
    fs._client_reader = None
    await fs._cleanup_client(writer, MagicMock(), MagicMock(done=lambda: True), "empty_read")
    assert fs._pending_delivery is None
    assert handler.companion_count_messages(_HASH) == 1

    fs._client_writer = object()
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))
    assert fs.frames[-1][0] != RESP_CODE_NO_MORE_MESSAGES


@pytest.mark.asyncio
async def test_an_evicting_client_cannot_acknowledge_for_the_old_one(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "only", "p1")
    old_writer = object()
    fs = _server(handler, old_writer)
    await fs._handle_cmd(bytes([CMD_SYNC_NEXT_MESSAGE]))

    # A new client takes the slot before the old one acknowledges.
    fs._client_writer = object()
    fs._ack_pending_delivery()
    assert handler.companion_count_messages(_HASH) == 1


def test_delivered_rows_are_history_until_retention_prunes_them(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _push(handler, "old", "p1")
    _push(handler, "new", "p2")
    _push(handler, "queued", "p3")
    old = handler.companion_pop_message(_HASH)
    new = handler.companion_pop_message(_HASH)
    assert (old["text"], new["text"]) == ("old", "new")
    assert handler.companion_count_messages(_HASH) == 1

    with handler._connect() as conn:
        conn.execute(
            "UPDATE companion_messages SET delivered_at = ? WHERE text = 'old'",
            (time.time() - 40 * 86400,),
        )
        conn.commit()
    handler.cleanup_old_data(days=7, companion_events_days=31)
    with handler._connect() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM companion_messages ORDER BY id")]
    assert texts == ["new", "queued"]


def test_offline_queue_cap_counts_undelivered_only(tmp_path):
    handler = SQLiteHandler(tmp_path)
    for i in range(3):
        _push(handler, f"c{i}", f"p{i}", is_channel=True)
    assert handler.companion_pop_message(_HASH)["text"] == "c0"
    assert handler.companion_pop_message(_HASH)["text"] == "c1"
    # Two delivered rows remain as history; only one row is queued, so a cap
    # of 2 admits the next message without evicting anything.
    assert handler.companion_push_message(
        _HASH,
        {"text": "c3", "timestamp": 1, "packet_hash": "p3", "is_channel": True},
        2,
    )
    assert [m["text"] for m in handler.companion_load_messages(_HASH)] == ["c2", "c3"]
