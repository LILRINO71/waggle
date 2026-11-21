---
name: media-convert
description: "Shrink or convert videos, audio and images."
version: 1.0.0
author: Waggle
platforms: [windows]
metadata:
  hermes:
    category: waggle
    tags: [video, audio, image, ffmpeg, compress, shrink, trim, gif, mp4, mp3, resize, photo, screenshot, clip, match video, discord, email]
    related_skills: [documents-to-pdf, cad-files]
---

# Media convert

Use for robotics-team clips and photos: "get this match video under 25 MB", "make a GIF of the intake test",
"trim 0:12 to 0:40", "pull the audio out", "resize these photos for the website", "turn this PNG into a JPG".

## Tools

- `media_convert` (path, to): video or audio to mp4, webm, gif, mp3, wav, m4a, or one png/jpg frame. `start`/`end`
  (seconds or H:MM:SS) trim; `width` and `fps` shrink; `max_mb` plans the bitrate to fit a size limit (25 for
  Discord and most email) and the answer says `met_target`. GIFs are capped at 30 s.
- `image_convert` (path, to): png, jpg, webp, bmp, gif, pdf. `max_side` or `width`/`height` resize; `quality` for
  jpg/webp. Camera and location metadata are removed, the camera rotation is applied first.

ffmpeg runs at low priority so the PC stays usable, but a long 1080p video still takes a while: tell the owner it
is working, or start it with `background_task` when the file is big.

## Steps

1. Ask for the full path if only a name was given; do not search the whole disk.
2. Choose the smallest change that does the job (trim before shrinking, shrink before re-encoding to webm).
3. Report the before and after sizes and the output path. For a size limit, check `met_target`; if it is false,
   suggest a lower `width` or a shorter clip.
4. Send the result with `send_to_me` (`file`) only if the owner asked for it on their phone.

## Rules

- New files only: originals are never touched or overwritten.
- No screen or camera recording: this converts files that already exist.
- Photos of people may be personal: keep results in the owner's own folders and chat.
