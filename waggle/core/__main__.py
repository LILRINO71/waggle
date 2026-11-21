"""python -m waggle.core run | stop | restart | status | off [--for 2h] [--all] | on | voice-setup
| watch [--loop | --once | --stop] [--stale-s N]

`off` is the one off switch (the file OFF in the Waggle home, see waggle/core/offswitch.py); `stop` only stops the
core once and the watchdog starts it again."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m waggle.core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the core in this process (used by the scheduled task)")
    sub.add_parser("stop", help="stop the running core gracefully (killed after 20 s)")
    sub.add_parser("restart", help="stop, then start the core")
    sub.add_parser("status", help="print the off switch and health; exit 0 healthy or switched off, 1 unhealthy, "
                                  "2 not running")
    o = sub.add_parser("off", help="switch Waggle off: the watchdog and logon tasks leave it off until `on`")
    o.add_argument("--for", dest="for_", metavar="TIME", default=None,
                   help="switch back on by itself after this long, e.g. 2h, 30m, 1h30m")
    o.add_argument("--all", action="store_true", help="also stop the Telegram gateway, Kokoro and the dashboard")
    sub.add_parser("on", help="switch Waggle back on: remove the switch and start the core (and what off stopped)")
    sub.add_parser("voice-setup", help="print the hands-free line's URL path and token (copy them on your own screen)")
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
    if args.cmd == "voice-setup":
        from waggle.core import voiceline
        for line in voiceline.setup_lines():
            print(line)
        return 0
    from waggle.core import control
    if args.cmd == "stop":
        return 0 if control.stop() else 1
    if args.cmd == "restart":
        if control.restart():
            return 0
        from waggle.core import offswitch
        if offswitch.is_off():
            print("Waggle is switched off; run `python -m waggle.core on` to switch it on")
        return 1
    if args.cmd == "off":
        from waggle.core import offswitch
        try:
            seconds = offswitch.parse_duration(args.for_) if args.for_ else None
        except ValueError as e:
            parser.error(str(e))
        result = control.off(until_s=seconds, all=args.all)
        print(control.off_text(result))
        return 0 if result["ok"] else 1
    if args.cmd == "on":
        result = control.on()
        print(control.on_text(result))
        return 0 if result["ok"] else 1
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
