"""The real-world tool packs (branch rebuild/tools, 2026-11-20), gathered for waggle_mcp's provider list.

Each pack is one module with its own MCP_TOOLS; delete a pack's file (and its test and skill) and it simply drops out
of the list here. Nothing is registered with Hermes: these tools are for the Claude chat brain (and background agents)
through the Waggle MCP server. Shared rules (never overwrite, only write where Waggle may, no console window, JSON
answers) live in tools_common.
"""
import importlib
import sys

PACKS = ("tools_cad", "tools_print", "tools_docs", "tools_media", "tools_github", "tools_explain")


def collect(packs=PACKS, importer=importlib.import_module):
    """Every (spec, fn) the packs offer; a pack whose module is gone is skipped."""
    found = []
    for name in packs:
        try:
            mod = importer(f"waggle.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"waggle.{name}":  # the pack exists but one of its own imports is broken: say so, keep the rest
                print(f"tools_ext: pack {name} not loaded: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"tools_ext: pack {name} not loaded: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        found.extend(getattr(mod, "MCP_TOOLS", []))
    return found


MCP_TOOLS = collect()
