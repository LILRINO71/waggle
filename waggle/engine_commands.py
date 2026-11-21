"""The slash commands Waggle itself serves in an engine chat, answered by plain Waggle code (no Claude, no model).

Which `/commands` exist, and who answers them (checked 2026-11-20):

  Waggle's own (this module):      /waggle [status]     the Claude window, Waggle's share, any usage block, media folder
                                   /waggle media        what the media folder holds, how long files are kept
                                   /waggle media clean  shows what it would clean and asks; "yes" (or "approve CODE")
                                                        moves them to the trash folder (media_clean_* below)
                                   /waggle media clean confirm   the same, with the question already answered
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
MEDIA_CLEAN_TOOL = "waggle_media_clean"  # the name its approval is filed under in approvals.db (waggle/policy.py)
OWNER_ORIGINS = ("user", "canary")  # who may clean: the owner's own message, or the test client signed in as them
CLEAN_WORDS = ("clean", "clear", "cleanup", "delete")
CONFIRM_WORDS = ("confirm", "yes", "now", "go")
_CMD = re.compile(r"^/([A-Za-z][A-Za-z0-9_]*)(?:@\w+)?(?:\s+(.*))?$", re.S)

HELP = ("Waggle commands:\n"
        "- /waggle: usage window, Waggle's share, saved attachments\n"
        "- /waggle media: what the media folder holds\n"
        "- /waggle media clean: shows what it would clean and asks; \"/waggle media clean confirm\" cleans without asking\n"
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
        if q.get("seven_day_pct") is not None:
            line = f"- Claude 7-day limit: {q['seven_day_pct']:g}% used"
            if q.get("seven_day_resets_at"):
                line += f", resets about {_when(q['seven_day_resets_at'])}"
            if q.get("seven_day_stop_at"):
                line += f"; background work pauses at {q['seven_day_stop_at']:.0%}"
            lines.append(line)
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


def _ago(ts):
    secs = max(0, time.time() - float(ts))
    if secs < 3600:
        return f"{max(1, int(secs // 60))} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} days ago"


def _snapshot():
    """What a clean would touch, as the arguments its approval is filed under: a changed folder is a new question."""
    from . import media_intake
    lst = media_intake.listing()
    return lst, {"files": lst["files"], "bytes": lst["bytes"], "newest": lst["newest"] or 0}


def media_clean_preview(chat, settings):
    """The first run of `/waggle media clean`: what it would clean, nothing deleted. In an engine chat the question
    is filed as an approval (policy.request_approval, session = the chat) so "yes" or "approve CODE" answers it."""
    from . import media_intake
    lst, snap = _snapshot()
    if not lst["files"]:
        return "There are no saved attachments to clean."
    days = media_intake.TRASH_KEEP_DAYS
    text = (f"Cleaning would move {lst['files']} saved attachment(s), {_size(lst['bytes'])}, to the trash folder "
            f"(oldest {_ago(lst['oldest'])}, newest {_ago(lst['newest'])}). Nothing has been deleted yet. "
            f"They stay in the trash for {days} days before they are erased.")
    if not chat:
        return text + "\nTo go ahead, send \"/waggle media clean confirm\"."
    from . import policy
    aid = policy.request_approval(MEDIA_CLEAN_TOOL, snap, "the owner asked to clean the saved attachments", session=chat)
    code = (policy.get(aid) or {}).get("code", "")
    return (text + f"\nReply \"yes\" or \"approve {code}\" to clean them, or \"deny {code}\" to leave them. "
            "(\"/waggle media clean confirm\" also works.)")


def media_clean_run(upto=None):
    """Move the saved attachments to the trash (only those saved at or before `upto`, when given); the reply."""
    from . import media_intake
    res = media_intake.trash(upto=upto)
    if not res["moved"]:
        return "There were no saved attachments to clean."
    return (f"Moved {res['moved']} saved attachment(s), {_size(res['bytes'])}, to the trash folder {res['where']}. "
            f"They are erased for good after {media_intake.TRASH_KEEP_DAYS} days; copy one out of that folder to keep it.")


def _forget_pending(chat):
    """The owner answered the question another way: close the media-clean approvals still open in this chat."""
    from . import policy
    for p in policy.pending(chat):
        if p.get("tool") == MEDIA_CLEAN_TOOL:
            policy.deny(p["id"])


def media_clean_answer(item, chat, settings, approve=True):
    """The owner's yes or no to a pending media-clean approval (called from chat_engine.approval_preflight).
    A yes runs the clean only when the folder is as it was when the question was asked and moves only what was
    there then; a folder that changed gets a new question instead. Returns the reply."""
    from . import policy
    if not approve:
        policy.deny(item["id"])
        return "Left your saved attachments alone."
    _, snap = _snapshot()
    if policy._approval_id(MEDIA_CLEAN_TOOL, snap, chat) != item["id"]:
        policy.deny(item["id"])
        return "The saved attachments changed since I asked, so nothing was cleaned.\n" + media_clean_preview(chat, settings)
    if not policy.grant(item["id"]):
        return "That question is no longer open. Nothing was cleaned."
    return media_clean_run(upto=snap["newest"])


def _media(settings, args, chat="", origin="user"):
    from . import media_intake
    words = args.lower().split()
    if words and words[0] in CLEAN_WORDS:
        rest = words[1:]
        if not rest:
            return media_clean_preview(chat, settings)
        if rest[0] in CONFIRM_WORDS and len(rest) == 1:  # the owner's own explicit instruction: it counts as the yes
            if origin not in OWNER_ORIGINS:
                return "Only the owner's own message can clean the saved attachments."
            if chat:
                _forget_pending(chat)
            return media_clean_run()
        return "Use \"/waggle media clean\" to see what it would clean, or \"/waggle media clean confirm\" to clean."
    u = media_intake.usage()
    return (f"Saved attachments: {u['files']} files, {_size(u['bytes'])}.\n"
            f"Photos, videos, files, locations and audio you send are kept {media_intake.keep_days(settings):g} days, "
            f"then deleted (limit {media_intake.limit_bytes(settings) // (1024 * 1024)} MB per file). "
            "\"/waggle media clean\" shows what it would clean and asks first.")


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


def serve(text, settings=None, chat="", origin="user"):
    """The reply to one command Waggle serves. `chat` (the chat key) lets a question be asked in that chat; without
    it (the command line) only the explicit "confirm" form cleans. Never raises."""
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
            return _media(s, sub[1] if len(sub) > 1 else "", chat, origin)
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
