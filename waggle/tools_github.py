"""GitHub portfolio audit for the chat brain: "is my GitHub ready for college applications?".

Read-only. Uses the GitHub CLI (`gh`, already signed in as the owner) to list the owner's public repos and, per repo,
check the things a reviewer sees in ten seconds: description, topics, a README with substance, a screenshot, a
install/usage section, a license, a CI workflow, a live demo link, and whether anything was pushed lately. Returns a
score per repo, the fixes that would lift the most repos, and a suggested six to pin (AI/ML work first). It changes
nothing on GitHub: improving a repo is a separate step the brain proposes and the owner approves.

Tool: portfolio_audit. Removal: delete this file, tests/test_tools_github.py, skills/github-portfolio-audit.
"""
import base64
import json
import re
import time
from datetime import datetime, timezone

from . import tools_common as tc
from .tools_common import ToolError, guarded

GH_EXTRA = (r"C:\Program Files\GitHub CLI\gh.exe", r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe")
LIST_FIELDS = ("name,description,isFork,isArchived,pushedAt,stargazerCount,primaryLanguage,repositoryTopics,"
               "licenseInfo,homepageUrl,url,isEmpty")
AI_WORDS = re.compile(r"\b(ai|ml|llm|agent|agents|neural|deep.?learning|machine.?learning|model|models|pytorch|torch|"
                      r"tensorflow|transformer|rag|embedding|vision|nlp|classifier|reinforcement|simulation|"
                      r"simulator|computer.?vision|robot\w*|data.?science)\b", re.I)
WEIGHTS = {"description": 15, "topics": 15, "readme": 20, "screenshot": 10, "usage": 10, "license": 5, "ci": 10,
           "recent": 10, "demo": 5}
FIX_TEXT = {"description": "add a one-line description", "topics": "add 3+ topics", "readme": "write a README (what, why, how)",
            "screenshot": "add a screenshot or GIF to the README", "usage": "add an Install / Usage section",
            "license": "add a license", "ci": "add a CI workflow (tests on push)", "recent": "no push in 90 days",
            "demo": "link a live demo or write-up"}


def gh_exe():
    exe = tc.which("gh", extra=GH_EXTRA)
    if not exe:
        raise ToolError("the GitHub CLI (gh) is not installed", "install gh and run `gh auth login`")
    return exe


def default_runner(args, timeout=60):
    """(returncode, stdout) of one gh call; no prompts, no colour, no console window."""
    import os
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
    code, out, err = tc.run([gh_exe(), *args], timeout=timeout, env=env)
    return code, out if code == 0 else (err or out)


def _json(runner, args):
    code, out = runner(args)
    if code != 0:
        raise ToolError("GitHub said no: " + tc.tail(out, 200), "check `gh auth status`")
    try:
        return json.loads(out)
    except ValueError:
        raise ToolError("GitHub returned something unreadable")


def _api(runner, path):
    """gh api <path> -> parsed JSON, or None when it 404s (no README, no workflows folder)."""
    code, out = runner(["api", path])
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def check_readme(readme_json):
    """What a README says about the repo: {'readme': bool, 'screenshot': bool, 'usage': bool, 'chars': n}."""
    if not readme_json:
        return {"readme": False, "screenshot": False, "usage": False, "chars": 0}
    try:
        text = base64.b64decode(readme_json.get("content", "")).decode("utf-8", "replace")
    except Exception:
        text = ""
    plain = re.sub(r"```.*?```", "", text, flags=re.S)
    return {"readme": len(plain.strip()) >= 500,
            "screenshot": bool(re.search(r"!\[[^\]]*\]\([^)]+\)|<img\s|\.(png|gif|jpg|jpeg|webp)\b", text, re.I)),
            "usage": bool(re.search(r"^#{1,4}\s*(install|installation|usage|getting started|quick ?start|run|how to)",
                                    text, re.I | re.M)),
            "chars": len(plain.strip())}


def days_since(iso, now=None):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 9999
    return ((now or datetime.now(timezone.utc)) - t).days


def score_repo(repo, readme, has_ci, now=None):
    topics = [t["name"] for t in (repo.get("repositoryTopics") or [])]
    checks = {"description": bool((repo.get("description") or "").strip()), "topics": len(topics) >= 3,
              "readme": readme["readme"], "screenshot": readme["screenshot"], "usage": readme["usage"],
              "license": bool(repo.get("licenseInfo")), "ci": has_ci,
              "recent": days_since(repo.get("pushedAt"), now) <= 90, "demo": bool((repo.get("homepageUrl") or "").strip())}
    score = sum(WEIGHTS[k] for k, v in checks.items() if v)
    text = " ".join([repo.get("name", ""), repo.get("description") or "", " ".join(topics)])
    lang = (repo.get("primaryLanguage") or {}).get("name") or ""
    ai = bool(AI_WORDS.search(text)) or (lang == "Python" and bool(AI_WORDS.search(repo.get("description") or "")))
    return {"name": repo["name"], "score": score, "ai_ml": ai, "language": lang, "stars": repo.get("stargazerCount", 0),
            "days_since_push": days_since(repo.get("pushedAt"), now), "missing": [k for k, v in checks.items() if not v],
            "description": (repo.get("description") or "")[:120], "url": repo.get("url")}


def audit(runner, owner="", limit=25, include_forks=False, now=None):
    args = ["repo", "list"] + ([owner] if owner else []) + ["--limit", "100", "--visibility", "public", "--json", LIST_FIELDS]
    repos = [r for r in _json(runner, args) if not r.get("isEmpty") and not r.get("isArchived")
             and (include_forks or not r.get("isFork"))]
    repos.sort(key=lambda r: r.get("pushedAt") or "", reverse=True)
    login = owner or (repos[0]["url"].split("/")[3] if repos else "")
    rows = []
    for r in repos[:limit]:
        readme = check_readme(_api(runner, f"repos/{login}/{r['name']}/readme"))
        ci = bool(_api(runner, f"repos/{login}/{r['name']}/contents/.github/workflows"))
        rows.append(score_repo(r, readme, ci, now))
    if not rows:
        raise ToolError("no public repositories found" + (f" for {owner}" if owner else ""))
    counts = {}
    for row in rows:
        for m in row["missing"]:
            counts[m] = counts.get(m, 0) + 1
    fixes = [{"fix": FIX_TEXT[k], "repos": n, "points_each": WEIGHTS[k]}
             for k, n in sorted(counts.items(), key=lambda kv: -kv[1] * WEIGHTS[kv[0]])][:5]
    pins = sorted(rows, key=lambda r: (r["ai_ml"], r["score"], -r["days_since_push"]), reverse=True)[:6]
    avg = round(sum(r["score"] for r in rows) / len(rows))
    return {"owner": login, "repos_checked": len(rows), "average_score": avg,
            "suggested_pins": [{"name": p["name"], "score": p["score"], "ai_ml": p["ai_ml"]} for p in pins],
            "biggest_fixes": fixes, "repos": sorted(rows, key=lambda r: r["score"])}


@guarded
def portfolio_audit(args, runner=None):
    runner = runner or default_runner
    owner = str(args.get("owner") or "").strip()
    if owner and not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
        raise ToolError("owner must be a GitHub username")
    limit = max(1, min(int(args.get("limit") or 25), 40))
    res = audit(runner, owner, limit, bool(args.get("include_forks")))
    from . import security
    return security.wrap_untrusted("github-portfolio-audit",
                                   json.dumps({"ok": True, **res, "checked_at": time.strftime("%Y-%m-%d")},
                                              ensure_ascii=False), limit=40000)


MCP_TOOLS = [
    ({"name": "portfolio_audit", "description": "Read-only audit of the user's public GitHub repos for a college "
      "portfolio, through the signed-in GitHub CLI: per-repo score (description, topics, README, screenshot, usage, "
      "license, CI, demo link, recent push), the fixes that lift the most repos, and 6 repos to pin (AI/ML first). "
      "Changes nothing on GitHub.",
      "parameters": {"type": "object", "properties": {
          "owner": {"type": "string", "description": "GitHub username; default the signed-in user"},
          "limit": {"type": "integer", "description": "repos to check, newest push first (default 25)"},
          "include_forks": {"type": "boolean"}}, "required": []}}, portfolio_audit),
]
