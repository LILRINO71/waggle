"""Find an existing tool before building one: search public catalogs, offer the best, install only with a yes.

The owner's rule: most tools are already built, Waggle's job is to connect them. When the chat brain lacks an ability
it calls find_tool(need). This module searches, in order (stdlib HTTP, results cached 24 h, no keys):
  1. the Hermes skills hub          (`hermes skills search --json`)
  2. the official MCP registry      (registry.modelcontextprotocol.io)
  3. the CLI-Anything hub catalog   (hkuds.github.io/CLI-Anything/registry.json)
  4. GitHub through `gh`            (licence MIT / Apache-2.0 / BSD, pushed in the last 6 months, 50+ stars)
Each result is one candidate: name, kind (skill / mcp / cli), licence, stars, last push, install command, risk notes.
Ranking: official > well maintained > fits Windows > no account or key > small (relevance to the need comes first).

install_tool(candidate_id) is ASK in waggle/policy.py every time, even in the owner's own turn: third-party code
runs. On a yes it installs pinned and isolated, registers, smoke checks, and writes the ledger:
  skill  -> `hermes skills install` (Hermes scans it first; lands in ~/.hermes/skills)
  MCP    -> %USERPROFILE%\\.waggle-tools\\<name> (npm --ignore-scripts at the registry's exact version, a venv with
            pypi==version, or a git commit at least two weeks old); the chat brain's MCP config gets the entry
  CLI    -> the same folder, a venv, a git commit at least two weeks old (CLI-Anything installs from its repo)
A smoke check that fails rolls the install back. Ledger: ~/.waggle/tools_installed.json.
By hand (needs a real terminal, so the chat brain's shell cannot use it):
  python -m waggle.toolfinder find "convert units" | install ID | uninstall NAME | list
Removal of the whole feature: delete this file, tests/test_toolfinder.py, the four rows in waggle/policy.py and the
"waggle.toolfinder" entry in waggle_mcp._load_providers.
"""
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import tools_common as tc
from .config import data_path

TTL = 24 * 3600
PIN_MIN_AGE_DAYS = 14  # a git pin is the newest commit at least this old (the same rule as winmcp's pinned release)
UA = "waggle-toolfinder/1"
REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"
CLI_HUB = "https://hkuds.github.io/CLI-Anything/registry.json"
CLI_HUB_REPO = "HKUDS/CLI-Anything"
PERMISSIVE = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause"}
SPDX = {"mit": "MIT", "apache-2.0": "Apache-2.0", "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause"}
COPYLEFT = re.compile(r"gpl|agpl|lgpl|mpl|eupl|cc-by-sa|sspl", re.I)
MIN_STARS = 50
MIN_RELEVANCE = 0.34  # a candidate must match about a third of the need's words, name hits counting double
MAX_AGE_DAYS = 183  # "pushed in the last 6 months"
# Vendors whose own repos count as official (a short list, not a claim of completeness).
OFFICIAL_OWNERS = {"modelcontextprotocol", "anthropics", "nousresearch", "microsoft", "googleworkspace", "github",
                   "cloudflare", "hkuds", "openai", "google", "aws", "awslabs", "stripe", "notion", "makenotion",
                   "slackapi", "atlassian", "jetbrains", "docker", "mongodb", "supabase", "vercel", "hashicorp"}
TELEMETRY_OFF = {"DO_NOT_TRACK": "1", "ANONYMIZED_TELEMETRY": "false", "TELEMETRY_DISABLED": "1",
                 "NEXT_TELEMETRY_DISABLED": "1", "SCARF_NO_ANALYTICS": "true", "PYTHONUTF8": "1",
                 "PIP_DISABLE_PIP_VERSION_CHECK": "1", "NPM_CONFIG_UPDATE_NOTIFIER": "false"}

STOP = set("a an the to of in on for with from and or into my me i it that this can could please want need needs able "
           "use using tool tools mcp server skill cli app something someone anything do does make get help how some "
           "some be is are via by at as you your we us our they them its just also able like".split())
GENERIC_VERBS = {"read", "open", "show", "find", "control", "play", "run", "work", "check", "send", "write", "convert"}


# ---- plumbing: every outside call is one small function so tests can replace it ------------------------------------

def tools_root():
    return Path.home() / ".waggle-tools"


def _run(cmd, timeout=120, env=None, cwd=None):
    """(returncode, stdout, stderr); no console window; the tree is killed on a timeout."""
    return tc.run(cmd, timeout=timeout, env=env, cwd=cwd)


def _http_get(url, timeout=20):
    if not url.startswith("https://"):
        raise ValueError("only https")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https only, checked above)
        return json.loads(r.read().decode("utf-8", "replace"))


def _http_post(url, body, timeout=20):
    if not url.startswith("https://"):
        raise ValueError("only https")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.status, r.read().decode("utf-8", "replace")


def _hermes_exe():
    from . import skills
    exe = Path(skills.HERMES_HOME) / "bin" / "hermes.exe"
    return exe if exe.exists() else Path(skills.HERMES_HOME) / "bin" / "hermes"


def _run_hermes(args, timeout=60):
    """(returncode, text) of one `hermes` call under Waggle's Hermes home."""
    from . import skills
    env = dict(os.environ, HERMES_HOME=str(skills.HERMES_HOME), NO_COLOR="1")
    code, out, err = _run([str(_hermes_exe()), *args], timeout=timeout, env=env)
    return code, re.sub(r"\x1b\[[0-9;]*m", "", (out or "") + (err or "")).strip()


def _gh(args, timeout=40):
    """Parsed JSON of one `gh` call (already signed in as the owner), or raises RuntimeError."""
    from . import tools_github
    code, out = tools_github.default_runner(args, timeout=timeout)
    if code != 0:
        raise RuntimeError("gh: " + tc.tail(out, 200))
    return json.loads(out)


# ---- cache (24 h) -------------------------------------------------------------------------------------------------

def _cache_file():
    return data_path("toolfinder_cache.json")


def _cache_load():
    try:
        c = json.loads(_cache_file().read_text(encoding="utf-8"))
        return c if isinstance(c, dict) and isinstance(c.get("e"), dict) else {"v": 1, "e": {}}
    except (OSError, ValueError):
        return {"v": 1, "e": {}}


def _cache_save(c):
    p = _cache_file()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _cached(key, now, fetch, refresh=False):
    """(data, from_cache). A failed fetch is never cached."""
    c = _cache_load()
    e = c["e"].get(key)
    if e and not refresh and now - e["t"] < TTL:
        return e["d"], True
    data = fetch()
    c = _cache_load()
    c["e"] = {k: v for k, v in c["e"].items() if now - v["t"] < TTL}
    c["e"][key] = {"t": now, "d": data}
    _cache_save(c)
    return data, False


# ---- the need -> search words ---------------------------------------------------------------------------------------

def keywords(need):
    words = []
    for w in re.findall(r"[a-z0-9][a-z0-9+#.\-]*", (need or "").lower()):
        w = w.strip(".-")
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]  # pdfs -> pdf, tables -> table
        if w and w not in STOP and w not in words:
            words.append(w)
    return words[:6]


def search_terms(kws):
    """The few words sent to the catalogs: the specific words first, then generic verbs, at most three."""
    picked = [w for w in kws if w not in GENERIC_VERBS]
    return (picked + [w for w in kws if w in GENERIC_VERBS])[:3]


def _clean(text, limit=220):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _slug_of(url):
    m = re.search(r"github\.com[/:]([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?:[/#?]|$)", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _iso_age_days(iso, now):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return max(0.0, (now - t) / 86400)


def _cand(**kw):
    c = {"id": "", "name": "", "title": "", "kind": "", "source": "", "description": "", "repo": "", "license": "",
         "stars": None, "pushed": "", "install_command": "", "install": {}, "installable": False, "why_not": "",
         "needs": [], "risks": [], "official": False, "trust": "", "size_kb": None, "language": "", "score": 0.0}
    c.update(kw)
    c["description"] = _clean(c["description"])
    c["name"] = _clean(c["name"], 80)
    c["title"] = _clean(c["title"], 80)
    return c


# ---- source 1: Hermes skills hub ------------------------------------------------------------------------------------

def src_hermes(terms, now):
    code, text = _run_hermes(["skills", "search", " ".join(terms), "--limit", "8", "--json"], timeout=90)
    start = text.find("[")
    if code != 0 or start < 0:
        raise RuntimeError("hermes skills search: " + _clean(text, 120))
    rows = json.loads(text[start:text.rfind("]") + 1])
    out = []
    for r in rows:
        ident = str(r.get("identifier") or "").strip()
        if not ident:
            continue
        trust = str(r.get("trust_level") or "community")
        c = _cand(id=f"skill:{ident}", name=r.get("name") or ident, kind="skill", source=f"hermes-hub/{r.get('source')}",
                  description=r.get("description"), trust=trust, official=trust in ("builtin", "official", "trusted"),
                  install_command=f"hermes skills install {ident} --yes", license="unknown (Hermes scans it first)",
                  install={"type": "hermes-skill", "identifier": ident}, installable=True)
        out.append(c)
    return out


# ---- source 2: the official MCP registry ----------------------------------------------------------------------------

def src_registry(terms, now):
    out, seen = [], set()
    for t in terms[:3]:
        url = REGISTRY + "?" + urllib.parse.urlencode({"search": t, "limit": 30, "version": "latest"})
        for row in (_http_get(url).get("servers") or []):
            s = row.get("server") or {}
            if s.get("name") and s["name"] not in seen:
                seen.add(s["name"])
                out.append(_registry_cand(s))
    return out


def _registry_cand(s):
    name = s["name"]
    repo = _slug_of((s.get("repository") or {}).get("url"))
    needs = []
    install, cmd, why = {}, "", ""
    for pkg in s.get("packages") or []:
        if (pkg.get("transport") or {}).get("type", "stdio") != "stdio":
            continue
        reg, ident, ver = pkg.get("registryType"), pkg.get("identifier"), pkg.get("version")
        needs += [e["name"] for e in pkg.get("environmentVariables") or [] if e.get("isRequired") and e.get("name")]
        required_args = [a for a in (pkg.get("packageArguments") or []) if a.get("isRequired")]
        if reg in ("npm", "pypi") and ident and ver and not install:
            install = {"type": reg, "package": ident, "version": ver}
            cmd = (f"npm install --ignore-scripts {ident}@{ver}" if reg == "npm" else f"pip install {ident}=={ver}")
            if required_args:
                install, why = {}, "needs command-line arguments the registry does not fill in"
        elif reg == "oci" and not install:
            why = "ships only as a Docker image"
        elif reg in ("nuget", "mcpb") and not install:
            why = f"{reg} package: not handled"
    if not install:
        for rem in s.get("remotes") or []:
            hdrs = [h.get("name") for h in rem.get("headers") or [] if h.get("isRequired")]
            if rem.get("type") in ("streamable-http", "sse") and rem.get("url", "").startswith("https://"):
                if hdrs:
                    needs += hdrs
                    why = why or "a hosted server that needs an API key or account"
                else:
                    install = {"type": "remote-mcp", "url": rem["url"]}
                    cmd, why = f"register {rem['url']} in the chat brain's MCP config", ""
                    break
    if needs:
        install = {}
        why = f"needs {', '.join(sorted(set(needs))[:3])} (an API key or account); Waggle does not handle keys"
    kind_risks = []
    if install.get("type") == "remote-mcp":
        kind_risks.append("hosted service: what you ask it leaves this PC")
    c = _cand(id=f"mcp:{name}", name=name.split("/")[-1], title=s.get("title") or "", kind="mcp",
              source="mcp-registry", description=s.get("description"), repo=repo, install=install,
              install_command=cmd or "", installable=bool(install), why_not=why, needs=sorted(set(needs)),
              risks=kind_risks)
    if install.get("type") == "npm":
        c["risks"].append("needs Node (installed, scripts off)")
    elif install.get("type") == "pypi":
        c["risks"].append("Python package in its own venv")
    return c


# ---- source 3: the CLI-Anything hub catalog --------------------------------------------------------------------------

_CLI_PIP = re.compile(r"^pip install git\+(https://github\.com/[\w.\-]+/[\w.\-]+?\.git)(?:@\S+)?#subdirectory=(\S+)$")


def src_cli_anything(terms, now):
    data, _ = _cached("cli-anything-catalog", now, lambda: _http_get(CLI_HUB, timeout=30))
    out = []
    for c in data.get("clis") or []:
        m = _CLI_PIP.match(str(c.get("install_cmd") or "").strip())
        install = {"type": "git-pip", "repo": _slug_of(m.group(1)), "subdirectory": m.group(2),
                   "entry": c.get("entry_point") or ""} if m else {}
        risks = []
        if c.get("requires"):
            risks.append("needs " + _clean(c["requires"], 90))
        out.append(_cand(id=f"cli:{c.get('name')}", name=c.get("name"), title=c.get("display_name") or "", kind="cli",
                         source="cli-anything-hub", description=c.get("description"), repo=CLI_HUB_REPO,
                         install=install, install_command=(c.get("install_cmd") or "") + " (pinned to a commit)",
                         installable=bool(install), why_not="" if install else "no pip command in the catalog",
                         risks=risks, official=False, license="Apache-2.0", trust="hub"))
    return out


# ---- source 4: GitHub through gh ----------------------------------------------------------------------------------------

GH_FIELDS = "fullName,description,stargazersCount,pushedAt,license,language,size,isArchived,url"


def src_github(terms, now):
    since = dt.datetime.fromtimestamp(now - MAX_AGE_DAYS * 86400, dt.timezone.utc).date().isoformat()
    base = ["search", "repos", "--stars", f">={MIN_STARS}", "--updated", f">={since}", "--sort", "stars",
            "--limit", "15", "--json", GH_FIELDS]
    rows = []
    core = ([w for w in terms if w not in GENERIC_VERBS] or terms)[:2]  # gh ANDs the words: keep the specific ones
    for words in (core + ["mcp"], core):
        try:
            rows += _gh([*base[:2], *words, *base[2:]])
        except RuntimeError:
            if not rows:
                raise
    out, seen = [], set()
    for r in rows:
        slug = r.get("fullName") or ""
        if slug in seen or not slug:
            continue
        seen.add(slug)
        lic = ((r.get("license") or {}).get("key") or "").lower()
        age = _iso_age_days(r.get("pushedAt"), now)
        if r.get("isArchived") or lic not in PERMISSIVE or (r.get("stargazersCount") or 0) < MIN_STARS \
                or age is None or age > MAX_AGE_DAYS:
            continue
        c = _gh_cand(r, slug, lic)
        out.append(c)
    return out


def _gh_cand(r, slug, lic):
    text = f"{slug} {r.get('description') or ''}".lower()
    kind = "mcp" if re.search(r"\bmcp\b|model context protocol", text) else \
        "skill" if re.search(r"\bskills?\b", text) else "cli"
    lang = r.get("language") or ""
    install, why, cmd = {}, "", ""
    if lang == "Python" and kind == "mcp":  # a Python MCP server has a command; a plain Python repo may only be a library
        install = {"type": "git-pip", "repo": slug, "subdirectory": "", "entry": ""}
        cmd = f"pip install git+https://github.com/{slug}.git@<commit two weeks old>"
    else:
        why = (f"no packaged installer Waggle can verify ({lang or 'unknown language'}); needs manual setup from the README")
        cmd = f"see https://github.com/{slug}"
    return _cand(id=f"gh:{slug}", name=slug.split("/")[1], title=slug, kind=kind, source="github", description=r.get("description"),
                 repo=slug, license=lic, stars=r.get("stargazersCount"), pushed=str(r.get("pushedAt") or "")[:10],
                 size_kb=r.get("size"), language=lang, install=install, install_command=cmd, installable=bool(install),
                 why_not=why)


SOURCES = (("hermes", src_hermes), ("registry", src_registry), ("cli-anything", src_cli_anything),
           ("github", src_github))


# ---- GitHub facts for candidates that do not carry them ----------------------------------------------------------

def repo_meta(slug, now):
    def fetch():
        j = _gh(["api", f"repos/{slug}"])
        return {"license": ((j.get("license") or {}).get("key") or "").lower(), "stars": j.get("stargazers_count"),
                "pushed": str(j.get("pushed_at") or "")[:10], "size_kb": j.get("size"), "language": j.get("language") or "",
                "archived": bool(j.get("archived"))}
    return _cached(f"repo|{slug}", now, fetch)[0]


def resolve_npm(c, now):
    """A GitHub TypeScript/JavaScript MCP server or CLI is installable when its package.json names an npm package whose
    registry entry points back at the same repo: the newest version at least two weeks old that has a command."""
    import base64
    slug = c["repo"]

    def fetch():
        try:
            pj = json.loads(base64.b64decode(_gh(["api", f"repos/{slug}/contents/package.json"]).get("content", "")))
            name = pj.get("name") or ""
            if not name or pj.get("private"):
                return {}
            doc = _http_get("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@"), timeout=20)
        except (RuntimeError, ValueError, OSError):
            return {}
        if slug.lower() not in json.dumps(doc.get("repository") or {}).lower():
            return {}
        best = ""
        for ver, v in (doc.get("versions") or {}).items():
            age = _iso_age_days((doc.get("time") or {}).get(ver), now)
            if "-" not in ver and v.get("bin") and age is not None and age >= PIN_MIN_AGE_DAYS:
                if not best or (age < _iso_age_days(doc["time"][best], now)):
                    best = ver
        return {"package": name, "version": best} if best else {}
    got = _cached(f"npm|{slug}", now, fetch)[0]
    if got:
        c["install"] = {"type": "npm", **got}
        c["install_command"] = f"npm install --ignore-scripts {got['package']}@{got['version']}"
        c["installable"], c["why_not"] = True, ""
        c["risks"].append("npm package at an exact version (scripts off)")
    return c


def enrich(cands, now, limit=8):
    """Fill licence, stars and last push from GitHub for the candidates that name a repo (at most `limit` calls)."""
    done = 0
    for c in cands:
        if c["source"] == "cli-anything-hub":
            continue  # the repo's stars and push date belong to the whole hub, not to this one harness
        if c["repo"] and c["stars"] is None and done < limit:
            try:
                m = repo_meta(c["repo"], now)
            except (RuntimeError, ValueError):
                continue
            done += 1
            c.update({k: m[k] for k in ("stars", "pushed", "size_kb", "language")})
            if c["source"] != "cli-anything-hub":
                c["license"] = m["license"] or "unknown"
            if m["archived"]:
                c["risks"].append("repository is archived")
    return cands


# ---- ranking + risk notes ------------------------------------------------------------------------------------------------

def relevance(c, kws):
    """0..1: how many of the need's words the name or description holds (a name hit counts double)."""
    if not kws:
        return 0.0
    name = f"{c['name']} {c['title']}".lower()
    body = f"{c['description']} {c['repo']}".lower()
    pts = 0.0
    for w in kws:
        weight = 0.5 if w in GENERIC_VERBS else 1.0
        if w in name:
            pts += 2 * weight
        elif w in body:
            pts += weight
    return min(1.0, pts / (len(kws) * 1.5))


def risk_notes(c, now):
    notes = list(c["risks"])
    desc = f"{c['description']} {c['why_not']}".lower()
    lic = (c["license"] or "").lower()
    if COPYLEFT.search(lic):
        notes.append(f"copyleft licence ({c['license']})")
    elif not lic or lic.startswith("unknown") and c["kind"] != "skill":
        notes.append("licence unknown")
    if c["needs"]:
        notes.append("needs " + ", ".join(c["needs"][:3]) + " (key or account)")
    elif re.search(r"api key|account|sign.?up|log.?in|premium|subscription|oauth|token",
                   re.sub(r"\b(no|without|zero|keyless|free of)\b[^.,;]{0,20}\b(api key|keys?|account|sign.?up|log.?in|token)s?\b",
                          "", desc)):
        notes.append("probably needs an account or key")
    if re.search(r"\b(mac(os)?|linux|raspberry pi)\b[^.]{0,20}\b(only|exclusive)\b|\bon any linux\b|^(mac(os)?|linux)\b", desc):
        notes.append("not for Windows")
    if c["source"] == "github":
        notes.append("Node" if c["language"] in ("TypeScript", "JavaScript") else c["language"] or "")
    if c["stars"] is not None and c["stars"] < 10:
        notes.append(f"barely used ({c['stars']} stars)")
    if c["size_kb"] and c["size_kb"] > 50000:
        notes.append(f"large repo ({c['size_kb'] // 1024} MB)")
    if c["pushed"]:
        age = _iso_age_days(c["pushed"], now)
        if age is not None and age > 365:
            notes.append("not updated in over a year")
    if c["trust"] == "community":
        notes.append("community skill (Hermes scans it before install)")
    if c["kind"] == "mcp":
        notes.append("telemetry switched off in its config")
    seen = []
    for n in notes:
        if n and n not in seen:
            seen.append(n)
    return seen


def score(c, kws, now):
    """Relevance first (it gates), then official > well maintained > fits Windows > no account or key > small."""
    rel = relevance(c, kws)
    s = 40.0 * rel
    owner = c["repo"].split("/")[0].lower() if c["repo"] else ""
    if c["official"] or (owner in OFFICIAL_OWNERS and c["source"] != "cli-anything-hub"):
        c["official"] = True
        s += 25
    elif c["source"] in ("mcp-registry", "cli-anything-hub"):
        s += 8
    if c["stars"]:
        s += min(15.0, math.log10(c["stars"] + 1) * 5)
    age = _iso_age_days(c["pushed"], now) if c["pushed"] else None
    if age is not None:
        s += max(0.0, 10.0 * (1 - age / 180.0))
    text = " ".join(c["risks"]).lower()
    if "not for windows" in text:
        s -= 25
    if c["install"].get("type") == "oci":
        s -= 10
    if "hosted service" in text:
        s -= 10
    if c["needs"]:
        s -= 15
    elif "needs an account" in text or "probably needs" in text:
        s -= 6
    if c["size_kb"]:
        s -= min(5.0, c["size_kb"] / 20000.0)
    if COPYLEFT.search((c["license"] or "").lower()):
        s -= 5
    if c["stars"] is not None and c["stars"] < 10:
        s -= 10
    s += 10 if c["installable"] else -15
    if rel == 0:
        s -= 50
    c["score"] = round(s, 1)
    return c["score"]


# ---- search --------------------------------------------------------------------------------------------------------------

def search(need, now=None, refresh=False, sources=None):
    """Ranked candidates for `need` across the four catalogs. A catalog that fails is listed in errors, never fatal."""
    now = time.time() if now is None else now
    kws = keywords(need)
    terms = search_terms(kws)
    res = {"need": _clean(need, 200), "keywords": kws, "candidates": [], "errors": [], "cached": True}
    if not terms:
        res["errors"].append("say what the tool should do")
        return res
    pool = []
    for name, fn in SOURCES:
        if sources and name not in sources:
            continue
        try:
            data, hit = _cached(f"{name}|{' '.join(terms)}", now, lambda fn=fn: fn(terms, now), refresh)
        except Exception as e:  # noqa: BLE001 (a catalog that is down must not stop the others)
            res["errors"].append(f"{name}: {type(e).__name__}: {_clean(e, 120)}")
            continue
        res["cached"] = res["cached"] and hit
        pool += data
    merged = {}
    for c in pool:  # one candidate per repo (CLI-Anything's many CLIs share a repo, so those stay apart)
        key = c["id"] if c["source"] == "cli-anything-hub" else (c["repo"] or c["id"])
        old = merged.get(key)
        if old is None:
            merged[key] = c
        elif (c["installable"], c["source"] != "github") > (old["installable"], old["source"] != "github"):
            c["stars"] = c["stars"] if c["stars"] is not None else old["stars"]
            c["pushed"] = c["pushed"] or old["pushed"]
            c["license"] = c["license"] if c["license"] not in ("", "unknown") else old["license"]
            merged[key] = c
    cands = [c for c in merged.values() if relevance(c, kws) >= MIN_RELEVANCE]
    top_pre = sorted(cands, key=lambda c: -relevance(c, kws))[:14]
    enrich(top_pre, now)
    for c in [c for c in top_pre if c["source"] == "github" and not c["installable"]
              and c["language"] in ("TypeScript", "JavaScript") and c["kind"] in ("mcp", "cli")][:4]:
        resolve_npm(c, now)
    for c in cands:
        c["risks"] = risk_notes(c, now)
        score(c, kws, now)
    cands.sort(key=lambda c: -c["score"])
    res["candidates"] = cands
    for c in cands[:10]:  # find_tool's answer must stay installable for a while
        _cached(f"cand|{c['id']}", now, lambda c=c: c, refresh=True)
    return res


def top(need, n=3, **kw):
    r = search(need, **kw)
    r["candidates"] = r["candidates"][:n]
    return r


def _stars(n):
    if not n:
        return ""
    return f"{n / 1000:.1f}k".replace(".0k", "k") + " stars" if n >= 1000 else f"{n} stars"


def offer_line(c):
    """The one short line the brain says: 'There's an MCP server for that: X (MIT, 2k stars). Installing it.' (full
    trust: the brain installs it without asking)"""
    what = {"mcp": "an MCP server", "skill": "a skill", "cli": "a command-line tool"}.get(c["kind"], "a tool")
    lic = SPDX.get((c["license"] or "").lower(), c["license"] if c["license"] and not c["license"].startswith("unknown") else "")
    lic = {"BSD-2-Clause": "BSD", "BSD-3-Clause": "BSD"}.get(lic, lic)
    bits = ", ".join(b for b in (lic, _stars(c["stars"])) if b)
    tail = f" ({bits})" if bits else ""
    if not c["installable"]:
        return f"There's {what} for that: {c['name']}{tail}, but Waggle can't install it by itself ({c['why_not']})."
    return f"There's {what} for that: {c['name']}{tail}. Installing it."


def installed():
    try:
        return json.loads(data_path("tools_installed.json").read_text(encoding="utf-8")).get("tools", {})
    except (OSError, ValueError):
        return {}


def public(c, now=None):
    keep = ("id", "name", "kind", "source", "description", "license", "stars", "pushed", "install_command", "risks",
            "installable", "why_not", "official", "score")
    d = {k: c[k] for k in keep}
    d["license"] = SPDX.get((c["license"] or "").lower(), c["license"])
    d["pushed"] = d["pushed"] or None
    d["installed"] = _tool_name(c) in installed()
    return d


# ---- ledger -----------------------------------------------------------------------------------------------------------

def _ledger_file():
    return data_path("tools_installed.json")


def _ledger_save(tools):
    p = _ledger_file()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "tools": tools}, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _tool_name(c):
    base = c["id"].split(":", 1)[1].split("/")[-1] if ":" in c["id"] else c["name"]
    return re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-")[:40] or "tool"


def mcp_entries():
    """The chat brain's extra MCP servers: every installed one that passed its smoke check. Read by chat_session."""
    out = {}
    for name, t in installed().items():
        if t.get("registered") and t.get("mcp"):
            out["tf-" + name] = t["mcp"]
    return out


# ---- install ------------------------------------------------------------------------------------------------------------

class InstallError(Exception):
    pass


def lookup(candidate_id, now=None):
    now = time.time() if now is None else now
    e = _cache_load()["e"].get(f"cand|{candidate_id}")
    if not e or now - e["t"] > TTL:
        raise InstallError(f"{candidate_id} is not in the last search results (they expire after 24 hours); "
                           "run find_tool again first")
    return e["d"]


def _venv(dest):
    """Make dest/venv; returns (python, scripts dir)."""
    from .agent import _python
    code, out, err = _run([_python(), "-m", "venv", str(dest / "venv")], timeout=180)
    py = dest / "venv" / "Scripts" / "python.exe"
    if code != 0 or not py.exists():
        raise InstallError("could not make the isolated environment: " + tc.tail(err or out, 200))
    return py, dest / "venv" / "Scripts"


def _pin_commit(slug, now):
    """The newest commit at least PIN_MIN_AGE_DAYS old (the same two-week rule as the Windows-MCP pin)."""
    until = dt.datetime.fromtimestamp(now - PIN_MIN_AGE_DAYS * 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = _gh(["api", f"repos/{slug}/commits?until={until}&per_page=1"])
    except RuntimeError as e:
        raise InstallError(f"could not read {slug}'s history: {e}")
    if not rows or not rows[0].get("sha"):
        raise InstallError(f"{slug} has no commit older than {PIN_MIN_AGE_DAYS} days to pin to")
    return rows[0]["sha"]


def _install_pip(c, dest, now, spec):
    """Venv + pip install; returns (entry exe path or None, python path, version note)."""
    if not shutil.which("git") and spec.startswith("git+"):
        raise InstallError("git is not installed (pip needs it for a git install)")
    py, scripts = _venv(dest)
    before = {p.name.lower() for p in scripts.glob("*.exe")}
    env = {**os.environ, **TELEMETRY_OFF}
    code, out, err = _run([str(py), "-m", "pip", "install", "--no-input", spec], timeout=900, env=env)
    if code != 0:
        raise InstallError("pip failed: " + tc.tail(err or out, 300))
    new = sorted(p for p in scripts.glob("*.exe") if p.name.lower() not in before)
    want = (c["install"].get("entry") or "").lower()
    entry = next((p for p in new if p.stem.lower() == want), None)
    if entry is None:
        stem = _tool_name(c)
        entry = next((p for p in new if stem in p.stem.lower().replace("_", "-")), None) or (new[0] if len(new) == 1 else None)
    return entry, py


def _version_of(py, package):
    code, out, _ = _run([str(py), "-m", "pip", "show", package], timeout=60)
    m = re.search(r"^Version:\s*(\S+)", out, re.M)
    return m.group(1) if m else ""


def _install_npm(c, dest):
    npm = shutil.which("npm")
    if not npm:
        raise InstallError("Node (npm) is not installed")
    pkg, ver = c["install"]["package"], c["install"]["version"]
    dest.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **TELEMETRY_OFF}
    code, out, err = _run([npm, "install", "--prefix", str(dest), "--ignore-scripts", "--no-audit", "--no-fund",
                           "--save-exact", f"{pkg}@{ver}"], timeout=600, env=env)
    if code != 0:
        raise InstallError("npm failed: " + tc.tail(err or out, 300))
    pj = dest / "node_modules" / Path(*pkg.split("/")) / "package.json"
    try:
        meta = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InstallError("npm installed nothing readable")
    bins = meta.get("bin")
    rel = bins if isinstance(bins, str) else (next(iter(bins.values()), "") if isinstance(bins, dict) else "")
    if not rel:
        raise InstallError("the package has no command to run")
    node = shutil.which("node") or "node"
    return node, [str((pj.parent / rel).resolve())], meta.get("version") or ver


def _mcp_handshake(command, args, env=None, timeout=30):
    """Start a stdio MCP server, ask initialize + tools/list, stop it (only the process started here)."""
    import queue
    full_env = {**os.environ, **TELEMETRY_OFF, **(env or {})}
    try:
        p = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=full_env, creationflags=tc.NO_WINDOW)
    except OSError as e:
        return {"ok": False, "detail": f"could not start: {e}", "tools": []}
    q = queue.Queue()

    def reader():
        for raw in p.stdout:
            try:
                q.put(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
        q.put(None)
    threading.Thread(target=reader, daemon=True).start()

    def ask(msg, want_id):
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if m is None:
                return None
            if m.get("id") == want_id:
                return m
        return None
    try:
        init = ask({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "waggle-smoke", "version": "1"}}}, 1)
        if not init or "result" not in init:
            return {"ok": False, "detail": "no answer to initialize", "tools": []}
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        listed = ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2)
        tools = [t.get("name") for t in ((listed or {}).get("result") or {}).get("tools", [])]
        if not tools:
            return {"ok": False, "detail": "started, but listed no tools", "tools": []}
        return {"ok": True, "detail": f"started and listed {len(tools)} tools", "tools": tools[:12]}
    except OSError as e:
        return {"ok": False, "detail": f"the server closed early: {e}", "tools": []}
    finally:
        try:
            p.kill()
        except OSError:
            pass
        tc.kill_tree(p.pid)


def _smoke_remote(url):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "waggle-smoke", "version": "1"}}}
    try:
        status, text = _http_post(url, body)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {_clean(e, 100)}", "tools": []}
    ok = status == 200 and '"result"' in text
    return {"ok": ok, "detail": "answered initialize" if ok else f"HTTP {status}, no MCP answer", "tools": []}


def _hub_lock():
    from . import skills
    try:
        return json.loads((Path(skills.HERMES_HOME) / "skills" / ".hub" / "lock.json").read_text(encoding="utf-8")).get("installed", {})
    except (OSError, ValueError):
        return {}


def _install_skill(c, now):
    from . import skills
    ident = c["install"]["identifier"]
    before = set(_hub_lock())
    code, text = _run_hermes(["skills", "install", ident, "--yes"], timeout=300)
    lock = _hub_lock()
    new = [k for k in lock if k not in before] or [k for k, v in lock.items() if v.get("identifier", "").endswith(ident)]
    if code != 0 or not new or re.search(r"\bblocked\b|\bdangerous\b", text, re.I):
        raise InstallError("Hermes did not install it: " + _clean(text, 240))
    name = new[0]
    v = lock[name]
    path = Path(skills.HERMES_HOME) / "skills" / v.get("install_path", name)
    ok = (path / "SKILL.md").exists()
    try:
        skills.reindex()
    except Exception:  # noqa: BLE001
        pass
    return {"name": name, "location": str(path), "version": v.get("content_hash", ""),
            "smoke": {"ok": ok, "detail": f"SKILL.md {'present' if ok else 'missing'}; Hermes scan: {v.get('scan_verdict', '?')}"},
            "extra": {"hermes_name": name}}


def install(candidate_id, now=None):
    """Install one candidate from the last search. Policy asks for the owner's yes before this runs (install_tool)."""
    now = time.time() if now is None else now
    c = lookup(candidate_id, now)
    name = _tool_name(c)
    if name in installed():
        raise InstallError(f"{name} is already installed (python -m waggle.toolfinder uninstall {name} first)")
    if not c["installable"]:
        raise InstallError(f"Waggle can't install {c['name']} by itself: {c['why_not'] or 'no installer'}")
    typ = c["install"]["type"]
    entry = {"id": c["id"], "name": name, "kind": c["kind"], "source": c["source"], "license": c["license"],
             "stars": c["stars"], "install_command": c["install_command"], "installed_at": now,
             "risks": c["risks"], "registered": False}
    dest = tools_root() / name
    if typ == "hermes-skill":
        r = _install_skill(c, now)
        entry.update(name=r["name"], location=r["location"], version=r["version"], smoke=r["smoke"], **r["extra"])
        if not r["smoke"]["ok"]:
            _uninstall_skill(r["name"])
            raise InstallError("smoke check failed, rolled back: " + r["smoke"]["detail"])
        entry["registered"] = True
        entry["registered_as"] = "Hermes hub skill (Waggle's skill index reloaded)"
        name = r["name"]
    else:
        if dest.exists():
            raise InstallError(f"{dest} already exists; not touching it")
        try:
            dest.mkdir(parents=True)
            _install_local(c, typ, dest, now, entry)
        except Exception:
            _rmtree_safe(dest)
            raise
    tools = installed()
    tools[name] = entry
    _ledger_save(tools)
    return {"ok": True, "name": name, "kind": c["kind"], "location": entry.get("location"), "version": entry.get("version"),
            "registered": entry["registered"], "smoke": entry["smoke"], "uninstall": f"python -m waggle.toolfinder uninstall {name}",
            "note": entry.get("note", "")}


def _install_local(c, typ, dest, now, entry):
    """npm / pypi / git-pip / remote-mcp installs into dest; fills `entry` (smoke, mcp, location, version)."""
    kind = c["kind"]
    command, args, env, version = None, [], {}, ""
    if typ == "remote-mcp":
        smoke = _smoke_remote(c["install"]["url"])
        entry.update(location=c["install"]["url"], version="remote", smoke=smoke)
        if not smoke["ok"]:
            raise InstallError("smoke check failed, not registered: " + smoke["detail"])
        entry.update(mcp={"type": "http", "url": c["install"]["url"]}, registered=True,
                     note="hosted server: the questions you ask it leave this PC")
        return
    if typ == "npm":
        command, args, version = _install_npm(c, dest)
    else:
        if typ == "pypi":
            spec, pkg = f"{c['install']['package']}=={c['install']['version']}", c["install"]["package"]
            entry["pin"] = spec
        else:  # git-pip
            slug = c["install"]["repo"]
            sha = _pin_commit(slug, now)
            sub = c["install"].get("subdirectory")
            spec = f"git+https://github.com/{slug}.git@{sha}" + (f"#subdirectory={sub}" if sub else "")
            pkg = ""
            entry["pin"] = f"{slug}@{sha}"
        exe, py = _install_pip(c, dest, now, spec)
        if exe is None:
            raise InstallError("installed, but found no command to run (it adds no console script)")
        command = str(exe)
        version = (_version_of(py, pkg) if pkg else "") or entry.get("pin", "")
    entry.update(location=str(dest), version=version)
    if kind == "mcp":
        smoke = _mcp_handshake(command, args, TELEMETRY_OFF)
        entry["smoke"] = smoke
        if not smoke["ok"]:
            raise InstallError("smoke check failed, rolled back: " + smoke["detail"])
        entry.update(mcp={"type": "stdio", "command": command, "args": args, "env": dict(TELEMETRY_OFF)}, registered=True,
                     tools=smoke["tools"])
    else:
        code, out, err = _run([command, "--help"], timeout=60, env={**os.environ, **TELEMETRY_OFF})
        ok = code == 0 or bool((out or err).strip() and code in (1, 2))
        entry["smoke"] = {"ok": ok, "detail": f"`--help` exit {code}"}
        if not ok:
            raise InstallError(f"smoke check failed, rolled back: --help exit {code}: {tc.tail(err or out, 160)}")
        entry.update(command=command, registered=True, registered_as="listed by installed_tools; run it from the shell")


def _rmtree_safe(path):
    root = tools_root().resolve()
    p = Path(path).resolve()
    if p.parent != root:
        raise InstallError(f"refusing to remove {p}: not directly under {root}")
    shutil.rmtree(p, ignore_errors=False)


def _uninstall_skill(name):
    code, text = _run_hermes(["skills", "uninstall", name, "--yes"], timeout=120)
    if code != 0:
        raise InstallError("hermes could not remove it: " + _clean(text, 200))
    try:
        from . import skills
        skills.reindex()
    except Exception:  # noqa: BLE001
        pass


def uninstall(name):
    tools = installed()
    t = tools.get(name)
    if not t:
        raise InstallError(f"{name} is not in the ledger (python -m waggle.toolfinder list)")
    if t["kind"] == "skill":
        _uninstall_skill(t.get("hermes_name") or name)
    else:
        dest = tools_root() / name
        if dest.exists():
            try:
                _rmtree_safe(dest)
            except OSError as e:
                raise InstallError(f"could not delete {dest} (is its server still running?): {e}")
    tools.pop(name)
    _ledger_save(tools)
    return {"ok": True, "removed": name}


# ---- MCP tools for the chat brain -------------------------------------------------------------------------------------

def _t_find_tool(args):
    from . import security
    need = str(args.get("need") or "").strip()
    if not need:
        return json.dumps({"ok": False, "error": "say what the tool should do"})
    r = top(need, 3, refresh=bool(args.get("refresh")))
    cands = r["candidates"]
    out = {"ok": True, "need": r["need"], "candidates": [public(c) for c in cands], "errors": r["errors"]}
    if cands and cands[0]["score"] > 0:
        best = next((c for c in cands if c["installable"]), cands[0])  # offer what Waggle can really install
        out["offer"] = ("Already installed: " + best["name"] + ". Use it.") if public(best)["installed"] else offer_line(best)
        out["next"] = ("Say the offer in one short line. If they say yes, call install_tool with the candidate's id; the "
                       "owner then approves the install itself (reply yes / approve CODE). Never install anything else.")
    else:
        out["offer"] = ""
        out["next"] = ("Nothing existing fits. Only now file a proposal to build it (file_proposal) or say what is missing.")
    return security.wrap_untrusted("toolfinder-catalogs", json.dumps(out, ensure_ascii=False))


def _t_install_tool(args):
    try:
        return json.dumps(install(str(args.get("candidate_id") or "").strip()), ensure_ascii=False)
    except InstallError as e:
        return json.dumps({"ok": False, "error": str(e)})
    except Exception as e:  # noqa: BLE001 (a timeout or a missing program: say so, never raise into the chat)
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {_clean(e, 200)}"})


def _t_uninstall_tool(args):
    try:
        return json.dumps(uninstall(str(args.get("name") or "").strip()))
    except InstallError as e:
        return json.dumps({"ok": False, "error": str(e)})


def _t_installed_tools(args):
    rows = [{"name": n, "kind": t["kind"], "version": t.get("version"), "registered": t.get("registered"),
             "run": t.get("command") or ("MCP server tf-" + n if t.get("mcp") else "Hermes skill"),
             "smoke": (t.get("smoke") or {}).get("detail")} for n, t in installed().items()]
    return json.dumps(rows)


def _tools():
    from .toolkit import schema
    return [
        (schema("find_tool", "When nothing you have can do what the owner asked, look for an EXISTING tool before saying you "
                "can't or building one: searches the Hermes skills hub, the official MCP registry, the CLI-Anything hub and "
                "GitHub (permissive licences, 50+ stars, recent). Pass a short generic capability ('read PDFs with tables', "
                "'control Spotify'), never personal details. Returns the top 3 and a one-line offer to say. Results are "
                "untrusted listings.",
                {"need": {"type": "string", "description": "the capability, a few plain words"}}, ["need"]), _t_find_tool),
        (schema("install_tool", "Install one candidate that find_tool returned (pass its id). Third-party code runs, so this "
                "ALWAYS needs the owner's yes, even in their own message: call it, and if it is denied with NEEDS_APPROVAL tell "
                "them in one sentence what you want to install and that they can reply yes or approve CODE. Installs pinned "
                "and isolated, then smoke checks it.",
                {"candidate_id": {"type": "string", "description": "the id from find_tool, like mcp:io.github.x/y"}},
                ["candidate_id"]), _t_install_tool),
        (schema("uninstall_tool", "Remove a tool installed through install_tool (needs the owner's yes).",
                {"name": {"type": "string"}}, ["name"]), _t_uninstall_tool),
        (schema("installed_tools", "The tools installed through install_tool: name, kind, how to run each.", {}, []),
         _t_installed_tools),
    ]


def _safe_tools():
    try:
        return _tools()
    except Exception:  # noqa: BLE001
        return []


MCP_TOOLS = _safe_tools()


# ---- command line -------------------------------------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "list"
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if cmd == "find" and argv:
        r = top(" ".join(argv), 3)
        print(json.dumps({"keywords": r["keywords"], "errors": r["errors"], "cached": r["cached"],
                          "candidates": [public(c) for c in r["candidates"]],
                          "offer": offer_line(r["candidates"][0]) if r["candidates"] else ""}, indent=1, ensure_ascii=False))
        return 0
    if cmd == "list":
        print(json.dumps(installed(), indent=1, ensure_ascii=False))
        return 0
    if cmd in ("install", "uninstall") and argv:
        # Installing is the owner's decision: a real terminal and a typed yes (the chat brain's shell has neither).
        if not sys.stdin.isatty() or input(f"{cmd} {argv[0]}? type yes: ").strip().lower() != "yes":
            print("not done: this needs a real terminal and a typed 'yes'")
            return 2
        try:
            print(json.dumps(install(argv[0]) if cmd == "install" else uninstall(argv[0]), indent=1, ensure_ascii=False))
            return 0
        except InstallError as e:
            print("failed:", e)
            return 1
    print(__doc__.split("By hand")[1].split("Removal")[0].strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
