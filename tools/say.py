"""Send ONE message to the live Waggle (through the Home page's path, the core voice line) and print what happened.

  py -3.14 tools/say.py "what is on my plate today?"           test-marked: answered, never learned from
  py -3.14 tools/say.py --as-owner "..."                       the owner's own words (only on the owner's word)
  py -3.14 tools/say.py --json "..."                           one JSON object instead of text

Prints the reply, the turn's status, how long it took and every tool Waggle called with a short result. A turn that
is still running after the voice line's hold time is followed in traces.db for up to --follow seconds.
Waits its turn when another voice request is running (the line takes one at a time).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--as-owner", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--follow", type=int, default=scenarios.FOLLOW_S)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args.as_owner:
        scenarios.MARK = ""
    scenarios.FOLLOW_S = args.follow
    r = scenarios.run_turn({"text": args.text, "expect": ""}, [], use_judge=False)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"status {r.get('status')}  {r.get('seconds_total', r['seconds'])} s  turn {r.get('turn')}  "
              f"origin {r.get('origin')}  ok {r.get('trace_ok')}{'  (held, followed)' if r.get('held') else ''}")
        for s in r["steps"]:
            print(f"  tool {s['tool']}{' DENIED' if s['denied'] else '' if s['ok'] else ' FAILED'}: "
                  f"{s['input'][:140]} -> {s['result'][:200]}")
        print("\n" + (r.get("reply") or "(no reply)"))
    return 0 if r.get("code") == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
