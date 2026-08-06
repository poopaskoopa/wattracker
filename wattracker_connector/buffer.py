"""Keep a ride safe when the network is not.

This is why the ride is worth proxying at all rather than just streaming and
hoping. A ride is an hour of the rider's actual effort; a dropped wifi link for
thirty seconds of it must not cost the whole session.

Two halves:

* While a ride is connected, every sample is written to a file on this
  machine as well as sent. Losing the link, the process, or the power does not
  lose what has already been ridden.
* When the link comes back, whatever was buffered is POSTed to
  ``/api/connector/ride``, which runs the identical save chain an in-process
  ride uses. A ride that spanned a reconnect lands as the same row as one that
  did not.

Deliberately a plain JSON-lines file rather than a database: it appends
cheaply, survives a hard kill mid-write (a torn last line is simply dropped),
and needs nothing this package is not already allowed to import.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from .config import config_dir

log = logging.getLogger(__name__)

_BUFFER_NAME = "ride-buffer.jsonl"

# Matches the server's MAX_BUFFERED_RIDE_SAMPLES. A day of 1 Hz samples, which
# no real ride approaches - a runaway sampler must not fill the disk.
MAX_SAMPLES = 86400


def buffer_path() -> str:
    return os.path.join(config_dir(), _BUFFER_NAME)


class RideBuffer:
    """Append-only record of the ride currently being ridden."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or buffer_path()
        self._count = 0
        self._open = False

    @property
    def path(self) -> str:
        return self._path

    def start(self, started_at: str, name: str, ftp: float,
              workout_id: Optional[int]) -> None:
        """Begin a ride, discarding any earlier one.

        A new ride starting means the previous buffer either uploaded or is
        never going to: keeping it would eventually replay a stale ride into
        the middle of a real one.
        """
        header = {
            "kind": "start", "started_at": started_at, "name": name,
            "ftp": float(ftp), "workout_id": workout_id,
        }
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(header) + "\n")
            self._count = 0
            self._open = True
        except OSError:
            log.warning("could not open the ride buffer at %s", self._path,
                        exc_info=True)
            self._open = False

    def append(self, power=None, cadence=None, hr=None) -> None:
        if not self._open or self._count >= MAX_SAMPLES:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps([power, cadence, hr]) + "\n")
            self._count += 1
        except OSError:
            log.debug("could not append to the ride buffer", exc_info=True)

    def finish(self) -> None:
        self._open = False

    def discard(self) -> None:
        self._open = False
        self._count = 0
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def load(self) -> Optional[Dict]:
        """Read a buffered ride back, or None if there is nothing usable.

        A torn final line - the process was killed mid-write - is dropped
        rather than failing the whole ride: one lost second beats one lost
        hour.
        """
        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return None
        if not lines:
            return None
        try:
            header = json.loads(lines[0])
        except ValueError:
            return None
        if not isinstance(header, dict) or header.get("kind") != "start":
            return None

        power: List = []
        cadence: List = []
        heartrate: List = []
        for line in lines[1:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # torn last line
            if not isinstance(row, list) or len(row) != 3:
                continue
            power.append(row[0] or 0)
            cadence.append(row[1])
            heartrate.append(row[2])
        if not power:
            return None
        return {
            "started_at": header.get("started_at"),
            "name": header.get("name") or "Ride",
            "ftp": header.get("ftp") or 0.0,
            "workout_id": header.get("workout_id"),
            "duration_s": len(power),
            "samples": {
                "power": power, "cadence": cadence, "heartrate": heartrate,
            },
        }


def upload_pending(server_url: str, token: str,
                   buffer: RideBuffer) -> Optional[int]:
    """POST a buffered ride, if there is one. Returns the activity id.

    Synchronous, and called from a thread: it is one blocking urllib request
    that happens at most once per reconnect, and stdlib urllib keeps the
    connector free of a second HTTP dependency alongside websockets.

    The buffer is discarded only on a definite answer - a stored ride or a
    recognised duplicate. Anything else (network error, 5xx) leaves it in
    place to be retried on the next reconnect, because the alternative is
    throwing away a ride to tidy up a file.
    """
    pending = buffer.load()
    if pending is None:
        return None

    import urllib.error
    import urllib.request

    payload = json.dumps(pending).encode("utf-8")
    request = urllib.request.Request(
        server_url.rstrip("/") + "/api/connector/ride",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code != 429:
            # The server will never accept this ride - a malformed or
            # unauthorised payload will not become valid by being retried, and
            # keeping it would block every later ride behind it.
            log.error("server rejected the buffered ride (%s); discarding",
                      exc.code)
            buffer.discard()
            return None
        log.warning("could not upload the buffered ride (%s); will retry",
                    exc.code)
        return None
    except Exception as exc:
        log.warning("could not upload the buffered ride (%s); will retry", exc)
        return None

    activity_id = body.get("activity_id")
    log.info(
        "uploaded buffered ride -> activity %s%s",
        activity_id, " (already stored)" if body.get("duplicate") else "",
    )
    buffer.discard()
    return activity_id
