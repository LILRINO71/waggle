# What you work on, across the whole PC (ActivityWatch) and focus blocks

Waggle used to see only your Claude Code sessions, git commits and chats. With **ActivityWatch** it also sees which
program and which kind of page you have in front, and for how long, so work in Onshape, Obsidian, school sites, the
laser software and so on counts as work on that project. On top of that, **focus blocks** let you say "focus on
calculus for 45 minutes" and Waggle quietly speaks up if you drift to YouTube, games or anime.

Nothing here calls Claude. Nothing leaves the PC.

## What is installed

| Piece | Where | Notes |
|---|---|---|
| ActivityWatch 0.14.1 (portable) | `%LOCALAPPDATA%\Programs\ActivityWatch` | `aw-qt.exe` starts the server (127.0.0.1:5600, this PC only) and two watchers: the active window and AFK (away from keyboard). MPL-2.0 licence, from github.com/ActivityWatch/activitywatch (release v0.14.1, October 2026; the zip's SHA-256 matched the one GitHub published). |
| Start at logon | Task Scheduler task `ActivityWatch` | Runs `~\.waggle\ActivityWatch Launcher.vbs`, hidden. Only a tray icon shows. It is a scheduled task, not a Startup-folder shortcut, because the Claude app cannot write a Startup shortcut that really runs. Made by `tools\install-activitywatch-task.ps1` (`-Uninstall` removes it). |
| Its data | `%LOCALAPPDATA%\activitywatch` | ActivityWatch's own database. It keeps raw window titles and page addresses there, as it always does. That is its job; Waggle never copies them. |
| Waggle's sensor | `waggle/aw_sensor.py` | Core timer `activitywatch.poll` every 10 minutes, off in quiet hours. Switch: `"activitywatch_enabled": false` in `~\.waggle\config.json`. Without ActivityWatch running it does nothing, quietly. |
| Focus blocks | `waggle/focus.py` | Exist only while you run a block. |

## Privacy

- ActivityWatch's raw titles and URLs stay in ActivityWatch. Waggle reads them in memory, keeps only derived facts
  (program, project guess such as ftc / waggle / school / onshape, minutes, away time, a **salted hash** of the title
  and the site's host only when it matched a known category) in `autonomy.db`, and forgets rows after 30 days.
- Raw titles and URLs are never logged, never written to events or notes, never sent to Claude.
- The only place a window title travels is the focus check's question to the **local** model on this PC (Ollama), and
  only for windows the rules cannot place.
- Leisure kinds (video, social, anime, games) are stored as a category and count for nothing in "what you work on".
- Pause it any time from ActivityWatch's tray icon (right click, Quit) or by setting `activitywatch_enabled` to false.

## Your steps: the browser extension (Chrome)

Without it, ActivityWatch sees "chrome" and the tab title but not the site. With it, Waggle can tell Onshape from
YouTube in the same Chrome. I did not install it; it is a browser add-on, so it is yours to add.

1. Make sure ActivityWatch is running (a small icon in the tray; or open `http://localhost:5600` in the browser, you
   should see its web page).
2. In Chrome, open the ActivityWatch Web Watcher page on the Chrome Web Store (search "ActivityWatch Web Watcher",
   or follow the Chrome link on https://docs.activitywatch.net/en/latest/watchers.html). Check that the publisher is
   ActivityWatch, then **Add to Chrome**.
3. Click the puzzle-piece icon, pin "ActivityWatch Web Watcher", click it, and check it says it is connected to
   `http://localhost:5600` (that is the default; leave it).
4. Leave "Allow in Incognito" off: private windows then stay invisible, which is what you want.
5. Check: `http://localhost:5600/#/buckets` shows a bucket named `aw-watcher-web-chrome`. Waggle picks it up on its
   own at the next 10-minute pass; nothing to configure.

If you use another browser, the same extension exists for Firefox and Edge; tell me and I will check the bucket.

## Focus blocks

Say it in chat or by voice:

- "focus on calculus for 45 minutes"  (any words: "help me concentrate on my FTC code for an hour")
- "stop the focus block" / "I'm done"
- Telegram: `/waggle focus 45 calculus`, `/waggle focus stop`, `/waggle focus` (how it is going)

What happens: every 90 seconds Waggle looks at the window in front of you through ActivityWatch. A window that
names your topic, or belongs with it (calculus accepts school sites, notes and an AI chat), is fine. A leisure site
or game is drift. Anything it cannot place goes to the local model with a 2-second limit; "unsure" counts as fine.
**Two drifts in a row** earn one short line in Waggle's dry butler manner, spoken on the PC speakers when you are at
the PC, otherwise as a Telegram text, **at most once every 3 minutes**. Away from the keyboard is never drift. At the
end you get one line: minutes on task and how many times you drifted.

A block needs ActivityWatch running; if it is not, Waggle says so instead of pretending to watch.

## When something is wrong

| Symptom | Check |
|---|---|
| Waggle's doctor says "ActivityWatch is not answering" | `schtasks /run /tn ActivityWatch`, then open `http://localhost:5600`. |
| It does not start at logon | `Get-ScheduledTask ActivityWatch` in PowerShell; re-run `tools\install-activitywatch-task.ps1`. |
| No web sites in the data | The extension step above; the bucket `aw-watcher-web-chrome` must exist. |
| A focus nudge was wrong | Say so in chat. Rules and the local model are the only judges; a wrong "drift" is a rule or model miss, not a Claude decision. |

Live checks (read-only):

```
curl http://localhost:5600/api/0/buckets/
python -m waggle.aw_sensor        # one pass: counts only, never titles
python -m waggle.focus 5 calculus # start a 5-minute block (then: python -m waggle.focus stop)
```
