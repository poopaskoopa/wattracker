"""A TCP proxy you can cut, for testing what the connector does when the
network goes away.

The connector's reconnect and ride-buffering behavior only means anything if
someone has actually taken the network away mid-ride. The obvious ways to do
that - disable the adapter, or firewall the server - are unusable on the
machine where this matters most: a Windows box with Zwift on it is often
reached over RDP across the same LAN as the server, so both of those sever the
session doing the testing. A firewall rule is also a system-wide change to
undo afterwards.

Putting a proxy in the path gives the connector a real transport failure -
connection refused, and truncation of an established WebSocket - while
touching nothing but this process:

    python tests/windows/breakable_proxy.py 18000 192.168.1.10 8000
    wattracker-connector --server http://127.0.0.1:18000 --token ... --save

Kill the proxy to drop the link; start it again to restore it. The connector
should back off and reconnect on its own, with no close code 4409 involved -
that one is deliberately fatal and means a second connector displaced this one.

The Host header is rewritten to the upstream's real host:port on the way
through. Without that the server's WATTRACKER_PUBLIC_HOSTS allowlist refuses
"127.0.0.1:18000", correctly, and the drop being tested is indistinguishable
from a misconfigured allowlist.

Stdlib only, and deliberately not a pytest test: it is a manual instrument for
hardware sessions, not something CI can meaningfully run.
"""
from __future__ import annotations

import asyncio
import sys


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                authority: bytes, rewrite_host: bool) -> None:
    """Copy one direction, rewriting Host in the first request's headers."""
    first = True
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if first and rewrite_host:
                head, separator, rest = data.partition(b"\r\n\r\n")
                if separator:
                    lines = [
                        b"Host: " + authority
                        if line.lower().startswith(b"host:") else line
                        for line in head.split(b"\r\n")
                    ]
                    data = b"\r\n".join(lines) + separator + rest
                first = False
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass  # the point of this tool is that connections die
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _serve(listen_port: int, host: str, port: int) -> None:
    authority = f"{host}:{port}".encode()

    async def handle(client_reader, client_writer):
        peer = client_writer.get_extra_info("peername")
        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            print(f"upstream refused for {peer}: {exc}", flush=True)
            client_writer.close()
            return
        print(f"open {peer}", flush=True)
        await asyncio.gather(
            _pump(client_reader, up_writer, authority, rewrite_host=True),
            _pump(up_reader, client_writer, authority, rewrite_host=False),
        )
        print(f"close {peer}", flush=True)

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    print(f"proxy listening on 127.0.0.1:{listen_port} -> {host}:{port}",
          flush=True)
    async with server:
        await server.serve_forever()


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    listen_port, host, port = int(argv[0]), argv[1], int(argv[2])
    try:
        asyncio.run(_serve(listen_port, host, port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
