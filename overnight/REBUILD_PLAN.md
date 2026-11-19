# Rebuild plan (2026-11-19): fix the causes, not the symptoms

After the first real use the user's verdict was: dumb, slow, never reaches out, doesn't talk back, learns
nothing. The docs and 290 tests said everything worked. Four read-only audits of the live system (logs, traces,
Hermes state, the vault) found why both were true at once.

## What the audits measured

| Area | Finding |
|---|---|
| Who answers | 11 of 16 real Telegram replies were written by the local 4B model, not Claude |
| Lost work | Claude runs inside Hermes's `pre_llm_call` hook (30 s limit). Over the limit, Hermes lets the 4B answer, drops Claude's finished reply, and skips the hook for 60 s. One answer was lost at 31.2 s |
| Tasks | About 3 of 13 real task requests got done; 0 of 4 tool-agent jobs were delivered as asked |
| Speed | Starting `claude -p` costs ~1 s; first words are possible in 1-1.5 s. Waggle waits for the whole answer inside JSON and registers `hold_stream=True`, so nothing shows for 3.5-14.5 s |
| Outreach | 1 unprompted message delivered in 3 days, 1 lost; 64 gateway start/stop notices. Only `critical` findings push; every watcher reads sources that are always empty |
| Calls | Never possible: the Twilio trial has no number and no verified caller ID; `call.configured()` only checks that strings exist |
| Voice | 4 voice notes, 0 real spoken answers. The reply text is the written answer read aloud (21-125 s of audio) |
| Knowledge | 0 facts learned from real conversations. The vault's useful content is a one-time hand seed. Retrieval always injects 6 chunks (floor 0.45 is below embedding noise), often Waggle's own route log |
| Learning | The loop is weekly and has never run by itself; it waits for explicit corrections (0 of 75 traces have any) |
| Blind spot | traces.db does not record turns the 4B answered or hook timeouts; tests and `hermes chat -q` never touch the Telegram path |

## Root causes

1. **Claude is a guest inside the local model's turn.** Hermes runs the 4B as the agent; Waggle intercepts a hook,
   calls Claude synchronously and swaps the output. Timeouts, two transcripts ("[Message withheld]"), no
   streaming and lost results all follow from that.
2. **Claude is called as a stateless, tool-less one-shot** that must return finished JSON, behind regexes that
   route the message before any model reads it.
3. **Waggle filters interruptions instead of taking initiative.** It watches nothing from the user's real life,
   only pushes `critical`, sends once with no retry, and never follows up on what the user said.
4. **Nothing flows in and nothing is distilled.** No ingestion from the user's notes, repos, mail or calendar;
   memory is truncated transcript snippets; learning looks at single messages with the 4B.
5. **Voice is the chat path with audio bolted on,** so it inherits every failure above and speaks essays.
6. **Verification never touched the real path,** and failures left no record.

## Workstreams

- **0 Stopgap (config only):** raise Hermes's hook timeout so Claude always answers, queue instead of interrupt,
  silence start/stop notices.
- **A Turn engine:** the hook returns in ~1 s; Waggle delivers the reply itself, streamed into the Telegram
  message; one long-lived Claude process per chat with Waggle's tools so it answers or acts in one call; heavy
  jobs run in the background and report back; the 4B answers only when Claude is unavailable; one transcript;
  every turn records who really answered, time to first words and delivery.
- **B Knowledge:** an ingest job (the user's Claude Code memory notes, repo READMEs and commits, GitHub,
  calendar and mail into the vault); clean retrieval; daily conversation digests and outcome capture; a fact
  model with dates and expiry; a daily learning loop with implicit signals, a knowledge quiz and a visible
  "Today I learned".
- **C Initiative:** an outbox with retries; fact watchers on real sources; a reasoning step that decides what is
  worth telling the user and how (text, voice note, call, evening note) under a daily cap and quiet hours;
  follow-ups on what the user said they would do; a daily evening note; honest call status.
- **D Voice (after A):** a short spoken answer plus the full text for voice notes, sent by Waggle itself.
- **E Automation scout (after B and C):** notice repeated or automatable work in what the user is doing, build
  the automation through the objectives engine, report it.

## Done means (measured on real Telegram turns, shown in `waggle doctor` and the Command Center)

- At least 95 % of turns answered by Claude while quota allows; 0 lost answers.
- Median time to first words at most 2 s for chat turns.
- Every voice note gets a spoken reply of at most 3 sentences plus the text.
- At least one useful unprompted message a day chosen by reasoning; 0 lost deliveries; an evening note daily.
- The vault grows every day from real sources; "Today I learned" has real entries; the quiz score is tracked.
- Every fallback, timeout and failed delivery is recorded and visible.
