# Talking to Waggle at the PC

> **Not tested with a real person speaking.** Everything below was checked on the PC with synthesised speech:
> a request made by the speech server and fed through the recording path in place of the microphone, and
> sentences played through the speakers and picked up by the webcam microphone. Nobody has yet spoken to it
> live, so how well it hears your own voice at your desk, how loud you need to be, and whether 1.2 s of silence
> feels right are not known yet. Please try it and say what was wrong.

At the PC you can talk to Waggle without touching your phone, and it answers through the PC speakers by itself.
Telegram stays as it is for when you are away, and every spoken answer is also written there (silently, so your
phone does not buzz while you sit at the PC).

## Talking to it

**The hotkey: Ctrl + Alt + ` (the key under Esc).**

1. Press it. A short rising beep means Waggle is listening.
2. Say what you want. Stop talking; after about a second of silence a short lower beep means it heard you.
   (Pressing the hotkey again while you talk sends it at once.)
3. Waggle answers out loud. It is the same conversation as Telegram, so "what did I just ask you?" works.
4. After it finishes, it keeps listening for about 6 seconds: just keep talking for a follow-up, no hotkey.
   Say nothing and the conversation ends.
5. To interrupt Waggle while it talks, press the hotkey: it stops at once and listens.

If Waggle's core is not running or the request fails, you hear a short sentence saying so (for example "I can't
reach Waggle's core right now"), never silence. Long jobs get "Working on it, sir"; the answer is spoken later
if you are still at the PC, and written in Telegram either way.

Ctrl + Alt + Space was planned, but another program on this PC already owns that combination (most likely
the Claude desktop app's quick-entry shortcut), so Windows refuses it to Waggle.

## The wake word

**On.** Just say **"Waggle"** first, then what you want: "Waggle, remind me to stretch in twenty minutes" or
"Hey Waggle, what's on today?". Say only "Waggle" and it answers "Sir?" and listens. The hotkey works as well.

It was switched on only after a measurement on this PC (below): no false trigger in ten minutes of the room and
of played-back talk, and it costs almost nothing. It has not yet heard *your* voice, so if it misses you or wakes
up when it should not, say so, or switch it off (`"pc_voice_wake": false`).

To change the name (for example to "Jarvis"), put this in `config.json` in the Waggle home and restart the PC
voice (`python -m waggle.pcvoice stop`; it starts again at the next logon, or start the scheduled task):

```json
"pc_voice_wake_names": ["Jarvis"]
```

Say the name first: "Jarvis, what's on my calendar?" or "Hey Jarvis ...". Only the name: Waggle says "Sir?" and
listens. The name must start what you say; "I told Jarvis ..." does not count. A short, unusual name works best.
After a change, measure it again (`python tools/pcvoice_measure.py --names Jarvis --sanity`).

What happens to what the microphone hears while the wake word is on: each burst of sound is cut out by its
loudness and turned into words by Waggle's own recogniser on this PC (nothing leaves the PC for this). If the words
do not begin with the name, they are dropped at once: the sound exists on disk only for the moment the recogniser
reads it (then it is deleted), and the words are never logged or stored. Windows shows its microphone-in-use icon the whole time the wake word is on.

## Waggle speaking first

When Waggle has something for you on its own (a reminder, the morning brief, a finished background job,
something it noticed) and you are at the PC, it says a short version out loud and the full message arrives in
Telegram without a notification sound. "At the PC" means: the screen is not locked and you touched the keyboard
or mouse in the last 5 minutes. When you are away, Telegram works exactly as before, with its sound. Quiet
hours still hold: nothing is said out loud during them.

## Switching things off

All settings go in `config.json` in the Waggle home. They are read when the PC voice starts.

| What | Setting | Default |
|---|---|---|
| The wake word | `"pc_voice_wake": false` | on |
| Waggle speaking its own messages | `"pc_voice_speak_messages": false` | on |
| The hotkey | `"pc_voice_hotkey": "ctrl+alt+q"` (any ctrl/alt/shift/win + key) | `ctrl+alt+backquote` |
| How long it waits for a follow-up | `"pc_voice_followup_s": 6` | 6 s |
| Silence that ends what you say | `"pc_voice_end_silence_s": 1.2` | 1.2 s |
| What counts as "away" | `"pc_presence_idle_s": 300` | 5 minutes without input |
| How loud sound must be to count as speech | `"pc_voice_min_level": 120` | 120 (frame RMS of 16-bit samples) |

- Stop it now: `python -m waggle.pcvoice stop` (it starts again at the next logon).
- See what it is doing: `python -m waggle.pcvoice status` (hotkey, wake word, microphone, counters, last error).
- Remove it completely: `powershell -ExecutionPolicy Bypass -File tools\install-pcvoice-task.ps1 -Uninstall`.

## For whoever deploys it

- `tools\install-pcvoice-task.ps1` registers the logon task "Waggle PC Voice" (`pythonw -m waggle.pcvoice run`,
  restart on failure, one instance). `-WhatIf` shows the plan.
- The core must run the new `/voice` code (`?format=wav`, `?client=pc`, `/voice/transcribe`) and the transport
  change (`waggle/pcspeak.py`), so restart the core after merging.
- The process starts no child processes, opens no window, and reads the microphone through the Windows waveIn
  API in-process. Logs: `<home>\pcvoice\pcvoice.log` (never the token, never what was heard).
- Checks: `python tools/pcvoice_check.py "a request"` (end to end with a file in place of the microphone, real
  Claude on Haiku, a throwaway home) and `python tools/pcvoice_measure.py --sanity` (the wake word's CPU, GPU
  and false triggers).

## Measured on this PC (November 2026)

| What | Result |
|---|---|
| Microphone | the webcam's, found at run time; a still room: frame RMS median 1-6, peak 200-230 of 32767 (0.7% of full scale) |
| Level | a 0.5 s test tone at 5% of full scale on the speakers reaches the microphone at about 3%; quiet recordings are raised before recognition (at most 30x); a request fed at 8% of full scale was recognised word for word |
| Wake word, 10 min | 5 min of the room as it was + 5 min of 31 played sentences without the name (including "Wiggle the cable" and "the waggle dance"): 45 utterances, 41 transcribed (0.32 s each), **0 false triggers**; a played "Hey Waggle, what time is it?" triggered |
| Wake word, cost | PC voice 0.8% of one CPU core, transcribing 1.7% of one core: 0.16% of the whole machine; GPU 0-0.2% on average (10 s samples) |
| End of speech to Waggle's voice | Two real Haiku turns: **17.7 s** (1.2 s of silence to end the request, 12.0 s recognition because the recogniser was loading in a fresh process on a busy machine, 3.7 s Claude, 0.6 s speech) and **24.5 s** (4.7 s recognition, about 8.6 s Claude, 9.7 s speech while the speech server was busy with another program). Warm, the parts measured separately add up to about 6-11 s: 1.2 s silence + 0.3 s recognition + Claude + 0.6 s speech |
| Screen | `tools/window_probe.py` around three full cycles (65 process starts): 0 windows, 0 focus changes, 0 busy cursors from these processes |
