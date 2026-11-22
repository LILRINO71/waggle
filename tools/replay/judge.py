"""The replay's judge: one fixed local-model call per case. It is the same for base and head.

It runs through Waggle's own Ollama client (waggle.ollama.chat_json) with the model Waggle itself uses (local_model)
and the options every live caller uses: a JSON schema and temperature 0, never a num_ctx (a different num_ctx would
make Ollama load a second copy of the model). The judge code is always the orchestrator's own, never the code under
test, so a change cannot grade itself.

The judge sees the redacted request, the old accepted reply (the reference) and the new candidate. It answers pass
or fail with one short reason. Version the rubric: a new version means old and new numbers are not comparable.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JUDGE_VERSION = 2
TIMEOUT_S = 240
SCHEMA = {"type": "object", "required": ["verdict", "reason"],
          "properties": {"verdict": {"type": "string", "enum": ["pass", "fail"]}, "reason": {"type": "string"}}}
SYSTEM = ("You grade one reply of a personal assistant called Waggle. Be strict and literal. Answer with JSON: "
          "verdict pass or fail, and one short reason.")
RULES = "\n".join([
    "Judge the candidate as a floor of quality, not as a copy of the reference. The candidate was written later, with no tools and no memory of the earlier conversation, so these differences are NOT failures: another wording, order or length; a different current time, date, weather or status; facts about the owner that could come from the owner's own notes (name, classes, projects); asking a short clarifying question when the request really cannot be understood alone.",
    'Fail if ANY holds:',
    '1. It does not respond to what the request asked, or gives no useful content.',
    '2. It contradicts a stable fact in the reference reply (a capability, a number, a definition, a decision).',
    '3. It claims to have DONE something (sent, saved, created, set, ran, deleted, changed): it had no tools.',
    '4. It states a made-up specific that is wrong or unsupported (an invented file, link, number, event).',
    '5. It is empty, an error message, or an apology with no content.',
    'Otherwise pass.',
])


def build_messages(case, candidate):
    parts = ["REQUEST (from the owner):\n" + (case.get("request") or ""),
             "REFERENCE REPLY (an earlier reply the owner accepted; its facts are true):\n" + (case.get("reference") or "")]
    if case.get("correction"):
        parts.append("The owner CORRECTED the earlier reply with: " + case["correction"] + "\nThe candidate must not "
                     "repeat the mistake that correction points at.")
    parts += ["CANDIDATE REPLY (the new answer to grade):\n" + (candidate or ""), RULES]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def default_chat(messages, schema):
    from waggle import ollama
    from waggle.config import Settings
    s = Settings.load()
    return ollama.chat_json(messages, schema=schema, model=s.local_model, settings=s, timeout=TIMEOUT_S)


def judge(case, candidate, chat=None):
    """(verdict, reason): verdict True (pass), False (fail) or None (could not judge: Ollama down, odd answer)."""
    if not (candidate or "").strip():
        return False, "empty reply"
    try:
        data = (chat or default_chat)(build_messages(case, candidate), SCHEMA)
    except Exception as e:  # noqa: BLE001 - a judge outage is a skip, never a verdict
        return None, f"judge unavailable: {type(e).__name__}"
    if not isinstance(data, dict) or data.get("verdict") not in ("pass", "fail"):
        return None, "judge gave no verdict"
    return data["verdict"] == "pass", str(data.get("reason") or "")[:240]
