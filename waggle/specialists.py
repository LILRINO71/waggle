"""Specialists: the same Sonnet call, briefed as the right expert, plus an optional budgeted second opinion.

1. pick() chooses a specialist from the message for free (keywords, no model call).
2. guide() adds that specialist's brief to Sonnet's system prompt, plus the user's own rules for it from
   the vault note Me/Specialists/<name>.md (personal rules stay in the private vault, not this repo).
3. For high-stakes answers (robotics designs, code) a second Sonnet pass reviews and fixes the reply, but
   only when the Pro budget is comfortably under Waggle's share. Sequential, never parallel: one extra call
   at most, so a busy day costs one call per message, not five.
"""
import re

from . import events, quota, vault
from .config import Settings

SPECIALISTS = {
    "tutor": {
        "match": r"\b(quiz|study|exam|test me|homework|teach me|explain|flashcards?|unit \d|math|calc|derivative|"
                 r"integral|limit|equation|chemistry|physics|history|essay)\b",
        "guide": "Act as a patient tutor. Check understanding before lecturing: ask one quick question or give one "
                 "worked example, then explain the gap. Use real math notation, short steps, and end with a "
                 "practice question. Never just hand over homework answers without the reasoning.",
    },
    "robotics": {
        "match": r"\b(ftc|robot|intake|shooter|drivetrain|chassis|servo|motor|odometry|pedro|roadrunner|teleop|"
                 r"autonomous|auto|cad|onshape|gobilda|rev|mechanism|gear ratio|torque|bracket)\b",
        "guide": "Act as a veteran FTC mentor and mechanical engineer. Prefer proven, simple, robust designs a "
                 "student team can build in time with the parts and shop they have. Give real numbers (gear "
                 "ratios, motor RPM, dimensions, loads) and name failure modes. For code, follow FTC SDK "
                 "conventions and keep tuning possible from the gamepad.",
    },
    "coder": {
        "match": r"\b(code|bug|error|exception|stack ?trace|python|java|javascript|typescript|function|class|api|"
                 r"compile|refactor|git|regex|sql|script)\b",
        "guide": "Act as a senior engineer. Find the root cause before suggesting a fix, show the smallest correct "
                 "change, and say how to verify it. Don't invent APIs; if unsure of a library detail, say so.",
    },
    "planner": {
        "match": r"\b(plan|schedule|organi[sz]e|prioriti[sz]e|deadline|week|todo|to-do|goals?|routine|calendar)\b",
        "guide": "Act as a no-nonsense planner. Turn the request into a short ordered list with time estimates, "
                 "put the most important thing first, and flag anything that won't fit.",
    },
}
REVIEWED = {"robotics", "coder"}  # answers where a wrong detail costs real build time

REVIEW_SYSTEM = """You are the second-opinion reviewer for a personal assistant's answer. Check it for factual,
numerical and logical errors, unsafe advice, and anything that ignores the user's stated constraints.
If it is good, return ok=true and repeat the reply unchanged. If not, return ok=false and a corrected reply
in the same voice and length. Never pad it."""
REVIEW_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}, "reply": {"type": "string"},
                                                  "issues": {"type": "string"}},
                 "required": ["ok", "reply", "issues"]}


def pick(text):
    """The best-matching specialist name, or 'general'. Ties go to the earlier entry."""
    t = (text or "").lower()
    scores = {n: len(re.findall(sp["match"], t)) for n, sp in SPECIALISTS.items()}
    best = max(scores, key=lambda n: scores[n])
    return best if scores[best] else "general"


def guide(name, settings=None):
    sp = SPECIALISTS.get(name)
    if not sp:
        return ""
    out = f"\n\nSpecialist for this message: {name}. {sp['guide']}"
    try:
        own = vault.read(f"Me/Specialists/{name}", settings)
    except Exception:  # noqa: BLE001
        own = ""
    if own and own.strip():
        out += f"\nThe user's own rules for this kind of request:\n{own.strip()[:2000]}"
    return out


def review_allowed(settings=None):
    """A second opinion only while Waggle has used under half its share of the window."""
    s = settings or Settings.load()
    ok, why = quota.can_escalate(s.router_model, s)
    if not ok:
        return False, why
    st = quota.status(s)
    if st["waggle_units"] > 0.5 * s.waggle_share * st["window_estimate_units"]:
        return False, "saving budget: no second opinion this window"
    return True, "ok"


def review(text, reply, name, settings=None, asker=None, allowed=None):
    """Return (reply, reviewed: bool). Falls back to the original reply on any problem."""
    s = settings or Settings.load()
    if name not in REVIEWED or len(reply or "") < 300:
        return reply, False
    ok, why = (allowed or review_allowed)(s)
    if not ok:
        events.log("review", specialist=name, ran=False, why=why)
        return reply, False
    from . import claude_cli
    asker = asker or claude_cli.ask
    try:
        res = asker(f"User message:\n{text}\n\nDraft reply:\n{reply}", model=s.router_model,
                    system=REVIEW_SYSTEM + guide(name, s), schema=REVIEW_SCHEMA, purpose="review")
    except Exception as e:  # noqa: BLE001
        events.log("review", specialist=name, ran=False, why=f"error: {e}"[:200])
        return reply, False
    d = res.get("json") or {}
    fixed = (d.get("reply") or "").strip()
    events.log("review", specialist=name, ran=True, ok=bool(d.get("ok")), issues=(d.get("issues") or "")[:300])
    if d.get("ok") or not fixed:
        return reply, True
    return fixed, True
