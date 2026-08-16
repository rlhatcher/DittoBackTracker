"""Entry point: python3 -m ditto"""

import argparse
import signal
import sys
import threading

from . import config
from .core import Service
from .web import create_app


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ditto")
    p.add_argument("--port", type=int, default=config.PORT)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    service = Service()

    # systemd stops us with SIGTERM. Without this the pedal stays mounted, so
    # the next start finds a dirty filesystem.
    done = threading.Event()

    def on_term(_signum, _frame):
        # Keep the handler minimal: just unblock the server. The real, blocking
        # shutdown runs in the finally below, off the signal handler, so a long
        # drain never executes at an arbitrary point with a lock held.
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_term)

    try:
        # Inside the try: Service() has already started its threads, so a
        # failure building the app must still run the finally cleanup.
        app = create_app(service)
        if args.debug:
            app.run(host=args.host, port=args.port, threaded=True)
        else:
            from waitress import serve
            # Every open SSE stream occupies a thread for its whole life, so
            # the pool needs room for a few stale tabs on top of real
            # requests. web.SSE_MAX_SECONDS is what actually bounds them.
            serve(app, host=args.host, port=args.port, threads=16,
                  channel_timeout=3600)
    finally:
        if not done.is_set():
            done.set()
            service.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
