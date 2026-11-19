# Blockers (things only the user can do)

| # | What | Why it needs you | How |
|---|---|---|---|
| B-01 | Google Calendar connector | DONE 2026-11-18: connected and detected automatically (see B-05) | — |
| B-02 | Twilio calls | Needs a Twilio phone number + your cell verified as a caller ID (account has 0 of each) | console.twilio.com → Phone Numbers → Buy (trial credit, $0) and Verified Caller IDs → add your cell. Then run `Setup Calls.bat`. |
| B-03 | Other messaging apps | You chose Telegram only | Nothing to do. If you change your mind: `hermes gateway setup` walks through Discord/Slack/WhatsApp. |
| B-04 | Live voice check | Needs your voice | Say "waggle" (or Ctrl+Alt+W, then Ctrl+B) and talk. |
| B-05 | Calendar account | The Google Calendar you connected (2026-11-18 ~21:25) looks like a **different Google account from your Gmail**, and it's empty for tomorrow | If that's not the calendar you use: claude.ai → Settings → Connectors → Google Calendar → disconnect, then connect with your main account. Waggle picks it up within a day, or run `python -m waggle.cli connectors --refresh`. |
