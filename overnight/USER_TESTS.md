# Tests only the user can do

A step counts as proven only when the real check below has passed on the real phone. Passing automated tests
is not enough. After each test, say "sent" in the main chat: the logs show who answered and how long it took,
which cannot be seen from the phone.

## Now (stopgap config is live since 2026-11-19 12:08)

| # | Do this in Telegram | Pass | Fail |
|---|---|---|---|
| 1 | Type a question that needs real knowledge, e.g. "What are my three most recent GitHub repos and what is each one for?" | A specific, correct answer within about 2 minutes | A vague or generic answer, or nothing |
| 2 | Send a question, and while it is still working send "also, what day is it?" | Both are answered, in order | The first answer is dropped or cut off |
| 3 | Ask for something slow, e.g. "Read through my waggle repo and name the three riskiest files" | A real answer, even if it takes a few minutes | A quick shallow answer (the small local model stepped in) |

## Calendar (one-time, about 2 minutes) — added 2026-11-20

Waggle reads your Google Calendar through its private address, with no Claude use. It texts you 30 minutes
before each timed event and puts today's events in the morning brief.

1. Open https://calendar.google.com on the PC, signed in to your Google account.
2. On the left, under "My calendars", point at your main calendar, click the three dots, then "Settings and sharing".
3. Scroll to "Integrate calendar" and find "Secret address in iCal format". Click the copy icon. Don't share it.
4. Open PowerShell and run: `cd C:\Users\hiheo\Claude\waggle`
5. Run: `python -m waggle.calendar_watch login`
6. Press Enter to accept the name "personal".
7. Right-click to paste the address, then press Enter. Nothing shows while you paste; that is normal.
8. It should print "ok: read calendar [personal], N events in the next 14 days".
9. Check: `python -m waggle.calendar_watch today` lists today's events. Say "calendar done" in the main chat.

If you ever reset the secret address in Google, run step 5 again.

## One-time setup only the user can do

| # | What | Why |
|---|---|---|
| 4 | At my.telegram.org open "API development tools", create an app, and keep the API ID and hash to yourself. Create a Telegram group "Waggle Lab" and add the Waggle bot to it. | Lets a test client send real messages as you, so every deploy is checked end to end. The login script arrives with step 5 and asks for the values in the terminal. Never paste them into a chat. |
| 5 | Optional: in the Twilio console claim the free trial number and verify your mobile number, then run `Setup Calls.bat`. Do not upgrade the account. | Without it, phone calls stay "not ready". |
| 6 | Close and delete the old "Waggle autonomous mission 3" session from the sidebar. | Its goals are archived; it only causes confusion. |

## As each piece goes live (announced in the main chat)

| Step | Do this | Pass |
|---|---|---|
| 1 Core running | Nothing. Proven by killing the core and watching it return. | |
| 2 Messages sent by the core | A message "Core delivery test" arrives. Reply "got it". | It arrives once, not twice |
| 4 Streaming answers | Send any question | First words within about 3 seconds, and the message keeps growing |
| 7 Evening note and undo | Reply `undo <code>` to one evening note | Waggle confirms the change was reverted |
| 8 Reminder | "Remind me in 2 minutes to stand up" | It arrives 2 minutes later, give or take 30 seconds |
| 8 Catch-up | Set a reminder for a time when the PC will be off | It arrives at the next login, marked late |
| 8 Unprompted messages | For three days, answer each message Waggle starts with a thumbs up or down | At least one useful message a day, none lost |
| 9 Voice | Send a 5-second voice note | A spoken reply under 20 seconds, plus the text |
| Calls (needs test 5) | Text "call me" | The phone rings |
| 12 Automation | Waggle proposes one after three trial runs | You switch it on, and it gets used |
| 13 Code change | Waggle proposes a fix to itself | You approve one merge |
