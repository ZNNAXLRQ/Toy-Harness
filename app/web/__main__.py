import argparse

import uvicorn

from app.web.api import create_app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local read-only Toy Harness HTTP API")
    parser.add_argument("--workspace", required=True, help="Existing directory to expose to read tools")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--offline", action="store_true", help="Use the scripted FakeProvider")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        app = create_app(args.workspace, offline=args.offline)
    except (OSError, ValueError):
        parser.error("--workspace must be an existing, accessible workspace directory")
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()