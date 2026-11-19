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
    import waggle.claude_cli as claude_cli
    monkeypatch.setattr(claude_cli, "mcp_servers", lambda claude=None, refresh=False: [])  # no real `claude mcp list`
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
    monkeypatch.setattr(notify, "PHONE_LOG", sent, raising=False)
    s = Settings(vault=str(tmp_path / "vault"), jobs_dir=str(tmp_path / "jobs"))
    s.save()
    return s


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
