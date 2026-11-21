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
| New tools: CAD convert/measure, print estimate, notes/Office to PDF, video/image convert, GitHub portfolio audit, explain any file, FTC team stats | one `git rm` line per tool in `skills/README.md` ("Removing a tool pack") |

## Also fixed overnight (nothing to decide)

- **Polite replies**: "no thanks", "no worries", "no, that's fine" no longer count as corrections that Waggle tries
  to learn from ("no, that's wrong" still does). This was Waggle's own self-fix proposal 7XJP. Its first deploy
  failed the live check because my test client was still using the Telegram login, so Waggle rolled itself back,
  as it should. I fixed that clash (test runs now take turns on the login) and deployed it again (8127b88).
  You may see an old "7XJP rolled back" note in the proposals history; it is live now.

## Things only you can do (each optional)

1. **Calendar**: one-time Google login, steps in `overnight/USER_TESTS.md` (Calendar section). Until then the
   calendar watch stays quiet.
2. **Phone calls**: a free Twilio trial, then double-click `Setup Calls.bat`. Skip it if texts are enough.
3. **PC voice**: say "Waggle, what time is it?" with the TV on, then with music playing; say "stop" while it
   talks; pause to think mid-sentence once. Tell me if it reacts to the TV, misses you, or cuts you off.
4. **Money goal**: most freelance and payment platforms need you to be 18+ or use a parent's account. The plan is in
   `WaggleVault/Goals/money/plan-shortlist.md`; nothing was signed up for and $0 was spent.
5. **Disk space (optional)**: models no longer needed:
   `ollama rm qwen3.8:27b` (17 GB, lost the comparison), `ollama rm embeddinggemma:300m` and
   `ollama rm qwen3-embedding:0.6b` (lost the embedding test; Waggle keeps using nomic-embed-text).
6. **Licence**: the waggle repo has no licence. MIT is the usual pick for a portfolio; say "add MIT" and I will.

## Research reports

`C:\Users\hiheo\Claude\reports\`: open-source-jarvis-2026-11.md, tools-mcp-skills-2026-11.md,
research-for-waggle-2026-11.md, adopt-vs-build-2026-11.md. Short versions are in the vault under `Sources/2026-11/`.
