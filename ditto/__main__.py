"""Entry point: python3 -m ditto"""

import argparse
import sys

from . import config
from .core import Service
from .web import create_app


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ditto")
    p.add_argument("--port", type=int, default=config.PORT)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--headless", action="store_true",
                   help="no GPIO/OLED — for development on a laptop")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    service = Service(headless=args.headless)
    app = create_app(service)

    if args.debug:
        app.run(host=args.host, port=args.port, threaded=True)
    else:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8,
              channel_timeout=3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
