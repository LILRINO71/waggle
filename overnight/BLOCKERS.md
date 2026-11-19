# Blockers (things only the user can do)

| # | What | Why it needs you | How |
|---|---|---|---|
| B-01 | Google Calendar (and Tasks, if listed) connector | OAuth sign-in | claude.ai → Settings → Connectors → Google Calendar → Connect. Waggle refreshes its connector list daily, or run `python -m waggle.cli connectors --refresh`. |
| B-02 | Twilio calls | Needs a Twilio phone number + your cell verified as a caller ID (account has 0 of each) | console.twilio.com → Phone Numbers → Buy (trial credit, $0) and Verified Caller IDs → add your cell. Then run `Setup Calls.bat`. |
| B-03 | Other messaging apps | You chose Telegram only | Nothing to do. If you change your mind: `hermes gateway setup` walks through Discord/Slack/WhatsApp. |
| B-04 | Live voice check | Needs your voice | Say "waggle" (or Ctrl+Alt+W, then Ctrl+B) and talk. |
