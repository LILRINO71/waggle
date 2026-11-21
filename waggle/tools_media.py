"""Video, audio and images for the chat brain: "shrink this match video under 25 MB", "make a GIF of the intake",
"pull the audio out", "resize these photos for the website".

- media_convert: ffmpeg (the static build the pip package imageio-ffmpeg ships, found on PATH or in the Python
  site-packages; no download needed). Runs at below-normal priority with 4 threads so the owner's PC stays usable.
  OBS (the CLI-Anything obs-studio harness) is not installed here, so no recording; ffmpeg only converts files.
- image_convert: Pillow (already installed): format change, resize, quality, metadata stripped by default.

Removal: delete this file, tests/test_tools_media.py, skills/media-convert.
"""
import re
from pathlib import Path

from . import tools_common as tc
from .tools_common import ToolError, guarded

VIDEO_IN = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".gif"}
AUDIO_IN = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus"}
MEDIA_OUT = {"mp4": ".mp4", "webm": ".webm", "gif": ".gif", "mp3": ".mp3", "wav": ".wav", "m4a": ".m4a", "png": ".png",
             "jpg": ".jpg"}
IMAGE_IN = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
IMAGE_OUT = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "webp": ".webp", "bmp": ".bmp", "gif": ".gif", "pdf": ".pdf"}
FFMPEG_EXTRA = (r"%LOCALAPPDATA%\Programs\Python\Python3*\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg*.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe")
TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$|^\d+(?:\.\d+)?$")


def ffmpeg_exe():
    exe = tc.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = tc.which(extra=FFMPEG_EXTRA)
    if not exe:
        raise ToolError("ffmpeg was not found", "pip install imageio-ffmpeg (ships a static ffmpeg), then retry")
    return exe


def seconds(value, name):
    """'90', '1:30' or '0:01:30.5' -> seconds (float); None when empty."""
    if value in (None, ""):
        return None
    v = str(value).strip()
    if not TIME.match(v):
        raise ToolError(f"{name} must be seconds or H:MM:SS (got '{value}')")
    if ":" not in v:
        return float(v)
    parts = [float(x) for x in v.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def duration_of(ffmpeg, path):
    """Length of a media file in seconds, read from `ffmpeg -i` (there is no ffprobe in the static build)."""
    _, out, err = tc.run([ffmpeg, "-hide_banner", "-i", str(path)], timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out + err)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def build_command(ffmpeg, src, dst, to, start=None, end=None, width=None, fps=None, max_mb=None, duration=None):
    """The ffmpeg argument list for one conversion. `duration` (source seconds) is needed for max_mb."""
    cmd = [ffmpeg, "-hide_banner", "-y", "-loglevel", "error", "-threads", "4"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    length = None
    if end is not None:
        length = end - (start or 0)
        if length <= 0:
            raise ToolError("end must be after start")
        cmd += ["-t", str(length)]
    elif duration:
        length = duration - (start or 0)
    scale = f"scale={int(width)}:-2" if width else None
    if to == "gif":
        vf = f"fps={int(fps or 12)},{scale or 'scale=480:-2'}:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse"
        return cmd + ["-vf", vf, "-an", str(dst)]
    if to in ("png", "jpg"):
        return cmd + (["-vf", scale] if scale else []) + ["-frames:v", "1", str(dst)]
    if to in ("mp3", "wav", "m4a"):
        codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "wav": ["-c:a", "pcm_s16le"],
                 "m4a": ["-c:a", "aac", "-b:a", "192k"]}[to]
        return cmd + ["-vn"] + codec + [str(dst)]
    filters = ",".join(f for f in (scale, f"fps={int(fps)}" if fps else None) if f)
    if filters:
        cmd += ["-vf", filters]
    vcodec = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-c:a", "libopus"] if to == "webm" else \
        ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart"]
    if max_mb:
        if not length or length <= 0:
            raise ToolError("could not read the video length, so max_mb cannot be planned")
        total_kbps = max_mb * 8 * 1024 * 0.93 / length  # leave headroom for the container
        video_kbps = int(total_kbps - 128)
        if video_kbps < 80:
            raise ToolError(f"{max_mb} MB is too small for {length:.0f} s of video", "trim it or allow more MB")
        vcodec = [a for a in vcodec if a not in ("-crf", "34", "-b:v", "0")]
        vcodec += ["-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.4)}k", "-bufsize", f"{video_kbps * 2}k"]
    elif to == "mp4":
        vcodec += ["-crf", "23"]
    return cmd + vcodec + [str(dst)]


@guarded
def media_convert(args):
    src = tc.read_path(args.get("path"), VIDEO_IN | AUDIO_IN)
    to = str(args.get("to") or "mp4").lower().lstrip(".")
    if to not in MEDIA_OUT:
        raise ToolError(f"cannot write '{to}'", "to is one of: " + ", ".join(MEDIA_OUT))
    if src.suffix.lower() in AUDIO_IN and to in ("mp4", "webm", "gif", "png", "jpg"):
        raise ToolError("an audio file has no picture; to must be mp3, wav or m4a")
    ffmpeg = ffmpeg_exe()
    start, end = seconds(args.get("start"), "start"), seconds(args.get("end"), "end")
    try:
        max_mb = float(args["max_mb"]) if args.get("max_mb") else None
    except (TypeError, ValueError):
        raise ToolError("max_mb must be a number")
    width = args.get("width")
    if width and not 64 <= int(width) <= 7680:
        raise ToolError("width must be 64-7680")
    duration = duration_of(ffmpeg, src) if (max_mb and end is None) or to == "gif" else None
    if to == "gif":
        span = (end - (start or 0)) if end is not None else ((duration or 0) - (start or 0))
        if span > 30:
            raise ToolError("a GIF longer than 30 s gets huge", "give start/end for a shorter piece")
    dst = tc.out_path(src, MEDIA_OUT[to], args.get("out"), suffix="-small" if max_mb else "")
    cmd = build_command(ffmpeg, src, dst, to, start, end, width, args.get("fps"), max_mb, duration)
    code, out, err = tc.run(cmd, timeout=int(args.get("timeout") or 900), low_priority=True)
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise ToolError("ffmpeg could not convert that", tc.tail(err or out, 400))
    info = tc.file_info(dst)
    res = {"output": info, "from": src.suffix.lstrip(".").lower(), "to": to, "source_size": tc.human_bytes(src.stat().st_size)}
    if max_mb:
        res["target_mb"] = max_mb
        res["met_target"] = dst.stat().st_size <= max_mb * 1024 * 1024
    return tc.ok(**res)


@guarded
def image_convert(args):
    src = tc.read_path(args.get("path"), IMAGE_IN)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ToolError("Pillow is not installed in Waggle's Python", "pip install pillow")
    to = str(args.get("to") or src.suffix.lstrip(".")).lower().lstrip(".")
    if to not in IMAGE_OUT:
        raise ToolError(f"cannot write '{to}'", "to is one of: " + ", ".join(IMAGE_OUT))
    max_side = args.get("max_side")
    width, height = args.get("width"), args.get("height")
    quality = int(args.get("quality") or 88)
    if not 1 <= quality <= 100:
        raise ToolError("quality is 1-100")
    ext = IMAGE_OUT[to]
    same = src.suffix.lower() == ext
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)  # honour the camera's rotation before the metadata is dropped
        before = im.size
        if max_side:
            im.thumbnail((int(max_side), int(max_side)), Image.LANCZOS)
        elif width or height:
            w = int(width) if width else round(im.width * int(height) / im.height)
            h = int(height) if height else round(im.height * int(width) / im.width)
            im = im.resize((max(1, w), max(1, h)), Image.LANCZOS)
        dst = tc.out_path(src, ext, args.get("out"), suffix="-resized" if (same and (max_side or width or height)) else
                          ("-copy" if same else ""))
        if to in ("jpg", "jpeg", "bmp", "pdf") and im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif to in ("jpg", "jpeg", "pdf") and im.mode != "RGB":
            im = im.convert("RGB")
        kw = {}
        if to in ("jpg", "jpeg", "webp"):
            kw["quality"] = quality
        if to in ("jpg", "jpeg"):
            kw["optimize"] = True
        im.save(dst, **kw)  # no exif= argument: location and camera data are not carried over
        after = im.size
    return tc.ok(output=tc.file_info(dst), size_px=f"{before[0]}x{before[1]} -> {after[0]}x{after[1]}",
                 metadata="stripped")


MCP_TOOLS = [
    ({"name": "media_convert", "description": "Convert, trim or shrink a video/audio file with ffmpeg: to mp4, webm, "
      "gif, mp3, wav, m4a or a single png/jpg frame. Optional start/end (seconds or H:MM:SS), width, fps, and max_mb "
      "to fit a size limit (e.g. 25 for Discord/email). Writes a NEW file (never overwrites) and returns its path. "
      "Heavy jobs run at low priority.",
      "parameters": {"type": "object", "properties": {
          "path": {"type": "string"}, "to": {"type": "string", "enum": list(MEDIA_OUT)},
          "start": {"type": "string"}, "end": {"type": "string"}, "width": {"type": "integer"},
          "fps": {"type": "integer"}, "max_mb": {"type": "number"}, "out": {"type": "string"}},
          "required": ["path"]}}, media_convert),
    ({"name": "image_convert", "description": "Convert or resize an image (png, jpg, webp, bmp, gif, tiff -> png, jpg, "
      "webp, bmp, gif, pdf) with Pillow. max_side or width/height to resize, quality for jpg/webp. Location and "
      "camera metadata are stripped. Writes a NEW file and returns its path.",
      "parameters": {"type": "object", "properties": {
          "path": {"type": "string"}, "to": {"type": "string", "enum": list(IMAGE_OUT)},
          "max_side": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"},
          "quality": {"type": "integer"}, "out": {"type": "string"}}, "required": ["path"]}}, image_convert),
]
