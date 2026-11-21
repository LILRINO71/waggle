# Hands-free Waggle: say "Hey Siri, Waggle", speak, hear the answer

**Read this first: I did not test any of the phone steps.** Nothing below that involves Tailscale, the
iPhone, the Shortcuts app or Siri was tried by me: steps 1, 2, 3, 5 and 6, including the command in step 3, are
written from Tailscale's and Apple's documented behaviour, not from running them. What was tested is the PC side only: the
`/voice` entry on the core, its token, text in and audio out, the Telegram copy, and the `voice-setup` command (see
`tests/test_voiceline.py`, `tests/test_voiceline_core.py` and one real run without a phone). If a step does not
look the way it is described here, the step is wrong, not you.

## What this is

Telegram cannot play a voice note by itself. This gives you a second way in: a Shortcut on your iPhone listens to
you, sends the words to your PC over your private Tailscale network, and plays Waggle's spoken answer straight
away. The same answer is also sent to your Telegram chat as text, so you have a written record. It is the same
conversation as your Telegram chat, so Waggle remembers what you said there and here.

Only your own devices can reach it: the PC publishes it to your private Tailscale network, never to the public
internet, and every request needs a secret token.

## Steps

Do these once. The PC must be on, and Waggle's core running, whenever you use it.

### 1. Install Tailscale on the PC and on the iPhone (free)

1. On the PC, install the Tailscale app from tailscale.com/download and sign in.
2. On the iPhone, install the Tailscale app from the App Store and sign in **to the same account**.
3. Switch it on on both. The Tailscale app on the iPhone should list your PC as a device.

### 2. Turn on HTTPS for your private network

1. On the PC or the iPhone, open the Tailscale admin page in a browser: login.tailscale.com/admin/dns
2. Under **HTTPS Certificates**, press **Enable HTTPS** and accept.
3. Note your PC's full private name on the same page or in the Tailscale app (**This device**). It looks like
   `my-pc.tail1234.ts.net`. Below it is written `<pc-name>`.

### 3. Publish Waggle's port to your private network only (one command on the PC)

Open PowerShell on the PC and run (8896 is Waggle's core port; if you changed `core_port` in
`~/.waggle/config.json`, use that number):

```
tailscale serve --bg http://127.0.0.1:8896
```

If PowerShell does not know `tailscale`, use the full path:
`& "C:\Program Files\Tailscale\tailscale.exe" serve --bg http://127.0.0.1:8896`.

If it prints a link and says Serve is not enabled, open the link, enable it, and run the command again.
Check it with `tailscale serve status`: it should show `https://<pc-name>` pointing at `http://127.0.0.1:8896`,
marked "tailnet only". **Never use `tailscale funnel`**: that would publish it to the whole internet.
To switch it off again: `tailscale serve reset`.

### 4. Get the two values to copy

On the PC, in a terminal on your own screen (do not paste the output anywhere public):

```
python -m waggle.core voice-setup
```

It prints two lines:

```
URL path: /voice
Token:    <a long random string>
```

Your Shortcut's address is `https://<pc-name>/voice`, for example `https://my-pc.tail1234.ts.net/voice`.
The token is like a password for this entry. It lives in `~/.waggle/voice.token`. Nothing else prints it, and Waggle
never logs it. If you ever want a new one, delete that file, restart the core (`python -m waggle.core restart`) and
run `voice-setup` again; then update the Shortcut.

Send yourself the two values some safe way (for example Messages to yourself or a note) so you can paste them on
the iPhone.

### 5. Build the Shortcut called "Waggle" on the iPhone

Open the **Shortcuts** app, press **+**, and name it exactly `Waggle` (Siri uses the name). Add these actions in
this order:

| # | Action | Set it to |
|---|--------|-----------|
| 1 | **Dictate Text** | Language: your language. Stop Listening: **After Pause**. |
| 2 | **Get Contents of URL** | URL: `https://<pc-name>/voice` (your own name from step 2). Tap **Show More**: Method **POST**. Headers, **Add new header**: key `Authorization`, text `Bearer ` (the word Bearer, a space) followed by the token from step 4 pasted right after the space. Request Body: **JSON**. **Add new field**: type **Text**, key `text`, value: the **Dictated Text** variable from action 1 (tap the value, choose the variable). |
| 3 | **Play Sound** | Input: the **Contents of URL** result of action 2. |

Header summary: `Authorization` = `Bearer <your token>`. Choosing the JSON request body makes the Shortcut send
`Content-Type: application/json` by itself.

**Optional, for a back-and-forth conversation:** add a **Repeat** action (set it to 5) and drag actions 1 to 3 inside
it, so after each answer it listens again. To stop early, say nothing: an empty dictation is refused by Waggle, and
you can stop the Shortcut from its own button. (Adding an **If** around actions 2 and 3 with "Dictated Text has any
value" makes it end quietly.)

Try it once by pressing the Shortcut's play button, say "remind me to call Sam in ten minutes", and listen.

### 6. Say "Hey Siri, Waggle"

Siri runs a Shortcut by its name. Say **"Hey Siri, Waggle"**, wait for the listening cue, then speak. Waggle's
answer plays, and the same answer arrives in Telegram as text, starting with a line `Heard: "..."` that shows what it
understood.

## What you will hear and see

- Normal: a short spoken answer (a few sentences at most). The full written answer is in Telegram.
- A long job (more than 25 seconds): you hear "Working on it, sir. I will message you when it is done." The job
  carries on, and the answer comes to Telegram when it is finished.
- If Waggle could not answer: you hear a short apology instead of silence.

## If something goes wrong

The Shortcut's result shows a short message from Waggle. By code:

| Shows | Meaning | Fix |
|-------|---------|-----|
| 401 unauthorized | The token is wrong or missing | Check the `Authorization` header says `Bearer `, a space, then the exact token; no line breaks. |
| 429 another voice request is running | A previous request has not finished | Wait for it, then try again. |
| 413 | The body is over 2 MB | Only possible if you send audio; dictated text is tiny. |
| 503 | The core is stopping, or no chat is set up | Check `python -m waggle.core status`; `engine_chats` must be set in `~/.waggle/config.json`. |
| Cannot connect / timed out | Tailscale is off, the PC is asleep, or step 3 was not run | Open Tailscale on the iPhone, check `tailscale serve status` on the PC. |

## Details for the curious

- Waggle also takes audio: send the recording as the request body with its audio type (`audio/mp4`, `audio/wav`, ...,
  at most 2 MB) and Waggle transcribes it itself.
- Add `?reply=json` to the address to get `{"spoken": "...", "text": "..."}` instead of audio.
- Settings in `~/.waggle/config.json`, all optional: `voice_line_telegram_copy` (true: the written answer is also
  sent to Telegram), `voice_line_chat` (the chat to talk in, such as `telegram:123456`; by default your engine chat),
  `voice_line_hold_s` (seconds before the "Working on it" line, 25).
- The spoken answer is M4A (AAC) made with ffmpeg; when ffmpeg is missing it is a WAV file, which also plays.
- Because the words arrive already dictated by the iPhone, Waggle has no confidence numbers for them, so it treats
  them as "not confidently heard" (`stt_confident` is false for this line): any check that repeats a risky request
  back to you before acting will apply to every request made here.
