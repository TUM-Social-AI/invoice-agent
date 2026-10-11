"""Launch the browser UI:  python -m src.ui [--port 8000] [--no-browser]"""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from src.ui.app import create_app
from src.ui.bootstrap import build_manager, default_config_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Invoice Agent browser UI")
    parser.add_argument("--config", default=default_config_path(), help="Config file path")
    parser.add_argument("--output", default="output/ui", help="Where uploads and results are stored")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    args = parser.parse_args()

    print("Loading agent and models (this can take a few seconds)...", flush=True)
    manager = build_manager(args.config, args.output)
    app = create_app(manager)

    # Local only: the UI can run an LLM on uploaded files, so never bind a public interface.
    host = "127.0.0.1"
    url = f"http://{host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Invoice Agent UI running at {url}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
