"""python -m waggle.core run | stop | restart | status | watch [--stale-s N]"""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m waggle.core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the core in this process (used by the scheduled task)")
    sub.add_parser("stop", help="stop the running core gracefully (killed after 20 s)")
    sub.add_parser("restart", help="stop, then start the core")
    sub.add_parser("status", help="print health; exit 0 healthy, 1 unhealthy, 2 not running")
    w = sub.add_parser("watch", help="start or restart the core when it is down or its heartbeat is stale")
    w.add_argument("--stale-s", type=float, default=None)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        from waggle.core import runtime
        return runtime.main()
    from waggle.core import control
    if args.cmd == "stop":
        return 0 if control.stop() else 1
    if args.cmd == "restart":
        return 0 if control.restart() else 1
    if args.cmd == "status":
        return control.status()
    result = control.watch(**({} if args.stale_s is None else {"stale_s": args.stale_s}))
    if sys.stdout:
        print(f"watch: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
