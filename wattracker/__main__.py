"""Launch entry point."""
from __future__ import annotations

import threading
import time
import webbrowser

import uvicorn

from . import config


def _open_browser(url: str) -> None:
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    from . import db

    host = config.server_host()
    port = config.server_port()
    url = config.browser_url(host, port)
    db.init_db()
    print(f"wattracker running at {url}")
    if config.open_browser_enabled():
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    # proxy_headers=False on purpose. uvicorn defaults it to True with
    # forwarded_allow_ips defaulting to "127.0.0.1" - and this app binds
    # loopback, so EVERY caller is inside the trusted range. That makes
    # X-Forwarded-For a client-controlled override of request.client.host: any
    # local process could name itself an arbitrary address. There is no proxy
    # in front of this app, so no forwarded header should ever be believed.
    uvicorn.run(
        "wattracker.server:app",
        host=host,
        port=port,
        reload=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
