---
name: watch-feeds
description: "What's new on the owner's watched feeds and releases."
version: 1.0.0
author: Waggle
platforms: [windows]
metadata:
  hermes:
    category: waggle
    tags: [rss, feeds, releases, blog, FIRST, FTC, ollama, news, watch]
    related_skills: [rss-feeds]
---

# Watch feeds

Use for "anything new from FIRST?", "did Ollama release something?", "watch this blog for me".
Run in a shell: `python -m waggle.feedwatch check` prints only entries not seen before (a feed's first look just
records what is there). `list` shows the feeds, `add NAME URL [--skip REGEX]` and `remove NAME` change them. The
list is the owner's: removing a default is fine. For a one-off read of any feed use the `rss-feeds` skill.
Tell the owner only what is new and why it matters to them; do not repeat old posts.
