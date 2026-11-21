"""Claude Code PreToolUse hook: every tool call a Waggle agent makes is checked by waggle.policy.

Claude Code runs:  python <repo>/waggle/agent_hook.py   with the hook event as JSON on stdin.
We print a permission decision (allow / deny) as JSON. "ask" is turned into deny + an approval id,
because nobody is at the keyboard during a headless run; Waggle relays the request to the user.
"""
import os
import sys

# Run as a script, Python puts this file's folder (waggle/) first on sys.path, so a json.py or hashlib.py an agent
# drops next to it would be imported instead of the standard library. Drop that entry before importing anything.
_HERE = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
sys.path[:] = [p for p in sys.path if p and os.path.normcase(os.path.abspath(p)) != _HERE]
sys.dont_write_bytecode = True

import hashlib  # noqa: E402
import importlib  # noqa: E402
import importlib.abc  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

# Integrity gate. Deliberately self-contained and importing nothing from waggle until the hashes match: it must not
# trust the files it is checking. Keep _CHECKED equal to guard.HOOK_CHECKED and the hashing equal to guard.sha
# (tests/test_guard.py asserts both).
_ROOT = Path(__file__).resolve().parent.parent
_CHECKED = ("waggle/__init__.py", "waggle/policy.py", "waggle/agent_hook.py", "waggle/guard.py",
            "waggle/security.py", "waggle/config.py", "waggle/budget.py", "waggle/quota.py", "waggle/agent.py",
            "waggle/claude_cli.py", "waggle/bridge.py", "hermes_plugin/waggle/__init__.py")
_TRUSTED_FOR_AUDIT = {"waggle/agent_hook.py", "waggle/security.py"}
_MANIFEST_VERSION = 3
_verified = {}  # rel -> the exact bytes that were hashed; the script-mode importer runs these, never a re-read


def _profile_dir():
    """The user's profile folder from the OS account (GetUserProfileDirectoryW on this process's token reads the
    registry, not USERPROFILE / HOME, which an agent can set for a process it starts). Same as guard.profile_dir."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            fn = ctypes.WinDLL("userenv").GetUserProfileDirectoryW
            fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            fn.restype = wintypes.BOOL
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if fn(wintypes.HANDLE(-4), buf, ctypes.byref(size)) and buf.value:
                return Path(buf.value)
            return None
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return None


# A test that runs this file in a subprocess passes _TEST_USER_HOME through runpy's init_globals; nothing an agent
# controls (environment, files) can set it.
_USER_HOME = globals().get("_TEST_USER_HOME") or _profile_dir()
_GUARD_DIR = Path(_USER_HOME) / ".waggle-guard" if _USER_HOME else None


def _sha(data):
    return hashlib.sha256(data if b"\0" in data else data.replace(b"\r\n", b"\n")).hexdigest()


def _resolved_root():
    return os.path.normcase(os.path.realpath(str(_ROOT)))


def _manifest_dir():
    """This checkout's own manifest folder (guard.checkout_dir): a hash of the resolved path the hook runs from, so
    no other checkout's approval, copy or junction can speak for this one."""
    return _GUARD_DIR / hashlib.sha256(_resolved_root().encode("utf-8")).hexdigest()[:32]


def _same(a, b):
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
    except (OSError, ValueError):
        return False


class _CheckoutFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Script mode only. `waggle` and `waggle.*` come from this checkout and nowhere else (not a pip install -e copy,
    not a .pth path), and the checked files run from the bytes that were hashed (no re-read, no .pyc)."""

    loaded = []

    def find_spec(self, name, path=None, target=None):
        if name != "waggle" and not name.startswith("waggle."):
            return None
        parts = name.split(".")
        base = _ROOT.joinpath(*parts)
        if (base / "__init__.py").is_file():
            return importlib.util.spec_from_file_location(name, base / "__init__.py", loader=self,
                                                          submodule_search_locations=[str(base)])
        if base.with_suffix(".py").is_file():
            return importlib.util.spec_from_file_location(name, base.with_suffix(".py"), loader=self)
        raise ImportError(f"{name} is not in the checkout this hook runs from")

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = Path(module.__spec__.origin)
        rel = path.relative_to(_ROOT).as_posix()
        source = _verified.get(rel)
        if source is None:
            source = path.read_bytes()
        self.loaded.append(rel)
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)


def _install_finder():
    stray = sorted(n for n in sys.modules if n == "waggle" or n.startswith("waggle."))
    if stray:
        raise ImportError(f"{stray[0]} was imported before the hook ran (startup code)")
    if not any(isinstance(f, _CheckoutFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _CheckoutFinder())


def _shadows():
    """Same as guard.shadows: <root>/json.py and the like, which the guard command and anything started with
    `python -m` from the checkout would import instead of the standard library."""
    import importlib.machinery
    names = {m.lower() for m in sys.stdlib_module_names}
    suffixes = (".py", ".pyc", *importlib.machinery.EXTENSION_SUFFIXES)
    out = []
    for e in os.scandir(_ROOT):
        if e.is_dir():
            if e.name.lower() in names and (Path(e.path) / "__init__.py").is_file():
                out.append(e.name)
        elif any(e.name.lower().endswith(x) and e.name[:-len(x)].lower() in names for x in suffixes):
            out.append(e.name)
    return sorted(out)


def integrity_failure(script=False):
    """None when the checked files match the manifest, else [{file, expected, actual}]. Any error is a failure."""
    try:
        if _GUARD_DIR is None:
            return [{"file": "user profile", "expected": "the account's profile folder", "actual": "not found"}]
        try:
            manifest = json.loads((_manifest_dir() / "manifest.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [{"file": "manifest.json", "expected": "an approved manifest", "actual": "missing"}]
        expected = manifest["files"]
        if not isinstance(expected, dict):
            raise ValueError("files is not a table")
        bad = []
        if manifest.get("version") != _MANIFEST_VERSION:
            bad.append({"file": "manifest.json", "expected": f"version {_MANIFEST_VERSION}",
                        "actual": f"version {manifest.get('version')}"})
        elif os.path.normcase(str(manifest.get("root"))) != _resolved_root():
            bad.append({"file": "manifest.json", "expected": f"approved for {_resolved_root()}",
                        "actual": f"approved for {manifest.get('root')}"})
        for rel in _CHECKED:
            try:
                data = (_ROOT / rel).read_bytes()
                actual = _sha(data)
            except OSError:
                data, actual = None, None
            if actual is None or expected.get(rel) != actual:
                bad.append({"file": rel, "expected": expected.get(rel), "actual": actual})
            else:
                _verified[rel] = data
        bad += [{"file": name, "expected": "no file shadowing a Python module at the checkout root",
                 "actual": "present"} for name in _shadows()]
        if not _same(Path.home(), _USER_HOME):  # Path.home() picks the default data folder and the write roots
            bad.append({"file": "USERPROFILE", "expected": str(_USER_HOME), "actual": str(Path.home())})
        if not bad:
            try:
                if script:
                    _install_finder()
                guard = importlib.import_module("waggle.guard")
            except Exception as e:
                return [{"file": "waggle/guard.py", "expected": "importable",
                         "actual": f"import failed ({type(e).__name__})"}]
            home = manifest.get("waggle_home")
            if not home or not _same(guard.config.HOME, home):  # approvals.db lives in the data folder
                bad.append({"file": "WAGGLE_HOME", "expected": str(home), "actual": str(guard.config.HOME)})
            elif script:
                guard.gate_passed()
        return bad or None
    except Exception as e:
        return [{"file": "manifest.json", "expected": "a readable manifest",
                 "actual": f"check failed ({type(e).__name__})"}]


def _record_violation(bad, tool, args):
    """violation.json for the rest of Waggle to read, plus an integrity_violation row in the audit log."""
    try:
        folder = _manifest_dir()
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"violation.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"time": time.time(), "files": bad}, indent=1), encoding="utf-8")
        os.replace(tmp, folder / "violation.json")
    except Exception:
        pass
    reason = "integrity_violation: " + ", ".join(b["file"] for b in bad)
    try:
        if {b["file"] for b in bad} <= _TRUSTED_FOR_AUDIT and "waggle" in sys.modules:
            from waggle import policy
            policy.audit(tool, args, policy.Decision("integrity_violation", reason), os.environ.get("WAGGLE_AGENT", "agent"))
        else:  # policy.py itself can't be trusted: same row format, written without importing anything
            home = Path(os.environ.get("WAGGLE_HOME") or Path.home() / ".waggle")
            home.mkdir(parents=True, exist_ok=True)
            row = {"time": time.time(), "source": os.environ.get("WAGGLE_AGENT", "agent"), "tool": tool,
                   "decision": "integrity_violation", "reason": reason, "call": f"{tool}"}
            with open(home / "audit.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
    except Exception:
        pass


# What each checked item is, in words the user knows. Self-contained (the hook must not trust guard.py); guard.py
# uses this same text for the in-process gate and Hermes's tools.
_PLAIN = {
    "waggle/__init__.py": "Waggle's package start-up file", "waggle/policy.py": "Waggle's permission rules",
    "waggle/agent_hook.py": "the safety hook every tool call goes through", "waggle/guard.py": "the tamper guard",
    "waggle/security.py": "the safety checks for Hermes tools", "waggle/config.py": "the settings loader",
    "waggle/budget.py": "the background budget code", "waggle/quota.py": "the usage-limit code",
    "waggle/agent.py": "the code that starts tool agents", "waggle/claude_cli.py": "the code that starts Claude runs",
    "waggle/bridge.py": "the Hermes tool bridge", "hermes_plugin/waggle/__init__.py": "the Hermes plug-in",
    "tests/test_trust.py": "the budget and approval tests", "tests/test_guard.py": "the guard's tests",
    "user:.hermes/plugins/waggle/__init__.py": "the installed copy of the Hermes plug-in",
}


def plain(b):
    """One failed item ({file, expected, actual} from the hook and the gate, or {path, kind, ...} from verify) in
    plain words."""
    f = str(b.get("file") or b.get("path") or "")
    exp, act, kind = str(b.get("expected") or ""), b.get("actual"), b.get("kind")
    if f == "manifest.json" or kind in ("no_manifest", "old_manifest", "other_checkout"):
        if act == "missing" or kind == "no_manifest":
            return "no safety approval (manifest.json) exists yet for this Waggle folder"
        if exp.startswith("version") or kind == "old_manifest":
            return "the safety approval (manifest.json) was made by an older Waggle version"
        if exp.startswith("approved for") or kind == "other_checkout":
            return "the safety approval (manifest.json) belongs to a different Waggle folder"
        return "the safety approval file (manifest.json) could not be read"
    if f == "USERPROFILE":
        return f"the user-profile folder (USERPROFILE) is {act} instead of {exp}"
    if f == "WAGGLE_HOME":
        return f"the Waggle data folder (WAGGLE_HOME) is {act}, but {exp} was approved"
    if f == "user profile":
        return "the user's profile folder could not be found"
    if exp == "importable":
        return "the tamper guard (waggle/guard.py) does not load"
    if exp.startswith("no file shadowing") or kind == "shadow":
        return f"a file named {f} in the Waggle folder would replace one of Python's own modules"
    if f == "home:config.json#keys":
        keys = ", ".join(b.get("keys") or []) or "budget, limits or trusted folders"
        return f"protected settings in config.json changed ({keys})"
    if f.startswith("home:eval/"):
        return f"a held-out evaluation file changed ({f[5:]})"
    what = _PLAIN.get(f)
    state = "is missing" if act is None and kind != "added" else "was changed"
    return f"{what} ({f}) {state}" if what else f"{f} {state}"


def denial_text(bad, root=None):
    """The one message an agent gets when the safety check fails: what changed, in plain words, and the exact
    command the user runs. The agent passes it on; it does not retry."""
    items = "; ".join(dict.fromkeys(plain(b) for b in bad)) or "the safety check could not run"
    cmd = f'cd "{root or _ROOT}"; python -m waggle.guard approve'
    env = any(str(b.get("file") or b.get("path")) in ("USERPROFILE", "WAGGLE_HOME") for b in bad)
    return (f"Waggle's safety check failed, so every action is blocked until the user approves. What changed: "
            f"{items}. Tell the user this once, in plain words, and that they must check the change and run this in "
            f"a terminal: `{cmd}` (it lists the change and asks y/N)."
            + (" If they did not move that folder on purpose, start Waggle normally instead of approving."
               if env else "") + " Do not retry or work around this.")


def lockdown(bad, tool="", args=None):
    _record_violation(bad, tool, args or {})
    why = denial_text(bad)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def handle(event):
    from waggle import policy
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    session = os.environ.get("WAGGLE_SESSION", "")
    d = policy.check(tool, args, session=session, source=os.environ.get("WAGGLE_AGENT", "agent"),
                     cwd=event.get("cwd"))
    if d.allowed:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "permissionDecisionReason": d.reason or "allowed by Waggle policy"}}
    if d.action == policy.DENY and d.reason.startswith("integrity_violation: "):
        why = d.reason[len("integrity_violation: "):]  # already says what changed and what the user runs
    elif d.action == policy.DENY:
        why = f"Blocked by Waggle's safety policy: {d.reason}. Do not retry or work around this; tell the user."
    elif getattr(d, "kind", "") == "hearing":  # the owner's words were not heard clearly: a question, not a permission
        code = (policy.get(d.approval_id) or {}).get("code", "")
        why = (f"NEEDS_APPROVAL [{code}]: {d.reason}. Speech recognition was unsure of the owner's words. Do not ask "
               f"for permission: ask them exactly the question above about what you heard, in one sentence. If they "
               f"answer yes, do the step. Never show internal ids or hook details.")
    else:
        code = (policy.get(d.approval_id) or {}).get("code", "")
        why = (f"NEEDS_APPROVAL [{code}]: {d.reason}. You cannot do this without the user's OK. Stop trying "
               f"alternatives for this step; finish what you can and tell the user in one plain sentence what you "
               f"want to do, so they can reply 'yes' (or 'approve {code}'). Never show internal ids or hook details.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}


def main(script=False):
    try:
        # Claude Code sends UTF-8. Decode it ourselves: -I ignores PYTHONUTF8 / PYTHONIOENCODING, and the console
        # code page would turn a non-ASCII command into an error (a deny) or into different text.
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        event = json.loads((raw.decode("utf-8") if isinstance(raw, bytes) else raw) or "{}")
        bad = integrity_failure(script=script)
        out = lockdown(bad, event.get("tool_name", ""), event.get("tool_input")) if bad else handle(event)
    except Exception as e:  # fail closed
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": f"Waggle policy check failed ({e}); not running it."}}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main(script=True)
