from waggle import selfbuild
from waggle.config import Settings


def test_detects_capability_refusals_only():
    yes = [("open spotify and play my liked songs", "I can't control Spotify from here."),
           ("what's on my screen", "I don't have access to your screen right now."),
           ("record my screen for 30 seconds", "I'm unable to record video."),
           ("sync my calendar to obsidian", "That's beyond my current capabilities.")]
    no = [("buy me airpods", "I can't make purchases without your OK."),
          ("text my mom happy birthday", "I can't send messages to people for you."),
          ("how are you", "I can't complain!"),
          ("open notepad", "Opened Notepad."),
          ("type my password into chase", "I can't type passwords.")]
    for req, rep in yes:
        assert selfbuild.is_capability_refusal(req, rep), req
    for req, rep in no:
        assert not selfbuild.is_capability_refusal(req, rep), req


def test_handle_starts_one_build_and_dedupes():
    made = []
    s = Settings(max_selfbuilds_per_day=3)
    req, rep = "open spotify and play lo-fi", "I can't control Spotify."
    out = selfbuild.handle(req, rep, s, creator=lambda *a: made.append(a) or "job1")
    assert "building the tool" in out and len(made) == 1
    title, prompt = made[0][0], made[0][1]
    assert title.startswith("Skill:") and "cli-hub-meta-skill" in prompt and "skills" in prompt
    again = selfbuild.handle("Open Spotify and play lo-fi!", rep, s, creator=lambda *a: made.append(a))
    assert "already building" in again and len(made) == 1
    assert selfbuild.handle("hi", "Hello!", s, creator=lambda *a: made.append(a)) is None


def test_daily_cap():
    s = Settings(max_selfbuilds_per_day=1)
    selfbuild.handle("open app one", "I can't open that.", s, creator=lambda *a: "j1")
    assert selfbuild.handle("open app two", "I can't open that.", s, creator=lambda *a: "j2") is None
