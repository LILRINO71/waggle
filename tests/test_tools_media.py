"""media_convert / image_convert (waggle/tools_media.py): argument building and plumbing with a fake ffmpeg,
Pillow for real, the real ffmpeg when present."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from waggle import policy
from waggle import tools_common as tc
from waggle import tools_media as tm


@pytest.fixture
def writable(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "writable_roots", lambda settings=None: [policy.norm(tmp_path)])
    return tmp_path


class FakeFfmpeg:
    def __init__(self, monkeypatch, duration="00:01:40.00", rc=0, write=True):
        self.calls, self.duration, self.rc, self.write = [], duration, rc, write
        monkeypatch.setattr(tm, "ffmpeg_exe", lambda: "ffmpeg.exe")
        monkeypatch.setattr(tc, "run", self.run)

    def run(self, cmd, timeout=120, low_priority=False, **kw):
        self.calls.append((cmd, low_priority))
        if "-i" in cmd and cmd[-1] != "-" and cmd[-2] == "-i":  # the duration probe: `ffmpeg -i file` with no output
            return 1, "", f"  Duration: {self.duration}, start: 0.0, bitrate: 1000 kb/s"
        if self.write:
            Path(cmd[-1]).write_bytes(b"x" * 1000)
        return self.rc, "", "boom" if self.rc else ""


def test_time_parsing():
    assert tm.seconds("90", "start") == 90 and tm.seconds("1:30", "s") == 90 and tm.seconds("0:01:30.5", "s") == 90.5
    assert tm.seconds("", "s") is None and tm.seconds(None, "s") is None
    with pytest.raises(tc.ToolError, match="start must be"):
        tm.seconds("soon", "start")


def test_build_command_variants():
    base = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-threads", "4"]
    gif = tm.build_command("ffmpeg", "in.mp4", "out.gif", "gif", start=2, end=5, width=320, fps=10)
    assert gif[:7] == base and gif[7:9] == ["-ss", "2"] and "-t" in gif and "3" in gif
    assert any("fps=10,scale=320:-2" in a and "palettegen" in a for a in gif) and "-an" in gif
    frame = tm.build_command("ffmpeg", "in.mp4", "o.jpg", "jpg", start=1)
    assert frame[-3:] == ["-frames:v", "1", "o.jpg"]
    audio = tm.build_command("ffmpeg", "in.mp4", "o.mp3", "mp3")
    assert "-vn" in audio and "libmp3lame" in audio
    mp4 = tm.build_command("ffmpeg", "in.mov", "o.mp4", "mp4", width=1280)
    assert "libx264" in mp4 and "-crf" in mp4 and "scale=1280:-2" in mp4 and "+faststart" in mp4
    with pytest.raises(tc.ToolError, match="after start"):
        tm.build_command("ffmpeg", "a", "b.mp4", "mp4", start=10, end=5)


def test_max_mb_plans_the_bitrate_from_the_length():
    cmd = tm.build_command("ffmpeg", "in.mp4", "o.mp4", "mp4", max_mb=25, duration=100)
    kbps = int(cmd[cmd.index("-b:v") + 1].rstrip("k"))
    assert "-crf" not in cmd and 1700 < kbps < 1900  # 25 MB * 8 * 0.93 / 100 s - 128 kbps audio
    assert int(cmd[cmd.index("-maxrate") + 1].rstrip("k")) > kbps
    with pytest.raises(tc.ToolError, match="too small"):
        tm.build_command("ffmpeg", "in.mp4", "o.mp4", "mp4", max_mb=1, duration=600)
    with pytest.raises(tc.ToolError, match="length"):
        tm.build_command("ffmpeg", "in.mp4", "o.mp4", "mp4", max_mb=5)
    trimmed = tm.build_command("ffmpeg", "in.mp4", "o.mp4", "mp4", start=20, max_mb=25, duration=100)
    assert int(trimmed[trimmed.index("-b:v") + 1].rstrip("k")) > kbps  # a shorter clip may use a higher bitrate


def test_media_convert_runs_ffmpeg_at_low_priority_and_reports_sizes(writable, monkeypatch):
    src = writable / "match.mp4"
    src.write_bytes(b"y" * 5000)
    fake = FakeFfmpeg(monkeypatch)
    out = json.loads(tm.media_convert({"path": str(src), "to": "mp4", "max_mb": 25, "width": 1280}))
    assert out["ok"] and Path(out["output"]["path"]) == writable / "match-small.mp4" and out["met_target"] is True
    assert out["source_size"] == "4.9 KB"
    run_cmd, low = fake.calls[-1]
    assert low is True and "-b:v" in run_cmd
    assert fake.calls[0][0][-2:] == ["-i", str(src)]  # the length was probed first


def test_media_convert_failures_leave_nothing_behind(writable, monkeypatch):
    src = writable / "a.mp4"
    src.write_bytes(b"x")
    FakeFfmpeg(monkeypatch, rc=1, write=True)
    out = json.loads(tm.media_convert({"path": str(src), "to": "mp3"}))
    assert not out["ok"] and "boom" in out["hint"] and not (writable / "a.mp3").exists()
    for args, word in [({"path": str(src), "to": "avi"}, "cannot write"), ({"path": str(src), "to": "mp4", "max_mb": "lots"}, "max_mb"),
                       ({"path": str(src), "width": 10}, "width"), ({"path": str(writable / "x.txt")}, "not found")]:
        assert word in json.loads(tm.media_convert(args))["error"], args
    song = writable / "s.mp3"
    song.write_bytes(b"x")
    assert "no picture" in json.loads(tm.media_convert({"path": str(song), "to": "gif"}))["error"]


def test_long_gifs_are_refused(writable, monkeypatch):
    src = writable / "long.mp4"
    src.write_bytes(b"x")
    FakeFfmpeg(monkeypatch, duration="00:02:00.00")
    assert "30 s" in json.loads(tm.media_convert({"path": str(src), "to": "gif"}))["error"]
    assert json.loads(tm.media_convert({"path": str(src), "to": "gif", "start": "5", "end": "15"}))["ok"]


def test_missing_ffmpeg_is_a_clear_failure(writable, monkeypatch):
    src = writable / "a.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(tc, "which", lambda *a, **k: "")
    monkeypatch.setitem(__import__("sys").modules, "imageio_ffmpeg", None)  # import fails
    out = json.loads(tm.media_convert({"path": str(src)}))
    assert not out["ok"] and "ffmpeg was not found" in out["error"]


needs_pillow = pytest.mark.skipif(importlib.util.find_spec("PIL") is None, reason="no Pillow")
needs_ffmpeg = pytest.mark.skipif(not (tc.which("ffmpeg") or importlib.util.find_spec("imageio_ffmpeg")), reason="no ffmpeg")


@needs_pillow
def test_image_convert_resizes_flattens_and_strips_metadata(writable):
    from PIL import Image
    src = writable / "photo.png"
    img = Image.new("RGBA", (2000, 1000), (200, 30, 30, 128))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    img.save(src, exif=exif)
    out = json.loads(tm.image_convert({"path": str(src), "to": "jpg", "max_side": 500, "quality": 70}))
    assert out["ok"] and out["size_px"] == "2000x1000 -> 500x250" and out["metadata"] == "stripped"
    dst = Path(out["output"]["path"])
    assert dst == writable / "photo.jpg"
    with Image.open(dst) as got:
        assert got.size == (500, 250) and got.mode == "RGB" and not got.getexif()
    smaller = json.loads(tm.image_convert({"path": str(src), "width": 100}))  # same format: not overwritten
    assert Path(smaller["output"]["path"]).name == "photo-resized.png" and smaller["size_px"].endswith("100x50")
    webp = json.loads(tm.image_convert({"path": str(src), "to": "webp", "height": 40}))
    assert webp["ok"] and webp["size_px"].endswith("80x40")
    pdf = json.loads(tm.image_convert({"path": str(src), "to": "pdf", "max_side": 300}))
    assert Path(pdf["output"]["path"]).read_bytes().startswith(b"%PDF")


@needs_pillow
def test_image_convert_refuses_bad_requests(writable):
    from PIL import Image
    src = writable / "a.png"
    Image.new("RGB", (10, 10)).save(src)
    assert "cannot write" in json.loads(tm.image_convert({"path": str(src), "to": "heic"}))["error"]
    assert "quality" in json.loads(tm.image_convert({"path": str(src), "quality": 500}))["error"]
    bad = writable / "b.png"
    bad.write_bytes(b"not an image")
    assert not json.loads(tm.image_convert({"path": str(bad), "to": "jpg"}))["ok"]


@needs_ffmpeg
def test_real_ffmpeg_makes_a_gif_and_an_mp3(writable):
    ff = tm.ffmpeg_exe()
    src = writable / "t.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(src)], check=True, creationflags=tc.NO_WINDOW)
    gif = json.loads(tm.media_convert({"path": str(src), "to": "gif", "width": 160}))
    assert gif["ok"] and Path(gif["output"]["path"]).read_bytes().startswith(b"GIF8")
    mp3 = json.loads(tm.media_convert({"path": str(src), "to": "mp3"}))
    assert mp3["ok"] and Path(mp3["output"]["path"]).stat().st_size > 1000
