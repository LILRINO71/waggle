"""Model catalogue (OpenJarvis's intelligence primitive): what models exist on this PC and in the cloud, what
each can do, whether it fits the actual hardware, how fast it has really been, and which to use for a task.

Nothing is downloaded. Hardware is detected (nvidia-smi + Windows APIs), local models come from Ollama's own
metadata (/api/tags + /api/show: context length, vision, tool calling, thinking, embedding), cloud models are
the Claude models reachable through the user's plan, and latency is measured from Waggle's traces.
"""
import ctypes
import json
import os
import platform
import shutil
import statistics
import subprocess
import time

from . import engines
from .config import Settings, data_path

CLAUDE = {
    "haiku": {"context": 200_000, "vision": True, "tools": True, "thinking": True, "tier": "fast", "weight": 1 / 3},
    "sonnet": {"context": 200_000, "vision": True, "tools": True, "thinking": True, "tier": "strong", "weight": 1.0},
    "opus": {"context": 200_000, "vision": True, "tools": True, "thinking": True, "tier": "strongest", "weight": 5 / 3},
}


def hardware(refresh=False):
    cache = data_path("hardware.json")
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    hw = {"os": f"{platform.system()} {platform.release()}", "cpu": platform.processor() or platform.machine(),
          "cores": os.cpu_count(), "ram_gb": _ram_gb(), "gpus": _gpus(),
          "disk_free_gb": round(shutil.disk_usage(os.path.expanduser("~")).free / 1e9, 1)}
    hw["vram_gb"] = max([g["vram_gb"] for g in hw["gpus"]] or [0])
    cache.write_text(json.dumps(hw, indent=1), encoding="utf-8")
    return hw


def _ram_gb():
    try:
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.ullTotalPhys / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except Exception:
        return None


def _gpus():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.used,driver_version",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 1),
                         "used_gb": round(float(parts[2]) / 1024, 1), "driver": parts[3] if len(parts) > 3 else ""})
    return gpus


def _show(url, name, cache):
    key = name
    if key in cache:
        return cache[key]
    try:
        d = engines._http(f"{url}/api/show", {"model": name}, timeout=10)
    except Exception:
        return {}
    mi = d.get("model_info") or {}
    ctx = next((v for k, v in mi.items() if k.endswith(".context_length")), None)
    params = mi.get("general.parameter_count")
    info = {"capabilities": d.get("capabilities") or [], "context": ctx,
            "params_b": round(params / 1e9, 1) if params else None, "family": (d.get("details") or {}).get("family"),
            "quant": (d.get("details") or {}).get("quantization_level"),
            "num_ctx": _num_ctx(d.get("parameters") or "")}
    cache[key] = info
    return info


def _num_ctx(params):
    for line in params.splitlines():
        if line.strip().startswith("num_ctx"):
            try:
                return int(line.split()[-1])
            except ValueError:
                pass
    return None


def local_models(settings=None, hw=None):
    s = settings or Settings.load()
    hw = hw or hardware()
    cache_p = data_path("models_show.json")
    try:
        cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    except ValueError:
        cache = {}
    try:
        tags = engines._http(f"{s.ollama_url}/api/tags", timeout=5).get("models", [])
    except Exception:
        return []
    out = []
    vram = hw.get("vram_gb") or 0
    for m in tags:
        info = _show(s.ollama_url, m["name"], cache)
        size = round(m.get("size", 0) / 1e9, 1)
        caps = info.get("capabilities", [])
        # weights + ~15% runtime overhead must fit in VRAM to run fully on the GPU
        fits = bool(vram) and size * 1.15 <= vram
        out.append({"name": m["name"], "engine": "ollama", "size_gb": size, "params_b": info.get("params_b"),
                    "quant": info.get("quant"), "family": info.get("family"), "context": info.get("context"),
                    "num_ctx": info.get("num_ctx"), "vision": "vision" in caps, "tools": "tools" in caps,
                    "thinking": "thinking" in caps, "embedding": "embedding" in caps or "embed" in m["name"],
                    "fits_gpu": fits, "where": "GPU" if fits else ("GPU+CPU (slow)" if vram else "CPU")})
    cache_p.write_text(json.dumps(cache), encoding="utf-8")
    return out


def observed_latency():
    """Median seconds per turn by model, from traces."""
    try:
        from . import traces
        rows = traces.all_traces(time.time() - 14 * 86400)
    except Exception:
        return {}
    by = {}
    for r in rows:
        if r.get("latency_s") and r.get("model"):
            by.setdefault(r["model"], []).append(r["latency_s"])
    return {k: round(statistics.median(v), 2) for k, v in by.items()}


def catalog(settings=None, refresh=False):
    s = settings or Settings.load()
    hw = hardware(refresh)
    lat = observed_latency()
    from . import claude_cli
    cloud = [{"name": k, "engine": "claude-cli", "model_id": claude_cli.MODEL_IDS[k], **v,
              "observed_s": lat.get(claude_cli.MODEL_IDS[k])} for k, v in CLAUDE.items()]
    local = local_models(s, hw)
    for m in local:
        m["observed_s"] = lat.get(m["name"])
    cat = {"hardware": hw, "cloud": cloud, "local": local, "servers": engines.discover(), "time": time.time()}
    data_path("models.json").write_text(json.dumps(cat, indent=1), encoding="utf-8")
    return cat


TASKS = ("chat", "route", "tools", "vision", "embed", "code", "reasoning", "build", "offline", "offline_code")


def pick(task, cat=None, settings=None):
    """Best available model for a task: (engine, model, why)."""
    s = settings or Settings.load()
    cat = cat or catalog(s)
    local = {m["name"]: m for m in cat.get("local", [])}
    gpu_local = [m for m in local.values() if m["fits_gpu"] and not m["embedding"]]
    if task in ("chat", "route", "tools"):
        return "claude", s.router_model if task != "tools" else s.agent_model, "fast Claude on your plan"
    if task == "reasoning":
        return "claude", s.deep_model, "strongest reasoning that answers in seconds"
    if task == "build":
        return "claude", s.builder_model, "long autonomous coding"
    if task == "embed":
        emb = [m for m in local.values() if m["embedding"]]
        return ("ollama", emb[0]["name"], "local embeddings") if emb else ("ollama", s.embed_model, "configured")
    if task == "vision":
        vis = sorted([m for m in gpu_local if m["vision"]], key=lambda m: -(m["params_b"] or 0))
        if vis:
            return "ollama", vis[0]["name"], "largest vision model that fits your GPU (screen stays on the PC)"
        return "claude", s.router_model, "no local vision model fits; Claude sees images too"
    if task in ("offline", "offline_code"):
        want_code = task == "offline_code"
        pool = [m for m in gpu_local if m["tools"] and (not want_code or "coder" in m["name"].lower())] or \
               [m for m in gpu_local if m["tools"]]
        pool.sort(key=lambda m: (-(m["params_b"] or 0), m["size_gb"]))
        if pool:
            return "ollama", pool[0]["name"], "largest tool-capable model that runs fully on your GPU"
        return "ollama", s.local_model, "configured local model"
    if task == "code":
        return "claude", s.agent_deep_model, "coding with tools"
    raise ValueError(f"unknown task {task}; one of {TASKS}")


def recommendations(cat=None, settings=None):
    s = settings or Settings.load()
    cat = cat or catalog(s)
    tips = []
    vram = cat["hardware"].get("vram_gb") or 0
    big = [m for m in cat["local"] if not m["fits_gpu"]]
    if big:
        tips.append(f"{len(big)} installed model(s) don't fit your {vram} GB GPU and will run partly on the CPU "
                    f"(slow): " + ", ".join(f"{m['name']} ({m['size_gb']} GB)" for m in big[:6]) +
                    f". Together they use {round(sum(m['size_gb'] for m in big), 1)} GB of disk.")
    lm = next((m for m in cat["local"] if m["name"].startswith(s.local_model)), None)
    if lm and lm.get("num_ctx") and lm["num_ctx"] < 64000:
        tips.append(f"{s.local_model} has num_ctx {lm['num_ctx']}; Hermes needs 64k.")
    if not any(m["vision"] and m["fits_gpu"] for m in cat["local"]):
        tips.append("No local vision model fits the GPU; screen reading will use Claude.")
    for srv in cat.get("servers", []):
        if srv["kind"] == "openai":
            tips.append(f"{srv['name']} is running at {srv['url']} and can be added as an engine.")
    return tips


def _t_model_catalog(args):
    cat = catalog(refresh=bool(args.get("refresh")))
    task = args.get("task")
    out = {"hardware": cat["hardware"], "cloud": [c["name"] for c in cat["cloud"]],
           "local": [{k: m[k] for k in ("name", "size_gb", "context", "vision", "tools", "where", "observed_s")}
                     for m in cat["local"]], "servers": cat["servers"], "tips": recommendations(cat)}
    if task:
        out["pick"] = dict(zip(("engine", "model", "why"), pick(task, cat)))
    return json.dumps(out, default=str)


MCP_TOOLS = [
    ({"name": "model_catalog", "description": "The AI models available to Waggle (local Ollama models with their "
      "context, vision and tool support and whether they fit the GPU; Claude models), the PC's hardware, running "
      "inference servers, tips, and optionally the best model for a task (chat, tools, vision, embed, code, "
      "reasoning, build, offline, offline_code).",
      "parameters": {"type": "object", "properties": {"task": {"type": "string"}, "refresh": {"type": "boolean"}},
                     "required": []}}, _t_model_catalog),
]
