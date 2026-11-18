import pytest

import waggle.config as config
from waggle.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "home" / "config.json")
    s = Settings(vault=str(tmp_path / "vault"), jobs_dir=str(tmp_path / "jobs"))
    s.save()
    return s


def bag_embed(texts):
    """Deterministic fake embedder: word-hash bag of words, 64 dims."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in t.lower().replace("search_query: ", "").replace("search_document: ", "").split():
            v[sum(map(ord, w.strip(".,!?#[]"))) % 64] += 1.0
        out.append(v)
    return out
