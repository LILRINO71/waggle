"""Notifications. Phase 1: a Windows toast + a queue Waggle mentions on your next message.
Phase 2 adds phone (Telegram/Discord) and Phase 3 adds calls; they plug in here."""
import datetime as dt
import json
import os
import subprocess
import time

from .config import Settings, data_path


def in_quiet_hours(settings=None, now=None):
    s = settings or Settings.load()
    start, end = s.quiet_hours
    h = (now or dt.datetime.now()).hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def _toast(text):
    if os.name != "nt":
        return
    safe = text.replace("'", "''")[:240]
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]"
          "|Out-Null;$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
          f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('Waggle: {safe}'))|Out-Null;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Waggle')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def send(text, urgent=False, settings=None):
    row = {"time": time.time(), "text": text, "urgent": urgent, "seen": False}
    with open(data_path("notifications.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    if urgent or not in_quiet_hours(settings):
        _toast(text)
    return row


def unseen(mark=True):
    p = data_path("notifications.jsonl")
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    new = [r for r in rows if not r.get("seen")]
    if mark and new:
        for r in rows:
            r["seen"] = True
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return new
