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

What happens to what the microphone hears while the wake word is on: each burst of sound is cut out (by the
neural speech gate below) and a small keyword spotter reads its first seconds for the name (next section). Only a burst
with the name in it is turned into words, by Waggle's own recogniser on this PC (nothing leaves the PC for this). If
the words do not begin with the name, they are dropped at once: the sound exists on disk only for the moment the
recogniser reads it (then it is deleted), and the words are never logged or stored. Room speech without the name (a
television, a call) is never transcribed at all. With `"pc_voice_wake_mode": "stt"` every burst is transcribed, as
before this change. Windows shows its microphone-in-use icon the whole time the wake word is on.

## Waggle speaking first

When Waggle has something for you on its own (a reminder, the morning brief, a finished background job,
something it noticed) and you are at the PC, it says a short version out loud and the full message arrives in
Telegram without a notification sound. "At the PC" means: the screen is not locked and you touched the keyboard
or mouse in the last 5 minutes. When you are away, Telegram works exactly as before, with its sound. Quiet
hours still hold: nothing is said out loud during them.

## Who it answers, and how it speaks (second round)

- **Only what is meant for Waggle.** After an answer, and after "Sir?", you do not need the wake name, so Waggle
  writes down what it hears and a small local model (the same Ollama that does mail triage; `settings.local_model`)
  decides whether it was said to Waggle. A television, a call or someone else in the room is ignored, and what
  Waggle's own speakers said a moment ago coming back through the microphone is dropped. "Stop", "never mind" and
  "that's enough" end the conversation (it says nothing more); "Waggle, stop" works from idle too. The hotkey is
  always a request and is never judged. If the model is slow or down, what you say is sent, as before. Waggle only
  listens between its own sentences, so it cannot hear you over its own voice: the hotkey is still the way to cut it
  off while it talks.
- **A longer window after a question.** When Waggle's answer ends in a question it listens 10 s for the reply
  without the wake name (6 s otherwise).
- **It starts talking sooner.** The answer is requested from `POST /voice/stream` and spoken sentence by sentence
  (at most 3 sentences, 45 words): the first sentence plays while Waggle is still writing the rest. A core without
  that path (older) is asked through `POST /voice` as before.
- **Reminders.** A reminder asked by voice (Telegram voice note or the PC) arrives as a voice note and then the text;
  with you at the PC it is said there ("Sir, your reminder: ...") and both Telegram messages arrive silently.
- **Measure the judge:** `python -m waggle.bench voice` (50 labelled cases in `bench/voice_intent.jsonl`; `--model-only`,
  `--no-model`, `--model NAME`).

## Hearing the name first: the keyword spotter (21 November, `pc_voice_wake_mode`)

Until now every burst of speech in the room went to the recogniser on the GPU, and the words had to start with
"Waggle". With a television or anime on, that was dozens of transcriptions an hour ("heard speech, no wake name (13 so
far)" in the log), and a real "Waggle" was sometimes written as "bagel" or "Tay Waggle" and lost.

Now a small model listens for the name itself, before any transcription:

| | |
|---|---|
| Model | sherpa-onnx-kws-zipformer-gigaspeech-3.3M (k2-fsa, Apache-2.0), int8, 5.3 MB, English, any word can be the keyword |
| Runner | `sherpa-onnx` 2.0.1 (Apache-2.0, released 27 Oct, a 2.5 MB cp314 Windows wheel plus a 17 MB runtime, in the core interpreter: `py -3.14 -m pip install sherpa-onnx==2.0.1`) |
| Files | `<home>\models\kws-zipformer-gigaspeech-3.3M\` (4 files), pinned by size and sha256 like the Silero files: `python -m waggle.kws fetch` (17.6 MB archive, checked, 4 files kept), `python -m waggle.kws status`, `python -m waggle.kws test FILE.wav` |
| Code | `waggle/kws.py`; the wiring is `PcVoice.wake_frame`, `_kws_hit`, `_rescue` and `heard` in `waggle/pcvoice.py` |

How it works: when an utterance ends (the Silero gate), the spotter reads its first seconds (the name has to begin within
`pc_voice_kws_head_s` = 1.0 s of the first word, so "Hey Waggle" fits and "...the waggle dance of bees" does not).
No name: the utterance is dropped without being transcribed. A name: the utterance with 1.5 s of sound before it
goes to the recogniser exactly as before, so "Waggle, what time is it" still sends the rest. If the recogniser wrote
the name as another word the spotter's hit still counts (`pc_voice_kws_trust`): "Bagel, what time is it" is recognised
again from the audio after the name; "Tay Waggle, what time..." uses the words after "Waggle"; a name-like word
inside a sentence or after "the", "my", "that" ... is not a wake. Follow-up windows after an answer never need the name
(unchanged). If the model or sherpa-onnx is missing, or the spotter raises, the mode becomes "stt" for that run and one
warning says so.

Two things about this model that the code works around (found by measuring): a keyword that starts 1.3-1.6 s after the
start of the audio it is fed (and again 1.5 s later) is missed, whatever the level (the plain recogniser reads the same
audio correctly), so every utterance is scanned twice, the second time with 0.7 s of silence in front, which puts the
name outside the dead zone in one of the two passes; and the microphone's quiet speech (1% of full scale) is raised
before scanning, as the recogniser's front end does (without it, a fifth of the "Waggle"s were missed). The model's
"mobile" variant does not run under sherpa-onnx 1.13.8 or 2.0.1 at all (a Reshape error), so the full int8 one is used.

### Measured (no microphone, no speaker: `python tools/kws_eval.py synth|sweep|run|soak`)

Kokoro (Waggle's own voice) spoke the clips; the test voices (am_adam, bf_emma, af_bella, bm_lewis, at speeds 0.9 and 1.1)
are not the ones used to pick the settings (bm_george, af_heart). Each clip is mixed into the room at the owner's own
level (speech RMS 250, floor 2; from the real log: speech RMS 180-330, floor 0.5-4), quieter (150), louder (700),
with a fan and with synthetic music 10 dB lower (and, in the last row only, with a second talker, a television, 10 dB
lower), then run frame by frame through the PC voice's own wake path
(`PcVoice.wake_frame`: the Silero gate, the spotter, `heard`) in dry-run mode with the real recogniser (faster-whisper
turbo through `waggle.stt`, the call `/voice/transcribe` makes).

| | stt (before) | kws, text must say the name | **kws (default)** | both |
|---|---|---|---|---|
| Wake phrases heard (200: 5 phrases x 8 voices/speeds x 5 rooms) | 95.0% | 95.0% | **100%** | 100% |
| Request came through whole ("time", "calendar", "stretch") | 95.0% | 95.0% | **100%** | 100% |
| Hard negatives that woke it (80: bagel, wiggle, waffle, haggle, gaggle, Maggie ... and the waggle dance said mid-sentence) | 0 | 0 | **0** | 0 |
| False wakes in 10 min of room (65 utterances: TV talk, music, keys, thumps) | 0 | 0 | **0** | 0 |
| Recogniser calls in those 10 min | 64 | 0 | **0** | 64 |
| CPU, gate + spotter, % of one core | 0.51 | 1.36 | **1.36** | 1.44 |
| Name said over a second talker 10 dB lower (40) | 15% | 10% | 22% | 28% |

- The 5% the old way lost: "Hey Waggle." came out of the recogniser as "Tay-Waggle!" or "Tay Waggle." for two voices; the
  spotter hears the name there, so "kws" keeps those.
- Soak, 60 minutes of room without any recogniser (6 evenings of the same kinds of sound): 379 utterances, 6 reached the
  recogniser (6 per hour, instead of 379), all of them a mid-sentence "waggle dance" line inside the head window, which the
  words then reject; no other sound let anything through. The spotter costs about 80 ms per utterance on one thread.
- The first run of the trusting mode woke on 12 of 80 hard negatives ("I read that the waggle dance ...": the spotter
  heard the name, the recogniser wrote it mid-sentence). `_rescue` now rejects a name-like word that is not first or second,
  or follows "the", "my" ..., and the table is that second run (the other columns were not affected).
- The name said while a television keeps talking is the weak spot of every mode: the gate sees one long utterance that
  began with the television, so the name is not at its start. Nothing here fixes that.
- Everything is synthesised speech in a synthetic room. The owner's real voice, the real microphone and real music are not
  in it. Say if it misses you or wakes up by itself, and set `"pc_voice_wake_mode": "stt"` to go back.
- `pc_voice_kws_threshold` works from 0.25 up (0.4 still heard 19 of 20, 0.6 only 8, 0.8 none; below 0.25 nothing
  changes): raise it if it wakes too easily. `pc_voice_kws_score` (1.0) is the model's boost for the keyword, leave it.

## Hearing you better: speech detection and the end of your turn

Two small models, run on the CPU inside the PC voice process (onnxruntime, already installed), replace guesses:

| Model | What it does | Licence | File (in `<home>\models`) |
|---|---|---|---|
| Silero VAD v6.2.3 | says for every 32 ms whether it is speech, so music, a fan, typing and thumps stop starting utterances and stop holding one open | MIT | `silero_vad.onnx`, 2,327,524 bytes |
| Smart Turn v3.2 (pipecat, CPU int8) | listens to the last 8 s, pause included, and says whether you have finished or only stopped to think | BSD-2-Clause | `smart-turn-v3.2-cpu.onnx`, 8,679,182 bytes |

Install: `python -m waggle.vad fetch` (only needed once; it downloads from the official repositories at a pinned tag or
commit and refuses and deletes anything whose size and sha256 are not the pinned ones, see `waggle/vad.py`).
`python -m waggle.vad status` shows what is present. Without the files, or without onnxruntime, the PC voice behaves as
before (loudness gate, fixed silence) and writes one line to its log.

- **Silero is on** (`pc_voice_vad`). It still needs the sound to be at least `pc_voice_min_level` loud, so it can only
  remove false starts, never add new ones (a television in another room). With it you can lower `pc_voice_min_level`
  (try 60) if the webcam microphone is too quiet for you.
- **Smart Turn is built but off** (`pc_voice_endturn`: "silence"). Measured with synthesised speech it ended turns in
  the middle of thinking pauses far too often (table below), so it stays off until it has been tried on a real voice.
  One safe way to use it already: `"pc_voice_endturn": "smart", "pc_voice_endturn_pause_ms": 1200,
  "pc_voice_endturn_max_s": 2.0` asks the model only after today's 1.2 s of silence and lets it wait up to 2.0 s when it
  thinks you are not finished: it can only add waiting, never cut you off earlier than today.

### Measured (no microphone; `python tools/endturn_eval.py synth|run|words|tune`)

Kokoro (Waggle's own voice, 4 voices, 2 speeds) spoke the sentences; mixed with a quiet room, a quiet speaker, a fan
(20 and 10 dB), pink noise, synthetic music (10 and 5 dB) and typing: 8 backgrounds, 4 endpointers run frame by frame as
the PC voice runs them. A = loudness + 1.2 s (before), B = Silero + 1.2 s (now), C/D = Smart Turn with each gate.
23 requests with a thinking pause of 0.5-1.5 s in the middle ("remind me to ... um ... call grandma"), the pause put in
at the word boundary found by Whisper's word times and checked by re-transcribing both halves; 20 complete requests.

| | A before | B Silero (default) | D Silero + Smart Turn | E Smart Turn, extend only |
|---|---|---|---|---|
| Complete requests: turn ended before the last word | 0 / 160 | 0 / 160 | 21 / 160 | 0 / 160 |
| Complete requests: mean wait after the last word | 1.39 s (40 never ended in music) | 1.28 s | 0.29 s | 1.28 s |
| Thinking pause shorter than 1.2 s: cut in half | 0 / 184 | 0 / 184 | 83 / 184 | 0 / 184 |
| Thinking pause up to 1.5 s: cut in half | 52 / 184 | 63 / 184 (and 48 stuck in A) | 163 / 184 | 39 / 184 |
| First words lost (capture starts late), missed turns | 0, 0 (1 in a stuck run) | 0, 0 | 0, 0 | 0, 0 |
| Turn never ended 4 s after the last word (music kept the gate open) | 141 runs | 0 | 0 | 0 |
| Utterances sent on per 10 min of music with nobody speaking | 20 | 0 | 0 | 0 |
| ... of door thumps | 80 | 0 | 0 | 0 |

Recogniser check of what was captured (Whisper small on the CPU; 32 sentences x 4 backgrounds): A kept the whole
sentence in 92 of 96 runs (32 runs never ended because of the music), B in 127 of 128; every difference was Whisper
spelling a word differently ("Waggle" as "wagle", "add" as "and"), and in each of them the capture began before the
first word, so no word was lost by either gate.
Smart Turn on its own published test data (25 English clips from the dataset it was trained with): 24 right, so the
feature extraction is correct (it matches the reference extractor to 1e-7); on the synthesised pauses it calls most
mid-sentence stops "finished". Synthesised speech has no hesitation in its voice, so this says Smart Turn is not safe to
switch on here, not that it fails with people: try it with a real voice before changing the default.
Costs: Silero under 1 ms per 30 ms, Smart Turn about 40 ms per check on one CPU thread.

## Answering faster (21 November, after the first test with a real voice)

You said it was good but a little slow, or that it might not hear you. The log showed the cause of most of the wait:

- **The judge never answered.** It used `local_model` (qwen3.6, 22 GB). Ollama needed about 20 s to load it next to the
  other models and then evicted it again when the notes search needed the embedder, so each judgement hit the 3 s
  limit and "(fallback)" sent the words as they were, 3 s late. Now the judge is `gemma4:latest` (49/50 on
  `bench/voice_intent.jsonl`, the same as qwen3.6; qwen3:8b 48/50; qwen3:4b only 41/50; median 0.2 s, about 3 GB), it
  waits at most 1.5 s, and after one failure it is skipped for a minute (the words are sent at once) while the model
  is loaded again in the background. It is also loaded when the PC voice starts.
- **The recogniser in the core unloads after 3 idle minutes** and loads again in 3-12 s. The PC voice now sends it a
  quarter second of silence (`/voice/transcribe`) as soon as speech has begun in the room or you press the hotkey, so it
  is loaded when your sentence ends. Nothing new is stored or logged.
- **The first message of a new day** starts a fresh Claude session (22 tools, the transcript): 5-18 s. The core's
  5-minute reap timer now starts that session after 4 am, while nobody waits (`chat_session.rollover`; the
  transcript is read again when your first message arrives).
- **Voice turns give the notes search 0.4 s instead of 0.8 s** before the model is asked without it.

Every voice turn writes one line to `pcvoice.log` (never the words), seconds per stage:

`turn timing: end=1.23s stt=0.31s judge=0.20s context=0.80s first_words=3.10s first_audio=3.90s total=5.40s (...)`

`end` is the silence waited to be sure you had finished, `stt` and `judge` are this process asking the core and the
judge (a dash: not done this turn, e.g. the hotkey is never judged), `context` is the core building the notes block,
`first_words` and `first_audio` count from sending the request, `total` is from the end of your last word to the first
sound. In brackets: where the judge's answer came from, the core's own `core_stt`, `model_first` (message to the
model's first words), and how loud you were (`rms`, `peak`, `floor`), so "can't hear me" can be checked: a peak near
the floor means the microphone, not the software. A failed judge also says why in the "heard in a conversation window"
line (for example `fallback: TimeoutError: timed out`).

Not changed: `pc_voice_endturn` stays `silence` and `pc_voice_end_silence_s` stays 1.2 s (the biggest fixed wait; the
measurements above show shorter silences cut thinking pauses), and `pc_voice_min_level` is untouched.

## Switching things off


All settings go in `config.json` in the Waggle home. They are read when the PC voice starts.

| What | Setting | Default |
|---|---|---|
| The wake word | `"pc_voice_wake": false` | on |
| Hear the name with the keyword spotter first (`kws`), transcribe everything (`stt`, the old way) or both | `"pc_voice_wake_mode": "stt"` | kws |
| Spotter: how sure it must be, how soon after speech starts the name must begin, whether its hit counts when the recogniser wrote another word | `"pc_voice_kws_threshold": 0.25`, `"pc_voice_kws_head_s": 1.0`, `"pc_voice_kws_trust": true` | 0.25, 1.0 s, true |
| Waggle speaking its own messages | `"pc_voice_speak_messages": false` | on |
| The hotkey | `"pc_voice_hotkey": "ctrl+alt+q"` (any ctrl/alt/shift/win + key) | `ctrl+alt+backquote` |
| How long it waits for a follow-up | `"pc_voice_followup_s": 12` | 12 s (a soft tone after each answer says it is still listening; off: `"pc_voice_followup_cue": false`) |
| All of "who it answers" and the sentence-by-sentence speech | `"pc_voice_smart": false` | on |
| Only the judge, echo and stop (keep the streaming) | `"pc_voice_judge": false` | on |
| Only the sentence-by-sentence speech | `"pc_voice_stream": false` | on |
| Follow-up window after a question | `"pc_voice_question_s": 20` | 20 s |
| Which local model judges | `"pc_voice_judge_model": "qwen3:8b"` (empty: `gemma4:latest`) | empty |
| How long the judge may take, and how sure it must be | `"pc_voice_judge_timeout_s": 1.5`, `"pc_voice_judge_min": 0.5` | 1.5 s, 0.5 |
| Silence that ends what you say | `"pc_voice_end_silence_s": 1.2` | 1.2 s |
| What counts as "away" | `"pc_presence_idle_s": 300` | 5 minutes without input |
| How loud sound must be to count as speech | `"pc_voice_min_level": 120` | 120 (frame RMS of 16-bit samples) |
| Which gate finds speech | `"pc_voice_vad": "energy"` (loudness only) or `"silero"` (a small neural model) | silero (energy when the model file is missing) |
| When your turn is over | `"pc_voice_endturn": "silence"` (the fixed silence above) or `"smart"` (Smart Turn) | silence |
| Smart Turn: first check after a pause of, longest silence, how sure | `"pc_voice_endturn_pause_ms": 200`, `"pc_voice_endturn_max_s": 1.6`, `"pc_voice_endturn_min_p": 0.5` | 200 ms, 1.6 s, 0.5 |

- Stop it now: `python -m waggle.pcvoice stop` (it starts again at the next logon).
- See what it is doing: `python -m waggle.pcvoice status` (hotkey, wake word, microphone, counters, last error).
- Remove the spotter: `"pc_voice_wake_mode": "stt"`; `py -3.14 -m pip uninstall sherpa-onnx sherpa-onnx-core`; delete
  `<home>\models\kws-zipformer-gigaspeech-3.3M`.
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
| Owner's first real test (21 Nov 09:48) | turn 1: 18 s from "heard" to the answer, turn 2: 4 s; both "(fallback)". Ollama's log shows the judge's 22 GB model loading for 20 s and being evicted; the notes search's embedder waited 9.7 s behind it. Not yet re-measured after the changes above |
| End of speech to Waggle's voice | Two real Haiku turns: **17.7 s** (1.2 s of silence to end the request, 12.0 s recognition because the recogniser was loading in a fresh process on a busy machine, 3.7 s Claude, 0.6 s speech) and **24.5 s** (4.7 s recognition, about 8.6 s Claude, 9.7 s speech while the speech server was busy with another program). Warm, the parts measured separately add up to about 6-11 s: 1.2 s silence + 0.3 s recognition + Claude + 0.6 s speech |
| Screen | `tools/window_probe.py` around three full cycles (65 process starts): 0 windows, 0 focus changes, 0 busy cursors from these processes |
