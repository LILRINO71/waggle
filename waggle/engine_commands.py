"""The slash commands Waggle itself serves in an engine chat, answered by plain Waggle code (no Claude, no model).

Which `/commands` exist, and who answers them (checked 2026-11-20):

  Waggle's own (this module):      /waggle [status]     the Claude window, Waggle's share, any usage block, media folder
                                   /waggle media        what the media folder holds, how long files are kept
                                   /waggle media clean  delete the saved attachments now
                                   /waggle memory       the last changes Waggle made to your vault (vault_git.py)
                                   /waggle memory show <id>      what one change did
                                   /waggle memory restore <id>   undo one change, as a new change
                                   /waggle help         this list
  Hermes only (left on the old path: the gateway answers them, the engine never sees them):
                                   /new /reset /stop /status /model /usage /retry /undo /compress /voice /yolo
                                   /skills /memory /cron /help /update ... every other Hermes built-in, and
                                   skill commands. They act on Hermes's own sessions and settings.
  Not existing: /on /off. There is no single "Waggle off" command (HANDOFF "Not proven" 5); Waggle's switches are
                                   settings (engine_chats, engine_media) and the core's scheduled tasks.

The plugin's pre_gateway_dispatch hook sends only the commands in SERVED to the core (kind "command"); the core's
inbound handler (chat_engine.handle_inbound) answers them through serve(), delivered by Waggle's own Bot API sender
and recorded as a turn with route "command".
"""
import re
import time

SERVED = {"waggle"}
_CMD = re.compile(r"^/([A-Za-z][A-Za-z0-9_]*)(?:@\w+)?(?:\s+(.*))?$", re.S)

HELP = ("Waggle commands:\n"
        "- /waggle: usage window, Waggle's share, saved attachments\n"
        "- /waggle media: what the media folder holds\n"
        "- /waggle media clean: delete the saved attachments now\n"
        "- /waggle memory: the last changes Waggle made to your notes; \"memory show <id>\" and \"memory restore <id>\"\n"
        "- /waggle help: this list\n"
        "Everything else starting with / (/new, /stop, /model and so on) is answered by Hermes.")


def parse(text):
    """(command, args) for a slash command ("/waggle@bot media" -> ("waggle", "media")), else None."""
    m = _CMD.match((text or "").strip())
    return (m.group(1).lower(), (m.group(2) or "").strip()) if m else None


def serves(text):
    """True when Waggle's own code answers this command (the engine takes it instead of Hermes)."""
    p = parse(text)
    return bool(p and p[0] in SERVED)


def _when(ts):
    try:
        return time.strftime("%#I:%M %p on %A", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _size(n):
    return f"{n / (1024 * 1024):.1f} MB"


def _status(settings):
    from . import media_intake, quota
    lines = ["Waggle status"]
    try:
        q = quota.status(settings)
        line = f"- Claude window: {q['window_pct']:g}% used"
        if q.get("resets_at"):
            line += f", resets about {_when(q['resets_at'])}"
        lines.append(line + (f" ({q['source']})" if q.get("source") else ""))
        lines.append(f"- Waggle's own use this window: {q['waggle_units']:g} units")
        if (q.get("blocked_until") or 0) > time.time():  # a block that has ended is history, not status
            lines.append(f"- Claude is blocked until about {_when(q['blocked_until'])}; the small local model answers")
    except Exception as e:
        lines.append(f"- usage: could not be read ({type(e).__name__})")
    try:
        u = media_intake.usage()
        lines.append(f"- saved attachments: {u['files']} files, {_size(u['bytes'])} "
                     f"(kept {media_intake.keep_days(settings):g} days)")
    except Exception:
        pass
    return "\n".join(lines)


def _media(settings, args):
    from . import media_intake
    if args.lower() in ("clean", "clear", "cleanup", "delete"):
        res = media_intake.cleanup(settings, max_age_days=0, now=time.time() + 1)
        return f"Deleted {res['removed']} saved attachment(s), freed {_size(res['freed'])}."
    u = media_intake.usage()
    return (f"Saved attachments: {u['files']} files, {_size(u['bytes'])}.\n"
            f"Photos, files and audio you send are kept {media_intake.keep_days(settings):g} days, then deleted "
            f"(limit {media_intake.limit_bytes(settings) // (1024 * 1024)} MB per file). "
            "\"/waggle media clean\" deletes them now.")


def _memory(settings, args):
    """/waggle memory [log [n] | show <id> | restore <id>]: Waggle's changes to the vault, from its local git history."""
    from . import vault_git
    if not vault_git.enabled(settings):
        return "Memory history is off (vault_git is false in the settings)."
    parts = args.split()
    word = parts[0].lower() if parts else "log"
    if word == "log" or word.isdigit():
        n = int(parts[1]) if word == "log" and len(parts) > 1 and parts[1].isdigit() else int(word) if word.isdigit() else 8
        rows = vault_git.log(min(max(n, 1), 25), settings)
        if not rows:
            return "No memory history yet. It starts the first time Waggle changes a note."
        return ("Waggle's recent changes to your notes (newest first):\n" + vault_git.format_log(rows) +
                "\n\n\"/waggle memory show <id>\" shows one, \"/waggle memory restore <id>\" undoes it.")
    if word in ("show", "diff") and len(parts) > 1:
        return vault_git.format_show(vault_git.show(parts[1], settings), diff=True)[:3500]
    if word in ("restore", "undo", "revert") and len(parts) > 1:
        return vault_git.restore(parts[1], settings)["message"]
    return "Use \"/waggle memory\", \"/waggle memory show <id>\" or \"/waggle memory restore <id>\"."


def serve(text, settings=None):
    """The reply to one command Waggle serves. Never raises."""
    from .config import Settings
    p = parse(text)
    if not p or p[0] not in SERVED:
        return "I don't answer that command."
    args = p[1]
    try:
        s = settings or Settings.load()
        sub = args.split(None, 1)
        word = sub[0].lower() if sub else ""
        if word in ("", "status", "quota", "usage"):
            return _status(s)
        if word == "media":
            return _media(s, sub[1] if len(sub) > 1 else "")
        if word in ("memory", "mem"):
            return _memory(s, sub[1] if len(sub) > 1 else "")
        if word in ("help", "?", "commands"):
            return HELP
        return f"I don't know \"/waggle {args[:40]}\".\n\n{HELP}"
    except Exception as e:
        return f"That command failed: {type(e).__name__}: {str(e)[:120]}"


def sticker_reply(emoji=""):
    """A sticker gets a short local answer, never a model call: its own emoji back, else a thumbs up."""
    e = (emoji or "").strip()
    return e if e and len(e) <= 8 else "👍"
