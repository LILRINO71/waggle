# Dead-man switch: reminders and a "Waggle is down" alert while the PC is off

A tiny program runs for free on Cloudflare's servers. Waggle tells it every 5 minutes "I'm alive" and hands it the
reminders due in the next 48 hours. If the PC is off or Waggle has crashed:

- a reminder that comes due still reaches you, on Telegram and in the ntfy app, about 3 to 8 minutes late;
- if Waggle's last heartbeat came between 07:00 and 22:30 (Los Angeles time) and then 20 minutes pass with no
  heartbeat, you get one message the same day: "Waggle is down since 14:05". Not repeated. A PC you switch off for the
  night never triggers it, and neither does an outage that runs through the night;
- when it comes back after an outage longer than an hour, one "Waggle is back" message. Shorter outages: nothing.

Nothing is ever sent twice. Waggle tells the Worker the moment it delivers a reminder, and the Worker waits 3 minutes
past the due time before it acts. A reminder the Worker already sent is marked done on the PC when it starts again.

Cost: nothing. No card. Free plan limits (5 timers, 100,000 requests a day, 1,000 KV writes a day) are far above
what this uses (about 600 requests and 300 writes a day).

## What the Worker holds

Only: the time of the last heartbeat, and for each reminder in the next 48 hours its id, due time and its words (cut
to 200 characters). Nothing else about you. If you do not want the words on Cloudflare, put
`"deadman_hide_text": true` in `C:\Users\hiheo\.waggle\config.json`; the Worker then sends "Reminder due" without
the words. The Telegram bot token, your chat id, the ntfy topic and the shared password are stored as Cloudflare
secrets (write-only) and are never in the repo or on screen.

## Your steps (about 10 minutes)

1. **Make the free Cloudflare account.** Open https://dash.cloudflare.com/sign-up , enter an email and a password,
   confirm the email. No card is asked for. Skip any "add a website" offers; you only need the account.
2. **Log in from PowerShell.** Run:

   `cd C:\Users\hiheo\Claude\waggle\cloud\deadman`
   `npx wrangler login`

   A browser opens. Click Allow. (The first `npx` run downloads wrangler, about a minute.)
3. **Run the setup script.** In the same PowerShell:

   `powershell -NoProfile -ExecutionPolicy Bypass -File ..\..\tools\deadman-setup.ps1`

   It makes the shared password, creates the free storage, deploys the Worker, puts the four secrets in without
   showing them, writes `deadman_url` into Waggle's config, sends you a test message (Telegram and ntfy) and does the
   first heartbeat. The first time, Cloudflare may ask you to choose a name for your free `workers.dev` address: type
   any short name. If the script stops, it says why in red; run it again after fixing it, it is safe to repeat.
4. **Check it.** Run `python -m waggle.deadman status`. You want: `Set up: yes`, `Last beat: ... ago`, and
   `Telegram ok, ntfy ok`.
5. **Make sure the running Waggle has the new part.** If you did not already, run
   `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\hiheo\Claude\waggle\tools\deploy.ps1`.
   After that the heartbeat runs by itself every 5 minutes. (Setting `deadman_url` later needs no restart.)

## Test it (about 20 minutes)

1. Tell Waggle on Telegram: "remind me in 10 minutes to test the backup".
2. Send the reminder to the Worker now instead of waiting for the next heartbeat:
   `python -m waggle.deadman beat`, then `python -m waggle.deadman status`. It should say `1 reminder(s) queued`.
3. Switch Waggle off for half an hour: `python -m waggle.core off --for 30m`.
4. Wait. About 13 to 18 minutes after you set the reminder, "⏰ test the backup" arrives on Telegram and in the ntfy
   app with a note that it was sent by the backup service. Waggle never sent it, the Worker did.
5. About 20 minutes after the last heartbeat, "Waggle is down since HH:MM" arrives (only if that heartbeat and the
   20 minutes are both between 07:00 and 22:30).
6. After 30 minutes Waggle switches itself back on. No second copy of the reminder arrives, and no "back" message
   (the outage was under an hour).

If step 4 never arrives: `python -m waggle.deadman status` (is the Worker queueing it?), and in
`cloud\deadman` run `npx wrangler tail -c wrangler.deploy.toml` while the next 5-minute mark passes.

## Good to know

- A reminder you create and then lose the PC within 5 minutes is not at the Worker yet (the heartbeat carries it
  next time). `python -m waggle.deadman beat` sends it at once.
- Reminders go out whenever the PC is silent, day or night. Only the "down" alert and the "back" note follow the
  waking-hours rule. To move the window, change `WAKE_START` and `WAKE_END` in `cloud\deadman\wrangler.toml` and run
  the setup script again.
- Mail alerts are not part of this (reading mail does not fit in the free plan's 10 ms of CPU).
- Turn it off: first remove the Worker (`npx wrangler delete -c wrangler.deploy.toml` in `cloud\deadman`), then set
  `"deadman_url": ""` in config.json. Do it in that order: a Worker that stops hearing the PC would otherwise send
  the reminders the PC is also delivering.
- Change the shared password: `python -m waggle.deadman setup --new-token`, then run the setup script again.
- Developer notes: the Worker's decisions are plain functions in `cloud\deadman\src\logic.js`; tests run with
  `node --test test/*.test.js` in that folder (Node's own runner, nothing to install).
