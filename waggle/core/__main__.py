"""python -m waggle.core run | stop | restart | status | watch [--loop | --once | --stop] [--stale-s N]"""
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
    w.add_argument("--every-s", type=float, default=None, help="seconds between passes of --loop (default 10)")
    mode = w.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="stay running and pass every 10 s (the scheduled task)")
    mode.add_argument("--once", action="store_true", help="one pass (the default)")
    mode.add_argument("--stop", action="store_true", help="stop the running long-lived watchdog")
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
    extra = {} if args.stale_s is None else {"stale_s": args.stale_s}
    if args.stop:
        return 0 if control.stop_watchdog() else 1
    if args.loop:
        if args.every_s is not None:
            extra["interval_s"] = args.every_s
        return control.watch_loop(**extra)
    result = control.watch(**extra)
    if sys.stdout:
        print(f"watch: {result}")
    return 0


if __name__ == "__main__":
    from waggle.core import procs
    procs.install_quiet_children()  # every child of the core, its plug-ins and the watchdog is windowless
    sys.exit(main())
