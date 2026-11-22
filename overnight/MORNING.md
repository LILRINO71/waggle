# Morning, 2026-11-21: what was built overnight, and how to keep or remove each piece

Everything below is live on your PC and was checked through Telegram with the test client, unless it says otherwise.
Each piece can be switched off on its own. To switch something off:

1. Open `C:\Users\hiheo\.waggle\config.json` in Notepad.
2. Add the line, for example `"gates_enabled": false,` inside the `{ }`.
3. Save, then in PowerShell run:
   `cd C:\Users\hiheo\Claude\waggle; powershell -NoProfile -ExecutionPolicy Bypass -File tools\deploy.ps1`
   (this restarts every Waggle process and checks itself).

| What it does | Keep it? Off switch |
|---|---|
| Background work checks with a cheap local test first and skips Claude calls it doesn't need | `gates_enabled`, `notify_check_enabled` |
| An engine that just failed rests a while instead of retrying | `engine_cooldowns_enabled` |
| Background answers try the local model, then Haiku, then Sonnet (cheapest that passes) | `cascade_enabled` |
| Objective and autonomy steps are judged against a written checklist | `checklist_judge` |
| Lessons: short rules added to chat turns by keyword | `python -m waggle.lessons off` (or `list` / `remove <id>`) |
| Every change Waggle makes to your vault is a git commit; `/waggle memory` lists, `/waggle memory restore <id>` undoes | `vault_git` |
| Keyword + meaning memory search (answers from notes 87% -> 97% on a vault copy) | `memory_hybrid` |
| New facts compared with old ones: add / update / replace / skip | `memory_fact_decide` |
| Non-urgent suggestions wait for a natural break (max 90 min); learns which kinds you like | `hold_for_breaks`, `outreach_ev_enabled` |
| PC voice ignores TV/room talk and its own voice; "stop" stops it; speaks sentence by sentence | `pc_voice_smart` |
| PC voice hears speech with a small neural model (Silero) instead of loudness: music and door thumps no longer start a turn, and noise no longer keeps it listening (synthetic tests; not tried on your voice yet) | `"pc_voice_vad": "energy"` |
| Background stops above 85% of the weekly Claude limit | `"seven_day_stop_at": 0` |
| Wolfram math checks | `wolfram_mcp` |
| Nightly backup of Waggle's data at 3:30 AM to `C:\Users\hiheo\WaggleBackups` (14 kept) | remove `"waggle.backup"` from `MODULES` in `waggle/core/plugins.py`, then delete the folder |
| Feeds worth watching: `python -m waggle.feedwatch check` lists new posts and releases from the FIRST blog, the FTC SDK, Ollama, faster-whisper and Hermes (remove any with `... remove NAME`); uses Hermes's `rss-feeds` skill, no model | `feedwatch_enabled`; `hermes skills uninstall rss-feeds` |
| Drop folder reads scans: scanned PDFs, .xlsx and pictures become text on this PC (MarkItDown + OCR through Ollama, in the small venv `~/.waggle-markitdown`) | `drop_ocr` (delete the venv folder to remove it) |
| Proposal builds can look up current library docs on Context7 (free, no key; only library names go out, never your code) | `proposals_context7` |
| Obsidian skills (Bases, JSON Canvas, Obsidian CLI) for the vault, installed in Hermes by deploy | `hub_skills`; `hermes skills uninstall obsidian-bases` (or json-canvas / obsidian-cli) |
| Proposal merge texts list new pyright type errors in the files a change touched (a warning, never blocks) | `proposals_pyright` |
| `python -m waggle.usage_view` shows the usage window plus tokens by 5-hour block and by session (ccusage, offline, read-only) | `ccusage_enabled` |
| Every autonomy and objective run is recorded; if the last 4 did nothing useful the evening note says so in one line (`python -m waggle.session_records` lists them) | `session_records_enabled` |
| New tools: CAD convert/measure, print estimate, notes/Office to PDF, video/image convert, GitHub portfolio audit, explain any file, FTC team stats | one `git rm` line per tool in `skills/README.md` ("Removing a tool pack") |
| Google through gogcli (pinned v0.48.1): Docs create/read/append, Drive search, Calendar list/create (no guests), Tasks list/add/done, Classroom due dates; Gmail never. Chat tools `google_*`, `python -m waggle.google` (status, docs-new, tasks, classroom-due), one extra morning-brief line, a `Google` line in doctor. Does nothing until you sign in once (`docs/GOOGLE.md`, about 10 minutes, double-click `Waggle Google Login.bat`) | `"google_gog": false` |
| Dead-man switch (needs you): a free Cloudflare Worker sends your reminders if the PC is off and says "Waggle is down" if it dies during the day (not when you switch the PC off at night). Does nothing until you do the steps in `docs/DEADMAN.md` (about 10 minutes, no card) | leave `deadman_url` empty; after setup, `npx wrangler delete` then `"deadman_url": ""` |
| ActivityWatch sees work across the whole PC (rebuild/activity): installed in `%LOCALAPPDATA%\Programs\ActivityWatch`, starts hidden at logon (task `ActivityWatch`). Every 10 min (not in quiet hours) Waggle keeps only derived facts (program, project guess, minutes, away time, salted title hash; never raw titles or URLs) and feeds them to the activity model and the repeated-work miner (`app_sequence` candidates). Details and privacy: `docs/ACTIVITY.md` | `activitywatch_enabled`; `powershell -File tools\install-activitywatch-task.ps1 -Uninstall`, then delete the folder |
| Focus blocks, off until you ask: "focus on calculus for 45 minutes" (chat, voice or `/waggle focus 45 calculus`). Every 90 s it checks the window in front; two drifts in a row (YouTube, games, anime) get one short spoken line (Telegram text if you are away), at most every 3 min; one summary line at the end. Rules first, the local model only for unclear windows. No Claude call | nothing runs unless you start a block; `/waggle focus stop`; remove `waggle.focus` from `MODULES` in `waggle/core/plugins.py` to remove it |
| Typed quick answers (rebuild/textfast): a Telegram text that is only "what time is it", "what day is it", "what are my reminders", a sum or a unit conversion is answered by code at once, never the local model; Claude sees the exchange on its next message. Slash commands, attachments and test-marked messages go the normal way | `"text_fast_path": false` |
| "Are you at the PC" now also asks ActivityWatch's AFK watcher (it counts a playing video as active), so an idle mouse during a video no longer reads as away. Only asked once the idle timer says away; it can add presence, never remove it; no ActivityWatch means the old signal alone | `activitywatch_enabled` |
| The weekly eval run loads each case's own model and loads the small voice model again afterwards (it used to leave the 22 GB model in memory, so the next voice turn paid an 8 s reload) | nothing to switch; revert the commit to undo |
| Recipes (rebuild/recipes, not deployed yet): repeated work ActivityWatch sees (for example Classroom, Docs, calculator at 4 PM on school days) is mined across days, drafted as a routine from tools Waggle really has (read-only: Classroom due, tasks, calendar), shadow-logged on paper first, then offered at most once a day (held for a natural break, welcome odds apply). "yes CODE" makes the routine, "no" / "never" drops it and teaches the odds; nothing runs before a yes. `python -m waggle.recipes list\|show\|accept\|reject\|shadow`, one evening-note and dashboard line when a suggestion is waiting. Few patterns until ActivityWatch has more days | `"recipes_enabled": false` |
| Windows-MCP (rebuild/winmcp, not deployed yet): the chat brain reads the Windows UI tree and acts on apps by screen point (Notepad test: Snapshot 0.2 s, Click 0.5 s, Type 1.6 s) instead of pixel guessing. Pinned 0.8.8 in its own venv `~/.waggle-tools/windows-mcp`, telemetry off, only App/Snapshot/WaitFor/Click/Type/Scroll/Shortcut/Clipboard, no shell. Starts with pythonw (no console window). It draws a topmost frame on screen from the first call until 15 s after the last (nothing while idle, measured), so it only runs in turns you started. Every call passes the same rules as computer_use (blocked apps, secrets, payment pages ask; the Claude window is refused). Deploy needs `guard approve` (policy.py, security.py, config.py changed) | `windows_mcp` |
| Waggle moves into your own vault (rebuild/vaultmove, NOT live until you run it): it reads all of PersonalIntelligenceVault (search, graph, your profile notes in the context and the morning brief) but writes only inside `12 - Waggle`, never touches your `.obsidian`, and its undo history (`/waggle memory`) lives in that folder. Run it once at deploy: `python tools\move_vault.py` (dry run, prints the plan), then `python tools\move_vault.py --apply`, then `python -m waggle.guard approve` and deploy. Copies WaggleVault into `12 - Waggle` with its history, backs up and edits `config.json`, repoints "Feed Waggle"; the old WaggleVault stays where it is, not renamed | `vault_own` (set to "" and `vault` back to WaggleVault); `vault_profile_notes` (list, empty = Waggle's own profile only) |

## Also fixed overnight (nothing to decide)

- **Polite replies**: "no thanks", "no worries", "no, that's fine" no longer count as corrections that Waggle tries
  to learn from ("no, that's wrong" still does). This was Waggle's own self-fix proposal 7XJP. Its first deploy
  failed the live check because my test client was still using the Telegram login, so Waggle rolled itself back,
  as it should. I fixed that clash (test runs now take turns on the login) and deployed it again (8127b88).
  You may see an old "7XJP rolled back" note in the proposals history; it is live now.

## Things only you can do (each optional)

1. **Calendar**: one-time Google login, steps in `overnight/USER_TESTS.md` (Calendar section). Until then the
   calendar watch stays quiet.
   **Google (Docs, Tasks, Classroom, calendar writes)**: the different, bigger login in `docs/GOOGLE.md`; it is what lets
   Waggle create a Google Doc, which the claude.ai Drive connector cannot.
2. **Phone calls**: a free Twilio trial, then double-click `Setup Calls.bat`. Skip it if texts are enough.
3. **PC voice**: say "Waggle, what time is it?" with the TV on, then with music playing; say "stop" while it
   talks; pause to think mid-sentence once. Tell me if it reacts to the TV, misses you, or cuts you off.
4. **Money goal**: most freelance and payment platforms need you to be 18+ or use a parent's account. The plan is in
   `WaggleVault/Goals/money/plan-shortlist.md`; nothing was signed up for and $0 was spent.
5. **Disk space (optional)**: models no longer needed:
   `ollama rm qwen3.8:27b` (17 GB, lost the comparison), `ollama rm embeddinggemma:300m` and
   `ollama rm qwen3-embedding:0.6b` (lost the embedding test; Waggle keeps using nomic-embed-text).
6. **Licence**: the waggle repo has no licence. MIT is the usual pick for a portfolio; say "add MIT" and I will.
7. **Chrome extension for ActivityWatch** (optional, 2 minutes): so Waggle can tell Onshape from YouTube in the same
   Chrome. Steps in `docs/ACTIVITY.md` ("Your steps"). Without it everything still works with program names and tab
   titles.

## Research reports

`C:\Users\hiheo\Claude\reports\`: open-source-jarvis-2026-11.md, tools-mcp-skills-2026-11.md,
research-for-waggle-2026-11.md, adopt-vs-build-2026-11.md. Short versions are in the vault under `Sources/2026-11/`.
