"""An in-process connector, attached over the real /connector/ws socket.

This is what makes the whole server/client split testable without a second
machine: the connector's own handlers run against a temp directory pretending
to be a Zwift install, and they are reached through the genuine WebSocket
route, RPC framing and token auth - not a stub of any of it.

Starlette's WebSocketTestSession is synchronous and lives in the calling
thread, so the connector loop runs in a background thread while the test body
carries on. That mirrors production, where the connector is a separate process
entirely.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Optional

from wattracker import connectorauth, rpc
from wattracker_connector.handlers import ConnectorConfig, build_handlers


class FakeConnector:
    """Runs the real connector handlers against a test websocket session."""

    def __init__(self, session, handlers, on_error=None):
        self._session = session
        self._handlers = handlers
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.errors = []
        self._on_error = on_error

    def start(self) -> "FakeConnector":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._serve())
        except Exception as exc:  # the socket closing ends the loop
            self.errors.append(exc)
        finally:
            loop.close()

    async def _serve(self) -> None:
        peer = rpc.RpcPeer(_TestSocket(self._session))
        while not self._stop.is_set():
            # WebSocketTestSession.receive_text blocks the calling thread, and
            # this one is running an event loop. Awaiting it directly would
            # pin that loop between requests, which is invisible while the
            # connector only answers calls and fatal the moment it also has
            # something of its own to do: the BLE sampler is a background task
            # on this loop, and a blocked loop never lets it take a sample.
            message = rpc.decode(await asyncio.to_thread(self._session.receive_text))
            if "method" in message:
                await peer.serve(message, self._handlers)

    def stop(self) -> None:
        self._stop.set()


class _TestSocket:
    """Gives RpcPeer its two methods over a synchronous test session.

    The sends are blocking calls made from the connector thread, which is
    exactly how the real client behaves from its own process.
    """

    def __init__(self, session) -> None:
        self._session = session

    async def send_text(self, text: str) -> None:
        self._session.send_text(text)

    async def receive_text(self) -> str:
        return self._session.receive_text()


class FakeRadio:
    """A trainer and power meter for connectors that have to be ridden.

    Deliberately minimal: it exists so the BLE handlers - the real ones - have
    something to sample, when what is under test is somewhere else entirely
    (a phone's browser, say) and the hardware is not the point.
    """

    def __init__(self, power=200, cadence=90.0, hr=140):
        self.power = power
        self.cadence = cadence
        self.hr = hr
        self.erg_available = True
        self.erg_enabled = False
        self.targets = []

    # -- the sensors, which are this same object --------------------------
    def latest_power(self):
        return self.power

    def latest_cadence(self):
        return self.cadence

    def latest_hr(self):
        return self.hr

    # -- the trainer -------------------------------------------------------
    async def async_enable_erg(self, watts=None):
        self.erg_enabled = True
        self.targets.append(watts)

    async def async_set_target_power(self, watts):
        self.targets.append(watts)

    async def async_stop(self):
        self.erg_enabled = False

    async def async_disable_erg(self):
        self.erg_enabled = False

    # -- the adapter -------------------------------------------------------
    def bluetooth_available(self):
        return True, "ok"

    async def scan(self, timeout=5.0, attempts=2):
        return [{"address": "AA", "name": "FakeKickr", "roles": ["trainer"]}]

    async def connect_sensors(self, timeout=6.0, selected=None):
        return {
            "trainer": self,
            "power_source": self,
            "hr_source": self,
            "clients": [],
            "clients_by_address": {},
            # The server builds its proxy sources from these roles, not from
            # what the sensors above happen to return, so a role missing here
            # reads as no such sensor however lively the readings are.
            "bindings": {
                "AA": {"name": "FakeKickr",
                       "roles": {"power": None, "trainer": None}},
                "BB": {"name": "FakeHRM", "roles": {"hr": None}},
            },
            "names": {"power": "FakeKickr", "trainer": "FakeKickr",
                      "hr": "FakeHRM"},
            "errors": [],
        }

    async def disconnect_sensor(self, conn, address):
        return conn


def attach_connector(client, user_id, zwift_home, label="Test PC", radio=None):
    """Pair a device, open /connector/ws, and run a connector on it.

    Returns ``(context_manager, config)``. Use as::

        with attach_connector(client, uid, tmp_path) as conn:
            ...

    Pass ``radio`` (a FakeRadio, or anything shaped like
    ``wattracker.ble.devices``) to additionally serve the ble.* methods, so the
    ride path can be driven end to end. Without it the connector answers only
    the file-and-folder half, which is what most callers want.
    """
    _device_id, token = connectorauth.generate_token(user_id, label)
    config = ConnectorConfig(
        activities_dir=str(zwift_home / "Activities"),
        workouts_dir=str(zwift_home / "Workouts"),
    )
    return _Attached(client, token, build_handlers(config), radio), config


class _Attached:
    def __init__(self, client, token, handlers, radio=None):
        self._client = client
        self._token = token
        self._handlers = handlers
        self._radio = radio
        self._ws = None
        self._connector = None

    def __enter__(self):
        self._ws = self._client.websocket_connect(
            "/connector/ws", headers={"Authorization": f"Bearer {self._token}"}
        )
        session = self._ws.__enter__()
        # The server greets first; drain it so the connector loop starts clean.
        session.receive_json()
        handlers = dict(self._handlers)
        if self._radio is not None:
            handlers.update(self._ble_handlers(session))
        self._connector = FakeConnector(session, handlers).start()
        return self._connector

    def _ble_handlers(self, session):
        """The real BLE handlers, over the fake radio, on the real socket.

        Events go back the way the client's do - written straight onto the
        same session - so the server's own ble.sample routing is exercised
        rather than stubbed.
        """
        from wattracker_connector import ble_handlers as blemod

        self._patched_radio = blemod.bledevices
        blemod.bledevices = self._radio

        async def send_event(event, **fields):
            session.send_text(rpc.encode({"event": event, **fields}))

        self._ble_state = blemod.BleState()
        return blemod.build_ble_handlers(self._ble_state, send_event)

    def __exit__(self, *exc):
        if self._connector is not None:
            self._connector.stop()
        if self._radio is not None:
            from wattracker_connector import ble_handlers as blemod

            blemod.bledevices = self._patched_radio
        return self._ws.__exit__(*exc)
