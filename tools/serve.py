"""
Deterministic development server launcher.

`python app.py` starts Werkzeug with debug=True, which enables the
automatic reloader. The reloader spawns a child process, which is awkward
for scripts and test runners that must start and stop the server cleanly.

This launcher imports the same Flask application and runs it without the
reloader, so exactly one process owns port 5000.

Usage:
    python tools/serve.py                       # 0.0.0.0:5000
    python tools/serve.py --host 127.0.0.1      # local only
    python tools/serve.py --port 8080
"""

import argparse
import os
import sys


BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)


def main(argv=None):

    parser = argparse.ArgumentParser(
        description="Run the VTU search API server.",
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=5000,
    )

    arguments = parser.parse_args(argv)

    sys.path.insert(0, BASE_DIR)

    import app

    app.app.run(
        host=arguments.host,
        port=arguments.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
