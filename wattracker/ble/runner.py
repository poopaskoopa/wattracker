"""RideController: the workout-runner state machine (pure, testable).

Given a Session (segments with %FTP + seconds) and the user's FTP, it drives a
trainer over ERG: at each tick it computes the current target watts and calls
``trainer.set_target_power``. The workout clock only advances while measured
power > 0. A ride starts after three continuous positive-power seconds, pauses
after three continuous no-power seconds, and resumes on the next positive
sample. Long-inactivity disconnect policy belongs to the WebSocket owner. On
finish the controller records the ride as an activity for the user.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import List, Optional, Tuple

from ..prescribe.planner import Session

_log = logging.getLogger(__name__)

IDLE = "idle"
STARTING = "starting"
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"

_DEFAULT_START_GRACE_S = 3.0
_DEFAULT_ZERO_GRACE_S = 3.0


def _flatten(session: Session) -> Tuple[List[tuple], int]:
    """Flatten a Session into timed blocks: (start, end, 'const'|'ramp', value).

    Intervals expand into steady on/off blocks; warmup/cooldown become ramps.
    ``value`` is a fraction of FTP (float) for const, or (lo, hi) for ramp.
    """
    blocks: List[tuple] = []
    t = 0
    for seg in session.segments:
        if seg.kind == "intervals" and seg.repeat:
            on = int(seg.on_duration or 0)
            off = int(seg.off_duration or 0)
            for _ in range(int(seg.repeat)):
                if on > 0:
                    blocks.append((t, t + on, "const", float(seg.on_power or 0.0)))
                    t += on
                if off > 0:
                    blocks.append((t, t + off, "const", float(seg.off_power or 0.0)))
                    t += off
        elif seg.kind in ("warmup", "cooldown", "ramp"):
            blocks.append(
                (t, t + seg.duration, "ramp",
                 (float(seg.power_low or 0.0), float(seg.power_high or 0.0)))
            )
            t += seg.duration
        else:  # steadystate / freeride
            blocks.append((t, t + seg.duration, "const", float(seg.power or 0.0)))
            t += seg.duration
    return blocks, t


# Public alias: the web layer reuses the flattened timeline for power profiles.
flatten_session = _flatten


class RideController:
    def __init__(
        self,
        session: Session,
        ftp: float,
        trainer=None,
        power_source=None,
        hr_source=None,
        user_id: Optional[int] = None,
        workout_id: Optional[int] = None,
        start_grace_s: float = _DEFAULT_START_GRACE_S,
        zero_grace_s: float = _DEFAULT_ZERO_GRACE_S,
        autosave: bool = True,
        started_at: Optional[_dt.datetime] = None,
        erg_enabled: Optional[bool] = None,
        manage_trainer_commands: bool = True,
    ) -> None:
        self.session = session
        self.ftp = float(ftp)
        self.trainer = trainer
        self.power_source = power_source
        self.hr_source = hr_source
        self.user_id = user_id
        self.workout_id = workout_id
        self.start_grace_s = float(start_grace_s)
        self.zero_grace_s = float(zero_grace_s)
        self.autosave = autosave
        self.manage_trainer_commands = bool(manage_trainer_commands)

        self.blocks, self.total_s = _flatten(session)
        self.status = IDLE
        self.elapsed = 0.0
        self._positive_run = 0.0
        self._zero_run = 0.0
        self._ever_started = False
        self.erg_available = bool(
            trainer is not None and getattr(trainer, "erg_available", True)
        )
        self._erg_armed = bool(
            self.erg_available and getattr(trainer, "erg_enabled", False)
        )
        self.erg_enabled = bool(
            self.erg_available
            and (True if erg_enabled is None else erg_enabled)
        )

        self.current_power = 0
        self.current_cadence: Optional[float] = None
        self.current_hr: Optional[int] = None
        self.current_target = 0

        self._samples = {"power": [], "cadence": [], "heartrate": []}
        self.started_at = started_at
        self.activity_id: Optional[int] = None
        self.saved_record: Optional[dict] = None

    # ---------------------------------------------------------- targets
    def target_fraction(self, t: float) -> float:
        """%FTP fraction at elapsed second `t`."""
        for (s, e, kind, val) in self.blocks:
            if s <= t < e:
                if kind == "const":
                    return val
                lo, hi = val
                return lo + (hi - lo) * ((t - s) / (e - s)) if e > s else lo
        if self.blocks:  # past the end -> hold the final block's value
            s, e, kind, val = self.blocks[-1]
            return val if kind == "const" else val[1]
        return 0.0

    def target_watts(self, t: float) -> int:
        return int(round(self.target_fraction(t) * self.ftp))

    def _block_index(self, t: float) -> int:
        for i, (s, e, _k, _v) in enumerate(self.blocks):
            if s <= t < e:
                return i
        return max(0, len(self.blocks) - 1)

    # ------------------------------------------------------------ ticks
    def tick(
        self,
        power: int = 0,
        cadence: Optional[float] = None,
        hr: Optional[int] = None,
        dt: float = 1.0,
    ) -> dict:
        """Advance the state machine by `dt` seconds with a measured `power`."""
        if self.status == FINISHED:
            return self.state()

        p = int(power or 0)
        self.current_power = p
        self.current_cadence = cadence
        self.current_hr = hr

        if p > 0:
            if self.status in (IDLE, STARTING):
                self._positive_run += dt
                if self._positive_run < self.start_grace_s:
                    self.status = STARTING
                    self.current_target = self.target_watts(0)
                    return self.state()
                self.status = RUNNING
                if self.started_at is None:
                    self.started_at = _dt.datetime.now()
                if self.erg_enabled and not self._erg_armed:
                    self._trainer_call("start_erg")
                    self._erg_armed = True
                if self.start_grace_s > 0:
                    # Countdown power proves the rider is ready but is not part
                    # of the prescribed workout or its saved samples.
                    self._positive_run = self.start_grace_s
                    self._zero_run = 0.0
                    self.current_target = self.target_watts(0)
                    if self.erg_enabled:
                        self._trainer_call("set_target_power", self.current_target)
                    return self.state()
            elif self.status == PAUSED:
                self.status = RUNNING
                # (Re-)arm ERG on every start AND resume (FTMS Request Control +
                # Start/Resume). When the rider stops, many trainers drop out of
                # ERG (or, after an FTMS Stop/Pause, refuse control until a fresh
                # Start/Resume). A bare set_target_power below does NOT put them
                # back in ERG, so on resume the trainer free-rides below target.
                # Request Control + Start is idempotent when already in ERG, so
                # re-issuing it on each resume is safe.
                if self.erg_enabled:
                    self._trainer_call("start_erg")
                    self._erg_armed = True
            self._positive_run = self.start_grace_s
            self._zero_run = 0.0
            self.elapsed += dt
            # Only real ride time counts as "started": completing the start gate
            # alone must never persist a zero-duration, zero-sample activity.
            self._ever_started = True

            # Set the ERG target for the current position. Sent every tick, so
            # it both follows segment/ramp changes and acts as a keepalive.
            self.current_target = self.target_watts(min(self.elapsed, self.total_s))
            if self.erg_enabled:
                self._trainer_call("set_target_power", self.current_target)

            # record a sample for the ride file
            self._samples["power"].append(p)
            self._samples["cadence"].append(cadence)
            self._samples["heartrate"].append(hr)

            if self.elapsed >= self.total_s:
                self._finish()
        else:
            self._positive_run = 0.0
            if self.status == STARTING:
                self.status = IDLE
            # Do not pause on a dropped packet/brief coast. Long inactivity is
            # finalized by the WebSocket so device cleanup remains centralized.
            if self.status == RUNNING:
                self._zero_run += dt
                if self._zero_run >= self.zero_grace_s:
                    self.status = PAUSED
        return self.state()

    def poll(self, dt: float = 1.0) -> dict:
        """Read the attached sources (advancing simulated ones), then tick."""
        for src in (self.power_source, self.hr_source):
            adv = getattr(src, "advance", None)
            if callable(adv):
                adv()
        p = self.power_source.latest_power() if self.power_source else 0
        cad = self.power_source.latest_cadence() if self.power_source else None
        hr = self.hr_source.latest_hr() if self.hr_source else None
        return self.tick(power=int(p or 0), cadence=cad, hr=hr, dt=dt)

    def stop(self) -> dict:
        """Manual stop/finish."""
        if self.status != FINISHED:
            self._finish()
        return self.state()

    @property
    def has_started(self) -> bool:
        return self._ever_started

    def set_erg_enabled(self, enabled: bool, command_trainer: bool = True) -> bool:
        """Enable/disable ERG without changing prescribed-target display."""
        requested = bool(enabled)
        if requested and not self.erg_available:
            self.erg_enabled = False
            return False
        if (
            requested == self.erg_enabled
            and (not command_trainer or not requested or self._erg_armed)
        ):
            return self.erg_enabled
        self.erg_enabled = requested
        if not command_trainer:
            self._erg_armed = requested
            return self.erg_enabled
        if requested:
            self._trainer_call("start_erg")
            self._erg_armed = True
            target = self.target_watts(min(self.elapsed, self.total_s))
            self.current_target = target
            self._trainer_call("set_target_power", target)
        else:
            self._trainer_call("set_target_power", 0)
            self._trainer_call("stop_erg")
            self._erg_armed = False
        return self.erg_enabled

    def update_sources(self, trainer=None, power_source=None, hr_source=None) -> None:
        """Replace live BLE role bindings after a per-device disconnect."""
        self.trainer = trainer
        self.power_source = power_source
        self.hr_source = hr_source
        available = bool(
            trainer is not None and getattr(trainer, "erg_available", True)
        )
        if not available:
            self.erg_enabled = False
        self.erg_available = available
        self._erg_armed = bool(
            available and getattr(trainer, "erg_enabled", False)
        )

    def _trainer_call(self, method: str, *args) -> None:
        """Invoke a trainer command, degrading gracefully (no trainer / BLE error)."""
        if self.trainer is None or not self.manage_trainer_commands:
            return
        fn = getattr(self.trainer, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            _log.warning("trainer %s failed; continuing without ERG", method,
                         exc_info=True)

    # --------------------------------------------------------- finishing
    def _finish(self) -> None:
        self.status = FINISHED
        if self.trainer is not None:
            self._trainer_call("set_target_power", 0)
            self._trainer_call("stop_erg")
        self.erg_enabled = False
        self._erg_armed = False
        if self.autosave and self.user_id is not None and self._ever_started:
            try:
                self._save()
            except Exception:
                # Recording must never crash the ride; leave activity_id None.
                pass

    def _save(self) -> None:
        from .. import db
        from ..ingest import importer

        started = self.started_at or _dt.datetime.now()
        n = len(self._samples["power"])
        times = [(started + _dt.timedelta(seconds=i)).isoformat() for i in range(n)]
        streams = {
            "time": times,
            "power": self._samples["power"],
            "cadence": self._samples["cadence"],
            "heartrate": self._samples["heartrate"],
            "distance": [],
            "altitude": [],
        }
        parsed = {
            "start_time": started.isoformat(),
            "duration_s": int(self.elapsed),
            "streams": streams,
        }
        name = f"Ride {started.date().isoformat()} {self.session.name}"
        record = importer._build_record(parsed, name, self.ftp)
        self.saved_record = record
        self.activity_id = db.insert_activity(self.user_id, record)
        if self.activity_id is not None and self.workout_id is not None:
            importer.link_selected_plan_workout(
                self.user_id, self.workout_id, self.activity_id
            )
        try:
            importer.maybe_update_ftp(self.user_id)
        except Exception:
            pass

    # ------------------------------------------------------------- state
    def state(self) -> dict:
        clamped = min(self.elapsed, self.total_s)
        return {
            "status": self.status,
            "elapsed": round(self.elapsed, 1),
            "total": self.total_s,
            "segment_index": self._block_index(clamped),
            "segment_count": len(self.blocks),
            "target_watts": self.current_target,
            "power": self.current_power,
            "cadence": round(self.current_cadence, 1)
            if self.current_cadence is not None else None,
            "hr": self.current_hr,
            "progress": round(clamped / self.total_s, 3) if self.total_s else 0.0,
            "ftp": self.ftp,
            "name": self.session.name,
            "activity_id": self.activity_id,
            "workout_id": self.workout_id,
            "erg_available": self.erg_available,
            "erg_enabled": self.erg_enabled,
            "start_countdown": round(
                max(0.0, self.start_grace_s - self._positive_run), 1
            ) if not self._ever_started else 0.0,
            "no_power_s": round(self._zero_run, 1),
        }
