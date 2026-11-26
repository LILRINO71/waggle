"""Paths and settings. Everything lives outside AppData on purpose: the Microsoft Store
build of the Claude app redirects AppData writes into its own private folder."""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("WAGGLE_HOME", Path.home() / ".waggle"))
CONFIG_FILE = HOME / "config.json"


@dataclass
class Settings:
    # The whole vault: Waggle READS all of it (search, graph, profile). No default on purpose: a config.json without
    # "vault" must not quietly start a second vault somewhere else (vault.root() refuses; `cli init` sets one).
    vault: str = ""
    # Waggle's own notes live in this folder inside the vault and it writes nowhere else. "" = the vault root (a vault
    # that is Waggle's alone); the owner's vault uses "12 - Waggle". The history (vault_git) lives in that folder too.
    vault_own: str = ""
    # The owner's own profile notes, vault-relative paths or globs, read into the "about me" context and the morning
    # brief (never written). Empty = Waggle's own Me/Profile only.
    vault_profile_notes: list = field(default_factory=list)
    vault_profile_chars: int = 1600  # how much of them goes into the context
    # Vault history (waggle/vault_git.py): every Waggle change to the vault is a local git commit you can undo.
    vault_git: bool = True
    vault_git_max_kb: int = 512  # a note Waggle writes that is bigger than this is refused and put back
    vault_git_readonly: list = field(default_factory=list)  # globs Waggle may never change, e.g. ["Me/Profile.md"]
    jobs_dir: str = str(Path.home() / "Claude" / "waggle-jobs")
    ollama_url: str = "http://127.0.0.1:11434"  # not "localhost": on Windows that adds ~2 s per request
    local_model: str = "waggle-4b"
    fallback_coder: str = "hf.co/mradermacher/Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated-i1-GGUF:Q4_K_M"
    embed_model: str = "nomic-embed-text"
    # Memory search (waggle/memory.py): BM25 full text next to the embeddings, fused by rank, with a boost for
    # questions about a date. False restores the embeddings-only search exactly.
    memory_hybrid: bool = True
    # Filing a fact (waggle/factdecide.py): compare it with its most similar known facts and decide ADD / UPDATE /
    # INVALIDATE / SKIP. False restores the old duplicate check only.
    memory_fact_decide: bool = True
    router_model: str = "haiku"  # decides AND answers most messages (~2.3 s via the Claude Code CLI)
    deep_model: str = "sonnet"  # hard reasoning the router hands up; also the second-opinion reviewer
    review_enabled: bool = False  # second-opinion pass on robotics/code answers (adds ~10 s)
    builder_model: str = "opus"
    # Tool-using agents (waggle/agent.py): quick jobs on Haiku, harder multi-step work on Sonnet.
    agent_enabled: bool = True
    agent_model: str = "haiku"
    agent_wait_s: float = 45.0  # longer agent jobs keep going in the background and report on Telegram
    agent_deep_model: str = "sonnet"
    # Chat with the user (Telegram, voice, CLI) may use stronger models than background work; empty = same as above.
    chat_model: str = ""  # answers + routing (router_model when empty)
    chat_agent_model: str = ""  # tool jobs from chat (agent_model when empty)
    chat_deep_model: str = ""  # coder / researcher / robotics jobs from chat (agent_deep_model when empty)
    job_model: str = ""  # the owner's standing choice for background jobs started from chat ("" = each agent's own)
    local_first: bool = False  # let the local model answer "simple" chat itself (faster, much less smart)
    # The user's own addresses: mail to these needs no approval (it reaches only the user). Set in config.json.
    self_emails: list = field(default_factory=list)
    # Speech-recognition fixes for voice notes, regex -> replacement (e.g. a robot name it keeps mishearing).
    voice_fixes: dict = field(default_factory=dict)
    agent_workdir: str = ""  # default: ~/Claude
    extra_write_roots: list = field(default_factory=list)
    # Share of the Pro limit Waggle may use, and the point where it stops escalating at all.
    waggle_share: float = 0.30
    stop_at: float = 0.85
    # The user's estimate of one 5-hour Pro window, in millions of weighted tokens (see quota.py). Waggle never
    # writes it: what it learns from real limit hits lives in budget.db, kept within quota.ESTIMATE_BOUNDS of this.
    window_units_estimate: float = 6.0
    window_hours: float = 5.0
    # Background work (autonomy, routines, proposals...) waits while the real seven-day usage (the CLI's
    # unifiedWindows.seven_day) is at or above this fraction, so it never eats the owner's week. 0 = no check.
    seven_day_stop_at: float = 0.85
    # Background budget (waggle/budget.py): objectives, routines, proactive checks, brief, learning.
    # Units are weighted tokens (an estimate of plan-limit use; ~0.06 per Haiku agent step, ~0.2 per Sonnet step).
    # Change with `python -m waggle.cli budget set 6` or by editing ~/.waggle/config.json.
    background_daily_units: float = 4.0
    background_stop_at: float = 0.6  # background stops at 60% of the 5-hour window; chat may go on to stop_at
    background_max_workers: int = 2  # background Claude calls running at once
    background_reset_hour: int = 4  # the background day starts at 04:00 local time
    background_downgrade_at: float = 0.25  # under 25% left: non-coding Sonnet steps run on agent_model instead
    interactive_yield_s: int = 60  # background waits this long after the user's last message
    search_top_k: int = 6
    quiet_hours: list = field(default_factory=lambda: [23, 7])
    # Proactive loop
    watch_roots: list = field(default_factory=lambda: [str(Path.home() / "Claude")])
    extra_repos: list = field(default_factory=list)
    max_verifications_per_day: int = 4
    max_calls_per_day: int = 2
    calls_enabled: bool = False  # phone calls (Twilio) are off until `python -m waggle.call enable`; off = no check, no log
    # Pictures, the screen and Hermes's image tool. Empty = local_model. gemma4 is blind to images on Ollama 0.40.16
    # (2026-11-21: "no image provided" for a plain page of text), qwen3.6 reads them.
    vision_model: str = ""
    # The Drop folder (waggle/dropbox_ingest.py): summaries and the weekly tidy use this local Ollama model (empty =
    # local_model); pictures are described by vision_model. Claude is never used for dropped files.
    drop_local_model: str = ""
    max_selfbuilds_per_day: int = 3
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""  # generated on first use; private channel for urgent push alerts
    # Dead-man switch (waggle/deadman.py, cloud/deadman/): the free Cloudflare Worker that delivers reminders and says
    # "Waggle is down" while the PC is off. Empty deadman_url = off. deadman_hide_text: the Worker holds "Reminder due"
    # instead of the reminder's words.
    deadman_url: str = ""
    deadman_hide_text: bool = False
    brief_time: str = "30 7 * * *"  # morning brief (cron)
    stale_days: int = 7
    # Initiative (waggle/tick.py, initiative.py, watchers.py, followups.py): what Waggle tells the user unprompted.
    daily_text_cap: int = 8  # unprompted texts/voice notes/calls a day; urgent items and things asked for bypass it
    initiative_every_min: int = 120  # how often the reasoning step looks at new signals (urgent ones: right away)
    initiative_model: str = "sonnet"
    # The thinking pass (waggle/think.py): Claude gets the whole picture and one open question, a few times a day.
    think_enabled: bool = True
    think_times: list = field(default_factory=lambda: ["07:40", "16:15", "19:45"])  # at most 4 a day
    think_model: str = "sonnet"
    think_effort: str = "medium"
    # Skill scout (waggle/skillscout.py): finds Agent Skills on GitHub every day and installs the useful, clean ones.
    skillscout_enabled: bool = True
    skillscout_model: str = "sonnet"
    topic_repeat_hours: int = 48  # the same topic is not raised again within this window
    evening_time: str = "21:00"  # the daily evening note (always sent)
    brief_catchup_hours: float = 3.0  # a missed morning brief is still sent this long after its time, never later
    evening_catchup_hours: float = 1.5
    followup_hour: int = 19  # check-in time for "I'll do X today/tomorrow" with no clock time
    followup_platforms: list = field(default_factory=lambda: ["telegram"])
    github_watch: bool = True
    github_repos: list = field(default_factory=list)  # "owner/name"; empty = the user's recently pushed repos
    github_every_min: int = 15
    upcoming_note: str = ""  # vault note with dated items and mail to answer; empty = Upcoming / Me/Upcoming
    outbox_max_age_h: float = 24.0  # undelivered messages give up (recorded as expired) after this long
    dev_sources: list = field(default_factory=lambda: ["oneshot", "cli", "tool", "test", "api", "batch"])
    voice_note_voice: str = "af_heart"
    voice_note_speed: float = 1.1
    # Fast answers to spoken requests (waggle/voice_fast.py): voice_fast_path answers the time, the date, the reminders
    # list, sums and unit conversions in plain code (no model); voice_fast_local lets the small local model answer a
    # self-contained general-knowledge question within voice_fast_cap_s seconds, else the request goes to Claude.
    # voice_fast_model "" = the PC voice judge's model (already loaded: a different one is loaded for 8+ s).
    voice_fast_path: bool = True
    voice_fast_local: bool = True
    voice_fast_model: str = ""
    voice_fast_cap_s: float = 2.5
    # A typed (Telegram text) message gets the same code tier only: the time, the date, reminders, sums, conversions.
    # Never the local model: a typed question can wait for Claude's better answer.
    text_fast_path: bool = True
    scout_enabled: bool = True
    # Cheap checks in front of Claude for work that runs on its own (waggle/gates.py, notifycheck.py, cooldowns.py).
    # Each switch is independent: false restores the old behaviour exactly.
    gates_enabled: bool = True  # a pre-check may skip a background Claude call when it is very sure (P >= 0.95)
    gates_local: bool = True  # the pre-check may ask the local model (only when Ollama already has a model loaded)
    notify_check_enabled: bool = True  # a local yes/no decides if a background result is worth a message now
    # Cheap-first cascade for background Claude calls (waggle/cascade.py): local model, then Haiku, then Sonnet, the
    # first answer that passes a self-check wins; usage steers it. false = every caller takes its old direct call.
    cascade_enabled: bool = True
    cascade_local: bool = True  # the cascade may try the local model first (false: it starts at Haiku)
    # Objective and autonomy steps are judged on a checklist of requirements: code checks what it can, an
    # evidence-only judge reads the step's artifacts for the rest, and files changed outside the write roots fail the
    # step (waggle/checklist.py). false = the old proof / Haiku check on the agent's report.
    checklist_judge: bool = True
    # Outreach timing and choice (waggle/holdq.py, outreach_ev.py). Each switch is independent; false = the old behaviour.
    hold_for_breaks: bool = True  # an unprompted, non-urgent message waits for the owner's next natural break
    hold_max_wait_min: float = 90.0  # ...and goes out anyway after this long
    outreach_ev_enabled: bool = True  # send an unprompted message only when P(welcome) x value - cost clears the threshold
    outreach_ev_threshold: float = 0.0
    recipes_enabled: bool = True  # repeated work seen by ActivityWatch -> a suggested routine (waggle/recipes.py)
    engine_cooldowns_enabled: bool = True  # an engine that just failed (limit, credit, busy, auth...) rests a while
    phone_target: str = "telegram"
    # Saved by install.py: cron scripts run with a trimmed environment and may not see LOCALAPPDATA.
    claude_bin: str = ""
    # Weather: saved the first time the user names a city; imperial = °F / mph.
    home_location: str = ""
    units: str = "imperial"
    # Turn engine (waggle/chat_engine.py): one long-lived Claude process per chat, streamed replies.
    engine_enabled: bool = False  # True = the turn engine answers in the gateway; False = the old router path
    # Telegram chat ids whose typed messages the gateway hands to the core's turn engine (empty = none).
    engine_chats: list = field(default_factory=list)
    # Photos, videos, files, audio files, stickers, location pins and Waggle's own /commands in an engine chat go to the engine too
    # (waggle/media_intake.py, waggle/engine_commands.py); False = only typed text and voice notes.
    engine_media: bool = True
    media_max_mb: float = 20  # a file larger than this is refused with a plain reply (also the Bot API's own limit)
    media_keep_days: float = 14  # saved attachments are deleted after this many days
    audio_transcribe_max_s: float = 600  # an audio file (not a voice note) up to this long is transcribed
    video_transcribe_max_s: float = 120  # the sound of a video up to this long is transcribed (frames are always taken)
    # WebFetch is never given (chat_session.UNTRUSTED_READERS): pages go through web_read (Step 1)
    chat_tools: list = field(default_factory=lambda: ["Read", "Glob", "Grep", "Bash", "PowerShell", "WebSearch", "Write", "Edit",
                                                      "NotebookEdit"])
    # Wolfram's free cloud MCP (no key): checked maths and science for the chat brain. false = not offered.
    wolfram_mcp: bool = True
    # Windows-MCP (waggle/winmcp.py): the chat brain reads the Windows UI tree and acts on apps. false = not offered.
    windows_mcp: bool = True
    # Google through gogcli (waggle/google.py, docs/GOOGLE.md): Docs, Drive, Calendar, Tasks, Classroom for the core and
    # the chat brain. Inert until the owner signs in once. google_account only matters if two accounts are signed in.
    google_gog: bool = True
    # waggle/toolfinder.py: find_tool searches public catalogs for an existing tool; installed MCP servers join the chat brain.
    toolfinder_enabled: bool = True
    google_account: str = ""
    # python -m waggle.feedwatch: new posts and releases from the owner's feed list (FIRST blog, FTC SDK, Ollama...).
    feedwatch_enabled: bool = True
    # Drop folder: scans, .xlsx and pictures read by markitdown-ocr with the local vision model (waggle/dropbox_markitdown.py).
    # install.py also fetches rss-feeds and the Obsidian Bases / JSON Canvas / CLI skills through Hermes's hub.
    hub_skills: bool = True
    drop_ocr: bool = True
    # python -m waggle.usage_view: ccusage's by-block and by-session token view beside the quota line (read-only, offline).
    ccusage_enabled: bool = True
    # Autonomy and objective runs leave a record (waggle/session_records.py); 4 useless runs in a row show in the evening note.
    session_records_enabled: bool = True
    # Proposal build jobs (not chat) may read current library docs from Context7's free hosted MCP (waggle/proposals.py).
    proposals_context7: bool = True
    # Proposal verify step: pyright errors in the touched files are listed in the merge message (a warning, never a gate).
    proposals_pyright: bool = True
    # Proposal verify step: replay the owner's real recent turns and the fixed eval cases on the base and on the change
    # (tools/replay, Inspect AI in ~/.waggle-tools/inspect, local model only); a change that makes answers worse is not
    # offered for merge. Skipped with a note when the venv is missing. False: no replay.
    proposals_replay: bool = True
    drop_ocr_model: str = ""  # empty = local_model
    # Nightly local backup of the data home (waggle/backup.py): dated zips here, the newest backup_keep kept. Not a
    # cloud folder, because the data includes tokens. Empty = %USERPROFILE%\WaggleBackups.
    backup_dir: str = ""
    backup_keep: int = 14
    # ActivityWatch (waggle/aw_sensor.py): window, AFK and browser activity across the PC, read from 127.0.0.1:5600 and
    # kept as derived facts only. Does nothing while ActivityWatch is not running. Focus nudges (waggle/focus.py)
    # have no switch: they exist only inside a block the owner starts.
    activitywatch_enabled: bool = True
    chat_effort: str = "low"
    chat_idle_s: int = 1800  # an idle chat process is stopped; the next message resumes the same session
    chat_turn_timeout_s: int = 600
    chat_session_max_turns: int = 40  # then a fresh session starts, seeded with the recent transcript
    # Lean chat launch (Step 1, waggle/chat_session.py): its own empty folder, only Waggle's MCP servers
    # (--strict-mcp-config), no user settings, memory or CLAUDE.md, and every MCP tool outside the small core loaded
    # on demand through ToolSearch. Fixed context 115,686 -> about 15,000 tokens. false = the old launch.
    chat_lean: bool = True
    # A chat session whose last call carried more than this many tokens of context starts fresh on the next turn,
    # seeded with a short summary of the conversation (0 = never by size; the day and turn limits still apply).
    chat_session_max_tokens: int = 60000
    night_shift: bool = True  # Step 3: approved objectives worked 23:00-07:00 (waggle/nightshift.py)
    night_week_cap: float = 0.35  # the night shift's share of the 7-day allowance (owner, 2026-11-21)
    sync_wait_s: float = 25.0  # surfaces without message editing (desktop, voice notes) wait this long, then ack
    stream_platforms: list = field(default_factory=lambda: ["telegram"])
    edit_interval_s: float = 1.2  # Telegram allows about one edit per second per chat

    def vision(self):
        """The local model that looks at pictures and the screen."""
        return self.vision_model or self.local_model

    @classmethod
    def load(cls):
        data = {}
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        s._loaded = asdict(s)
        return s

    def save(self):
        """Write the settings this object changed (since it was loaded, or from the defaults when it was built in
        code) into config.json as the file is now. Settings nobody changed, keys the file leaves out and keys this
        version does not know stay exactly as they are: the tamper guard hashes the budget/limit keys, so a save that
        only sets ntfy_topic must not rewrite them. A missing file is written in full, as a template to edit. The
        write is atomic, so a reader never sees half a file."""
        HOME.mkdir(parents=True, exist_ok=True)
        now = asdict(self)
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            base = getattr(self, "_loaded", None) or asdict(type(self)())
            data.update({k: v for k, v in now.items() if k not in base or base[k] != v})
        else:
            data = now
        tmp = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        self._loaded = now


def data_path(name):
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / name
