"""Hold unprompted messages for the owner's next natural break (Iqbal and Bailey, CHI 2008: a notification at a task
breakpoint costs less frustration and reaction time than one at once).

What is held: a text or voice message that Waggle itself decided to send (origins initiative, followup, scout,
routine), that is not urgent and that the owner did not ask for. It is written to the outbox in the `held` state
(waggle/core/outbox.py) instead of `queued`. What is never held: reminders, calendar heads-ups, mail alerts, the
brief and the evening note, replies, anything urgent or asked for, pushes and calls (they are not in HOLD_ORIGINS or
HOLD_KINDS, so a new kind of message is never held by accident).

A held row is released, one row per break, by the first of these:
  owner_message  the owner's own next message (inbox rows with origin "user"; tests and canaries never count)
  commit         a commit in a repo Waggle knows (the HEAD logs, waggle/activity.py)
  push           a push (the remote-tracking reflog says "update by push")
  session_end    a Claude Code session that was active within 10 minutes of the hold has been quiet for 3 minutes
                 (waggle/activity.py scan; Waggle's own `claude -p` runs are machine work and do not count)
  pc_return      the owner was away from the PC (locked or idle) while it was held and is back (waggle/presence.py)
  max_wait       hold_max_wait_min (90) passed: it goes out anyway
A released row becomes `queued`; meta["hold"] records released_by, released_at and waited_s, and an
"outbox.released" event is written. Quiet hours still apply: a row released at night waits for the morning.

The sender loop does the releasing (core/sender.before_send, registered by tick.register_core): release_hook runs
before each claim, and costs one small query when nothing is held. config.json "hold_for_breaks": false restores the
old behaviour (every message queued at once).

    python -m waggle.holdq status     the held rows and the last releases
"""
import json
import logging
import time
from pathlib import Path

from . import events
from .config import Settings

log = logging.getLogger("waggle.holdq")

HOLD_KINDS = ("text", "voice")
HOLD_ORIGINS = ("initiative", "followup", "scout", "routine")
SESSION_QUIET_S = 180.0      # a Claude Code session quiet this long has ended its burst
SESSION_LOOKBACK_S = 600.0   # ...if it was active this long before (or any time after) the message was held
SIGNAL_EVERY_S = 20.0        # the file and presence signals are read at most this often
REPOS_EVERY_S = 600.0
META_USED = "holdq.used"     # break ids already spent on a release (one row per break)

_last_read = 0.0
_repos_cache = {"at": 0.0, "map": {}}


def enabled(settings=None):
    s = settings or Settings.load()
    return bool(getattr(s, "hold_for_breaks", True))


def max_wait_s(settings):
    try:
        return max(60.0, float(getattr(settings, "hold_max_wait_min", 90)) * 60.0)
    except (TypeError, ValueError):
        return 90 * 60.0


def should_hold(kind, origin, *, urgent=False, asked=False, settings=None):
    """True for an unprompted, non-urgent text or voice message of a holdable origin."""
    s = settings or Settings.load()
    return bool(enabled(s) and kind in HOLD_KINDS and origin in HOLD_ORIGINS and not urgent and not asked)


def _presence_state():
    from . import presence
    return presence.state()


def new_hold(settings, now):
    """The meta["hold"] dict of a message being held now. `away` says whether the owner is away from the PC at this
    moment (locked or idle): coming back is then a break."""
    away = False
    try:
        st = _presence_state()
        away = (not st.get("at_pc")) and st.get("idle_s") is not None
    except Exception:  # noqa: BLE001 - unreadable presence: treated as "not known to be away"
        pass
    return {"held_at": now, "max_wait_s": max_wait_s(settings), "max_wait_at": now + max_wait_s(settings),
            "away": away}


# ---- the break signals -----------------------------------------------------------------------------------

def _owner_messages(since):
    from waggle.core import store
    try:
        rows = store.connect().execute("SELECT received_ts FROM inbox WHERE received_ts>? AND origin='user' "
                                       "ORDER BY received_ts", (since,)).fetchall()
    except Exception:  # noqa: BLE001 - no inbox table yet
        return []
    return [{"type": "owner_message", "ts": float(r[0]), "id": f"msg:{r[0]}", "lookback": 0.0} for r in rows]


def _repos(s, now):
    """{repo path: key} for the repos Waggle knows, without the vault and Waggle's own worktrees (machine commits)."""
    if now - _repos_cache["at"] < REPOS_EVERY_S and _repos_cache["map"]:
        return _repos_cache["map"]
    from . import activity
    skip = ("waggle-vault", "waggle_vault", "/.waggle", "autonomy-worktrees", "/proposals/")
    out = {}
    try:
        for p in activity._known_repos(s):
            low = str(p).replace("\\", "/").lower()
            if any(x in low for x in skip) or not (Path(p) / ".git").exists():
                continue
            out[str(p)] = Path(p).name.lower()
    except Exception:  # noqa: BLE001
        pass
    _repos_cache.update({"at": now, "map": out})
    return out


def push_times(repos, since):
    """Push times from the remote-tracking reflogs ("update by push"): a file read, only of logs touched since."""
    out = []
    for path in repos:
        base = Path(path) / ".git" / "logs" / "refs" / "remotes"
        if not base.is_dir():
            continue
        for log_file in base.rglob("*"):
            try:
                if not log_file.is_file() or log_file.stat().st_mtime <= since:
                    continue
                for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    head, _, msg = line.partition("\t")
                    if not msg.startswith("update by push"):
                        continue
                    parts = head.split()
                    if len(parts) >= 3 and parts[-2].isdigit() and float(parts[-2]) > since:
                        out.append((path, float(parts[-2])))
            except OSError:
                continue
    return out


def _repo_breaks(s, since, now):
    from . import activity
    repos = _repos(s, now)
    out = []
    try:
        for key, times in activity.commit_times(repos, now=now).items():
            out += [{"type": "commit", "ts": t, "id": f"commit:{key}:{int(t)}", "lookback": 0.0}
                    for t in times if t > since]
    except Exception as e:  # noqa: BLE001
        log.debug("commit times unreadable: %s", e)
    for path, t in push_times(repos, since):
        out.append({"type": "push", "ts": t, "id": f"push:{Path(path).name.lower()}:{int(t)}", "lookback": 0.0})
    return out


def _session_breaks(s, since, now):
    from . import activity
    out = []
    try:
        for d in activity.scan_sessions(s, now=now):
            last = d.get("last")
            if d.get("machine") or not last or last < since or now - last < SESSION_QUIET_S:
                continue
            out.append({"type": "session_end", "ts": last + SESSION_QUIET_S, "id": f"session:{d.get('session')}:{int(last)}",
                        "lookback": SESSION_LOOKBACK_S})
    except Exception as e:  # noqa: BLE001
        log.debug("sessions unreadable: %s", e)
    return out


def _pc_return(waiting, now, state=None):
    """Presence, per held row: note when the owner is seen away (locked or idle), and return the oldest row whose
    owner was away and is back now (a break). The other rows that were waiting for the same return are reset: one
    row per break."""
    from waggle.core import outbox
    try:
        st = state if state is not None else _presence_state()
    except Exception:  # noqa: BLE001 - unreadable presence: no break from it
        return None
    back = []
    for _, r in waiting:
        away = bool(((r["meta"] or {}).get("hold") or {}).get("away"))
        if not st.get("at_pc"):
            if st.get("idle_s") is not None and not away:
                outbox.patch_hold(r["id"], away=True)
        elif away:
            back.append(r)
    for r in back[1:]:
        outbox.patch_hold(r["id"], away=False)
    return back[0] if back else None


def breakpoints(since, now, settings):
    """Every file- and database-based break seen after `since`, oldest first: [{"type", "ts", "id", "lookback"}]."""
    out = _owner_messages(since)
    out += _repo_breaks(settings, since, now)
    out += _session_breaks(settings, since, now)
    return sorted(out, key=lambda b: b["ts"])


# ---- releasing -------------------------------------------------------------------------------------------

def _used():
    from waggle.core import store
    try:
        return list(json.loads(store.get_meta(META_USED, "[]") or "[]"))
    except ValueError:
        return []


def _release(row, by, now, s):
    from . import notify
    from waggle.core import outbox
    not_before = None
    if notify.in_quiet_hours(s, now):
        not_before = notify.quiet_ends(s, now)   # quiet hours still apply: it goes out in the morning
    if not outbox.release(row["id"], by, not_before=not_before, now=now):
        return None
    waited = round(now - float((row["meta"].get("hold") or {}).get("held_at") or row["created"] or now), 1)
    events.log("hold_release", id=row["id"], origin=row["origin"], by=by, waited_s=waited,
               quiet_deferred=not_before is not None)
    return {"id": row["id"], "by": by, "waited_s": waited, "origin": row["origin"]}


def release_pass(now=None, settings=None, *, signals=None, presence_state=None):
    """Release what is due: every held row past its maximum wait, and one row per new break. signals(since, now,
    settings) -> breaks replaces the file and database readers, presence_state (a presence.state() dict) the PC
    reading (tests). Returns [{"id", "by", "waited_s", "origin"}]."""
    global _last_read
    from waggle.core import outbox, store
    now = store.now_or(now)
    rows = outbox.held()
    if not rows:
        return []
    s = settings or Settings.load()
    out, waiting = [], []
    for r in rows:
        hold = (r["meta"] or {}).get("hold") or {}
        if now >= float(hold.get("max_wait_at") or 0):
            got = _release(r, "max_wait", now, s)
            if got:
                out.append(got)
        else:
            waiting.append((float(hold.get("held_at") or r["created"] or now), r))
    if not waiting:
        return out
    if signals is None:
        if now - _last_read < SIGNAL_EVERY_S:
            return out
        _last_read = now
    back = _pc_return(waiting, now, presence_state)
    if back is not None:
        got = _release(back, "pc_return", now, s)
        if got:
            out.append(got)
        waiting = [(h, r) for h, r in waiting if r["id"] != back["id"]]
    if not waiting:
        return out
    since = min(h for h, _ in waiting) - SESSION_LOOKBACK_S
    brks = (signals or breakpoints)(since, now, s)
    used = _used()
    for held_at, r in sorted(waiting, key=lambda x: x[1]["id"]):
        for b in brks:
            if b["id"] in used or b["ts"] <= held_at - float(b.get("lookback") or 0):
                continue
            got = _release(r, b["type"], now, s)
            if got:
                used.append(b["id"])
                out.append(got)
            break
    store.set_meta(META_USED, json.dumps(used[-60:]))
    return out


def release_hook(now=None):
    """The sender loop's hook: a small query when nothing is held, a release pass when something is."""
    from waggle.core import outbox
    if not outbox.held(1):
        return []
    return release_pass(now)


# ---- status ----------------------------------------------------------------------------------------------

def status(now=None):
    from waggle.core import outbox
    now = now or time.time()
    held = [{"id": r["id"], "origin": r["origin"], "waits_s": int(now - (r["meta"].get("hold") or {}).get("held_at", now)),
             "max_wait_in_s": int((r["meta"].get("hold") or {}).get("max_wait_at", now) - now),
             "text": (r["body"] or "")[:80]} for r in outbox.held()]
    recent = [{"time": e.get("time"), "id": e.get("id"), "by": e.get("by"), "waited_s": e.get("waited_s")}
              for e in events.read("hold_release", now - 86400)[-10:]]
    return {"enabled": enabled(), "held": held, "released_24h": recent}


def main(argv=None):
    st = status()
    print(f"hold_for_breaks={st['enabled']}  held now: {len(st['held'])}")
    for h in st["held"]:
        print(f"  #{h['id']} {h['origin']:<10} held {h['waits_s']//60} min, goes out anyway in "
              f"{max(h['max_wait_in_s'], 0)//60} min: {h['text']}")
    for r in st["released_24h"]:
        print(f"  released #{r['id']} by {r['by']} after {int(r['waited_s'] or 0)//60} min "
              f"at {time.strftime('%H:%M', time.localtime(r['time'] or 0))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
