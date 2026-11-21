"""Static guard: every call under waggle/ that starts a process passes the no-window handling, so new code cannot
bring the flicker back.

On Windows a console program started by a process without a console (the core runs under pythonw) gets its own
visible window, and with Windows Terminal as the default terminal that is a terminal window that takes the
focus. Measured with tools/window_probe.py: one `git` call without CREATE_NO_WINDOW opened one window and took
the foreground; a pass of outreach.look made about eighty.

A call is accepted when it passes `creationflags=` built from CREATE_NO_WINDOW (directly, or through a variable
assigned in the same function), or DETACHED_PROCESS (no console at all), or CREATE_NEW_CONSOLE (a window the
user asked for); or when its lines carry the comment `# window: ok <why>`. os.system, os.popen, os.startfile,
os.spawn*, _winapi.CreateProcess and the asyncio subprocess helpers are refused unless marked."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "waggle"
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
OS_CALLS = {"system", "popen", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
            "spawnvp", "spawnvpe"}
ASYNC_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}
OK_TOKENS = ("NO_WINDOW", "DETACHED_PROCESS", "NEW_CONSOLE")
MARKER = "# window: ok"


def _attrs(node):
    """The `module.attr` pairs a call's function expression refers to (the function may be `popen or subprocess.Popen`)."""
    found, todo = set(), [node]
    while todo:
        sub = todo.pop()
        if isinstance(sub, ast.Call):  # a call inside the function expression (`run(...).stdout.strip`) is its own call
            continue
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            found.add((sub.value.id, sub.attr))
        todo.extend(ast.iter_child_nodes(sub))
    return found


def _kind(call):
    pairs = _attrs(call.func)
    for mod, attr in pairs:
        if mod == "subprocess" and attr in SUBPROCESS_CALLS:
            return "subprocess"
        if mod == "os" and attr in OS_CALLS:
            return "os"
        if mod == "asyncio" and attr in ASYNC_CALLS:
            return "asyncio"
        if mod == "_winapi" and attr == "CreateProcess":
            return "winapi"
    return None


def _functions(tree):
    """Maps every node to its innermost enclosing function (or the module)."""
    owner = {}

    def visit(node, current):
        for child in ast.iter_child_nodes(node):
            nxt = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else current
            owner[child] = nxt
            visit(child, nxt)
    owner[tree] = tree
    visit(tree, tree)
    return owner


def _assigned_text(scope, name):
    out = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            out.append(ast.unparse(node.value))
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and isinstance(node.target, ast.Name) \
                and node.target.id == name and node.value is not None:
            out.append(ast.unparse(node.value))
    return " ".join(out)


def _flagged(call, scope):
    for kw in call.keywords:
        if kw.arg == "creationflags":
            text = ast.unparse(kw.value)
            if any(t in text for t in OK_TOKENS):
                return True
            names = {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
            return any(any(t in _assigned_text(scope, n) for t in OK_TOKENS) for n in names)
    return False


def offending_calls(root=ROOT):
    bad = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        owner = _functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _kind(node)
            if kind is None:
                continue
            span = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
            if MARKER in span or MARKER in lines[node.lineno - 2 if node.lineno > 1 else 0]:
                continue
            if kind == "subprocess" and _flagged(node, owner.get(node, tree)):
                continue
            bad.append(f"{path.relative_to(root.parent)}:{node.lineno}: {ast.unparse(node.func)}(...)")
    return bad


def test_every_process_start_under_waggle_has_the_no_window_handling():
    bad = offending_calls()
    assert not bad, ("these calls start a process without creationflags=...CREATE_NO_WINDOW (a console program "
                     "would open a visible window under pythonw); add the flag, or `# window: ok <why>`:\n  "
                     + "\n  ".join(bad))


def test_the_check_itself_catches_the_unflagged_forms(tmp_path):
    pkg = tmp_path / "waggle"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        "import os, subprocess\n"
        "def a():\n    subprocess.run(['git', 'status'])\n"
        "def b(popen=None):\n    (popen or subprocess.Popen)(['x'], creationflags=0)\n"
        "def c():\n    os.system('dir')\n"
        "def d():\n    subprocess.check_output(['x'], creationflags=0x10)\n", encoding="utf-8")
    (pkg / "good.py").write_text(
        "import os, subprocess\n"
        "def a():\n    subprocess.run(['git'], creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "def b():\n    flags = 0\n    if os.name == 'nt':\n        flags = subprocess.CREATE_NO_WINDOW\n"
        "    subprocess.Popen(['x'], creationflags=flags)\n"
        "def c():\n    subprocess.run(['x'], creationflags=0)  # window: ok test double\n"
        "def d():\n    subprocess.Popen(['x'], creationflags=subprocess.DETACHED_PROCESS)\n", encoding="utf-8")
    found = offending_calls(pkg)
    assert [f.split(":")[1] for f in found] == ["3", "5", "7", "9"]
    assert not any("bad.py" not in f for f in found)
    assert not any("good.py" in f for f in found)
