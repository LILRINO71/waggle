import pytest

import waggle.config as config
import waggle.router as router
from waggle.config import Settings


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test ever touches the real ~/.waggle, the real vault, Ollama or Claude."""
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "home" / "config.json")
    monkeypatch.setattr(router, "local_sort", lambda text, s=None, chat=None: ("unsure", 0.0))
    import waggle.quota as quota
    monkeypatch.setattr(quota, "TRANSCRIPTS", tmp_path / "transcripts")  # not the real Claude Code usage
    import waggle.claude_cli as claude_cli
    monkeypatch.setattr(claude_cli, "mcp_servers", lambda claude=None, refresh=False: [])  # no real `claude mcp list`
    monkeypatch.setattr(claude_cli, "find_claude", lambda: None)  # never the real Claude CLI (tests opt in with fakes)
    import waggle.ollama as ollama

    def no_ollama(*a, **k):
        raise ollama.OllamaError("no Ollama in tests")
    monkeypatch.setattr(ollama, "_post", no_ollama)
    import waggle.checklist as checklist
    monkeypatch.setattr(checklist, "home_dir", lambda: tmp_path / "userhome")  # side-damage zones: never the real home
    import waggle.skills as skills

    def no_embed(texts, kind):
        raise ConnectionError("no embeddings in tests")
    monkeypatch.setattr(skills, "_default_embed", no_embed)  # keyword-only unless a test passes embed=
    import waggle.notify as notify
    sent = []
    monkeypatch.setattr(notify, "_toast", lambda text: None)
    monkeypatch.setattr(notify, "to_phone", lambda text, settings=None: sent.append(text) or True)
    real_ntfy = notify.to_ntfy
    monkeypatch.setattr(notify, "to_ntfy", lambda text, *a, post=None, **k: real_ntfy(
        text, *a, post=post or (lambda url, body: sent.append("ntfy:" + body["message"]) or True), **k))
    monkeypatch.setattr(notify, "PHONE_LOG", PhoneLog(sent), raising=False)
    monkeypatch.setattr(notify, "_synth", lambda text, out, settings=None: False)  # no real Kokoro in tests
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))  # never the real Hermes state, cron or `hermes send`
    import waggle.wincred as wincred
    vault_ = {}  # never the real Windows Credential Manager
    monkeypatch.setattr(wincred, "read", lambda target: vault_.get(target))
    monkeypatch.setattr(wincred, "write", lambda target, user, secret: vault_.__setitem__(target, (user, secret)) or True)
    monkeypatch.setattr(wincred, "delete", lambda target: vault_.pop(target, None) is not None)
    import waggle.mailwatch as mailwatch

    def no_imap(*a, **k):
        raise OSError("no real IMAP in tests")
    monkeypatch.setattr(mailwatch, "IMAP_FACTORY", no_imap)  # tests pass a fake IMAP server
    import waggle.watchers as watchers
    monkeypatch.setattr(watchers, "run_gh", lambda args, timeout=40: None)  # no real `gh` calls
    # quiet_hours off: otherwise tests that expect a phone message fail when run between 23:00 and 07:00.
    s = Settings(vault=str(tmp_path / "vault"), jobs_dir=str(tmp_path / "jobs"), quiet_hours=[0, 0],
              vault_git=False,  # tests/test_vault_git*.py turn the vault history on
              hold_for_breaks=False, outreach_ev_enabled=False)  # test_holdq.py / test_outreach_ev.py turn them on
    s.save()
    return s


class PhoneLog:
    """What would reach the phone: anything a raw sender was handed directly, then every row queued in the core
    outbox (notify only queues; the core's sender sends). Push rows read "ntfy:<text>", calls "call:<text>"."""

    def __init__(self, direct):
        self.direct = direct

    def _items(self):
        from waggle.core import outbox
        rows = sorted(outbox.list(limit=100000), key=lambda r: r["id"])
        prefix = {"push": "ntfy:", "call": "call:"}
        return list(self.direct) + [prefix.get(r["kind"], "") + (r["body"] or "") for r in rows]

    def __iter__(self):
        return iter(self._items())

    def __len__(self):
        return len(self._items())

    def __getitem__(self, i):
        return self._items()[i]


@pytest.fixture
def settings(isolated_home):
    return isolated_home


def bag_embed(texts):
    """Deterministic fake embedder: word-hash bag of words, 64 dims."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in t.lower().replace("search_query: ", "").replace("search_document: ", "").split():
            v[sum(map(ord, w.strip(".,!?#[]"))) % 64] += 1.0
        out.append(v)
    return out
