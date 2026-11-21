"""A detached job run: `python -m waggle.core.jobrun <job_id> <owner>`, started by the core.

It loads the plug-ins to find the handler, records its own pid on the job (so a core that died before doing
so does not requeue a job that is still running), renews the lease while the handler runs and records the
outcome as the job's owner. It keeps running if the core exits; the next core re-adopts it. Like the core it makes every child it starts
windowless (procs.install_quiet_children)."""
import logging
import os
import sys
import threading

from waggle.core import jobs, plugins, procs

log = logging.getLogger("waggle.core.jobrun")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python -m waggle.core.jobrun <job_id> <owner>", file=sys.stderr)
        return 64
    job_id, owner = int(argv[0]), argv[1]
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s jobrun {job_id} %(levelname)s %(message)s")
    plugins.load(record=False)
    if not jobs.set_pid(job_id, owner, os.getpid()):
        log.warning("job %s is no longer held by %s; not running it", job_id, owner)
        return 2
    job = jobs.get(job_id)
    h = jobs.handler(job["kind"])
    if h is None:
        jobs.fail(job_id, owner, f"no handler registered for {job['kind']!r} in the worker process")
        return 1
    done = threading.Event()

    def keep_lease():
        while not done.wait(max(1.0, h.lease_s / 3)):
            if not jobs.heartbeat(job_id, owner, lease_s=h.lease_s):
                log.warning("lost the lease")
                return

    threading.Thread(target=keep_lease, daemon=True).start()
    try:
        result = jobs.invoke(h, job)
    except Exception as e:  # recorded on the job
        log.exception("handler failed")
        jobs.fail(job_id, owner, f"{type(e).__name__}: {e}")
        return 1
    finally:
        done.set()
    return 0 if jobs.complete(job_id, owner, result) else 2


if __name__ == "__main__":
    procs.install_quiet_children()  # a handler's children (git, claude, ...) never flash a window
    sys.exit(main())
