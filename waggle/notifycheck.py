"""Notify or not: after a background job produced a result, one local yes/no decides whether it is worth a message
now (borrow-list row 4).

A routine that checks something every morning, or a research objective, ends with a result. Whether that result
meets what the owner asked for, and is worth a buzz right now, is a small judgment the local model can make at no
cost: decide(request, result) -> {"notify": bool, "reason": str, "by": "model" | "failure" | "off"}.

  yes   the caller sends the message as it always did
  no    the result goes into tonight's evening note instead (initiative.keep_for_evening)
  failure (the model is down, slow, or did not answer a yes or no): the evening note too, never a push. A missed
          morning message is better than a wrong buzz, and nothing is lost: the full result is still in the vault.
          (khoj, whose idea this is, notifies on any error; Waggle does the opposite.)
  off   config.json "notify_check_enabled": false -> the old behaviour, always send.

Only results of work that already ran are judged: reminders, mail alerts, calendar heads-ups, delivery and anything
the owner asked for by name never come through here. Every decision is an event of kind "notify_check", counted in the
evening note by gates.evening_line().
"""
import json
import time

from .config import Settings

TIMEOUT_S = 20.0   # not on a hot path: the result already exists; a cold local model gets time to load

SYSTEM = """You help a personal assistant decide whether to interrupt its owner (a student) with the result of a
background job. You get what the owner asked for and what the job found. Say "yes" only when the result answers the
request AND contains something the owner would want to know now (a change, a finding, something to do). Say "no" when
it found nothing new, only repeats what was known, misses the point of the request, is mostly an apology or an
error, or can wait for the evening summary. When unsure, say "no": nothing is lost, it goes into the evening note.
Answer JSON only: {"decision": "yes" or "no", "reason": "one short sentence"}"""


SCHEMA = {"type": "object", "required": ["decision", "reason"], "properties": {
    "decision": {"type": "string", "enum": ["yes", "no"]}, "reason": {"type": "string"}}}


def _ask(messages, settings, timeout):
    from . import ollama
    return ollama.chat_json(messages, schema=SCHEMA, settings=settings, timeout=timeout)


def _verdict(out):
    """True/False from a model answer, or ValueError when it is neither."""
    if isinstance(out, str):
        out = json.loads(out)
    if not isinstance(out, dict):
        raise ValueError("answer is not an object")
    d = out.get("decision", out.get("notify"))
    if isinstance(d, bool):
        return d, str(out.get("reason") or "")
    if isinstance(d, str) and d.strip().lower() in ("yes", "no"):
        return d.strip().lower() == "yes", str(out.get("reason") or "")
    raise ValueError("no yes or no in the answer")


def decide(request, result, *, settings=None, ask=None, subject="", timeout=TIMEOUT_S, log=True):
    """Never raises. See the module doc."""
    s = settings or Settings.load()
    if not getattr(s, "notify_check_enabled", True):
        return {"notify": True, "reason": "notify check is switched off", "by": "off"}
    t0 = time.time()
    try:
        ok, why = _verdict((ask or _ask)([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"What the owner asked for:\n{str(request)[:1500]}\n\n"
                                        f"What the job found:\n{str(result)[:4000]}"}], s, timeout))
        out = {"notify": ok, "reason": why[:200] or ("worth a message now" if ok else "not worth a message now"),
               "by": "model"}
    except Exception as e:  # noqa: BLE001 - model down, too slow, or an answer that is not yes/no
        out = {"notify": False, "by": "failure",
               "reason": f"local model could not answer ({type(e).__name__}: {str(e)[:80]}): kept for the evening note"}
    if log:
        try:
            from . import events
            events.log("notify_check", subject=str(subject)[:80], notify=out["notify"], by=out["by"],
                       reason=out["reason"][:200], ms=int((time.time() - t0) * 1000))
        except Exception:  # noqa: BLE001
            pass
    return out
