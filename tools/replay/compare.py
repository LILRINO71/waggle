"""Base against head: the per-category diff and the one decision "is head worse".

A result is {id, category, tier, status} with status pass | fail | skip. tier is "code" (a deterministic case: no
model, no noise) or "model" (a local model answered or judged: some noise). A case counts only when BOTH sides
scored it; a skip on either side drops it, so an outage is never a regression.

The margin (the one decision, decided here):
  - code tier: ANY case that passed on base and fails on head blocks. These cases have no noise, so one loss is real.
  - model tier: head is worse when its net loss (cases lost minus cases gained) is MORE than
    max(2 cases, 5 % of the paired model-tier cases). Two cases because one local-model answer flips from run to run
    on its own (a base-against-base run showed this, see run.py), so a single flip must never decide a merge; 5 %
    so a large set is not judged by a margin that is too tight. run.py re-runs every flipped case twice more on both
    sides and takes the majority before it gets here, which removes most of that noise.
"""
import math

MARGIN_FLOOR = 2
MARGIN_PCT = 0.05


def margin_for(n_model):
    return max(MARGIN_FLOOR, math.ceil(MARGIN_PCT * n_model))


def _by_id(results):
    return {r["id"]: r for r in results}


def pair(base, head):
    """[(base_result, head_result)] for the cases both sides scored (pass or fail)."""
    b, h = _by_id(base), _by_id(head)
    out = []
    for i, x in b.items():
        y = h.get(i)
        if y and x["status"] != "skip" and y["status"] != "skip":
            out.append((x, y))
    return out


def flips(base, head):
    """Ids of model-tier cases whose pass/fail differs between the two sides: the ones worth re-running."""
    return [x["id"] for x, y in pair(base, head) if x["tier"] == "model" and x["status"] != y["status"]]


def compare(base, head, floor=MARGIN_FLOOR, pct=MARGIN_PCT):
    pairs = pair(base, head)
    cats = {}
    lost, gained = [], []
    for x, y in pairs:
        c = cats.setdefault(x["category"], {"tier": x["tier"], "n": 0, "base": 0, "head": 0})
        c["n"] += 1
        c["base"] += x["status"] == "pass"
        c["head"] += y["status"] == "pass"
        if x["status"] == "pass" and y["status"] == "fail":
            lost.append(x)
        elif x["status"] == "fail" and y["status"] == "pass":
            gained.append(x)
    n_model = sum(1 for x, _ in pairs if x["tier"] == "model")
    margin = max(floor, math.ceil(pct * n_model))
    code_lost = [x["id"] for x in lost if x["tier"] == "code"]
    model_lost = [x["id"] for x in lost if x["tier"] == "model"]
    model_gained = [x["id"] for x in gained if x["tier"] == "model"]
    net_loss = len(model_lost) - len(model_gained)
    reasons = []
    if code_lost:
        reasons.append(f"{len(code_lost)} deterministic case{'s' if len(code_lost) != 1 else ''} that passed on base "
                       f"now fail ({', '.join(code_lost[:6])})")
    if net_loss > margin:
        reasons.append(f"model-scored answers are worse by {net_loss} case{'s' if net_loss != 1 else ''} net "
                       f"({len(model_lost)} lost, {len(model_gained)} gained; the margin is {margin})")
    return {"blocked": bool(reasons), "reasons": reasons, "margin": margin, "paired": len(pairs),
            "paired_model": n_model, "categories": cats, "lost": [x["id"] for x in lost],
            "gained": [x["id"] for x in gained], "code_lost": code_lost, "model_lost": model_lost,
            "model_gained": model_gained, "net_model_loss": net_loss}


def table(cmp_):
    """The per-category diff as plain lines."""
    lines = [f"{'category':<22}{'tier':<7}{'cases':>6}{'base':>6}{'head':>6}{'diff':>6}"]
    for name in sorted(cmp_["categories"]):
        c = cmp_["categories"][name]
        d = c["head"] - c["base"]
        lines.append(f"{name:<22}{c['tier']:<7}{c['n']:>6}{c['base']:>6}{c['head']:>6}{d:>+6}")
    return lines


def majority(votes):
    """pass when at least half of the votes passed (an even split counts as pass: only a clear loss is a loss)."""
    votes = [v for v in votes if v in ("pass", "fail")]
    if not votes:
        return "skip"
    return "pass" if votes.count("pass") * 2 >= len(votes) else "fail"
