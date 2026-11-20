"""Core plug-in used only by tools/flicker_check.py (loaded through WAGGLE_CORE_PLUGINS in a temp Waggle home).

It adds two job kinds whose handlers start a console program the way code that forgot the no-window flag would,
and replaces the inbound handler with one that answers through chat_engine.handle_inbound into a collecting
delivery, so a chat turn runs inside the core process without Telegram."""
import json
import subprocess

from waggle.core import jobs, inbound, settings


def _unflagged_child(payload):
    r = subprocess.run(["git", "--version"], capture_output=True, text=True)
    return {"out": r.stdout.strip()}


def _turn(msg):
    from waggle import chat_engine, delivery
    d = delivery.CollectDelivery()
    rec = chat_engine.handle_inbound(msg["chat"], msg.get("text") or "", delivery=d, origin="test",
                                     platform=msg.get("platform") or "telegram", turn_id=msg.get("turn_id"),
                                     attempt=int(msg.get("attempt") or 1), address=str(msg.get("address") or ""))
    settings.path("probe_turn.json").write_text(json.dumps({"reply": d.text, "outcome": rec.outcome}),
                                                encoding="utf-8")
    return {"turn_id": rec.turn_id, "outcome": rec.outcome}


def register_core():
    jobs.register("probe.inproc", _unflagged_child, max_attempts=1, timeout_s=60)
    jobs.register("probe.detached", _unflagged_child, detached=True, max_attempts=1, timeout_s=60)
    inbound.register(_turn)
