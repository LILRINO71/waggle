"""Modules the core loads at start. Each listed module defines `register_core()`, which registers its job
kinds, timers and transports. A module that fails to import or register is recorded (event and a failed
capability `plugin:<module>`) and the core keeps running without it.

WAGGLE_CORE_PLUGINS (comma-separated module names) adds modules for tests and experiments; with
WAGGLE_CORE_PLUGINS_ONLY=1 those are the only modules loaded (tests use this so a test core never loads the
production plug-ins)."""
import importlib
import logging
import os
import traceback

from waggle.core import events, ledger

MODULES = ["waggle.core.transports", "waggle.chat_jobs", "waggle.tick", "waggle.knowledge_core", "waggle.autonomy_core",
           "waggle.dropbox_ingest", "waggle.canary", "waggle.calendar_watch", "waggle.proposals", "waggle.learnloop",
           "waggle.backup", "waggle.duties_core", "waggle.evals", "waggle.deadman", "waggle.nightshift",
           "waggle.skillscout"]

MODULES += ["waggle.aw_sensor", "waggle.focus", "waggle.recipes"]  # ActivityWatch sensing (10-min poll) and focus-block nudges

log = logging.getLogger("waggle.core.plugins")


def modules():
    extra = [m.strip() for m in os.environ.get("WAGGLE_CORE_PLUGINS", "").split(",") if m.strip()]
    if os.environ.get("WAGGLE_CORE_PLUGINS_ONLY") == "1":
        return extra
    return [*MODULES, *(m for m in extra if m not in MODULES)]


def load(*, record=True):
    """Import every module and call its register_core(). Returns {"loaded": [...], "failed": {name: error}}."""
    out = {"loaded": [], "failed": {}}
    for name in modules():
        try:
            module = importlib.import_module(name)
            register = getattr(module, "register_core", None)
            if register is None:
                raise AttributeError(f"{name} has no register_core()")
            register()
        except Exception as e:  # a broken plug-in must never stop the core
            error = f"{type(e).__name__}: {e}"
            out["failed"][name] = error
            log.error("plug-in %s failed: %s\n%s", name, error, traceback.format_exc())
            if record:
                events.emit("core.plugin_failed", source="core", ref=name, data={"error": error})
                ledger.record(f"plugin:{name}", False, method="import and register_core at core start",
                              detail=error)
            continue
        out["loaded"].append(name)
        if record:
            ledger.record(f"plugin:{name}", True, method="import and register_core at core start",
                          ttl_s=7 * 86400)
    return out
