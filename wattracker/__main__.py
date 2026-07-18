"""Launch entry point: start uvicorn and open the browser."""
from __future__ import annotations

import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://localhost:{PORT}"


def _open_browser() -> None:
    time.sleep(1.2)
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def main() -> None:
    from . import db

    db.init_db()
    print(f"wattracker running at {URL}")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("wattracker.server:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
