"""Tests for the explicit, offline FTP/TSS repair command.

Each test marked GUARD was checked to FAIL against the pre-fix tool recovered
from ``c8cf3dc``; the defect it guards is named in its docstring.
"""
import json
import shutil
import sqlite3

import pytest

from wattracker import db
from wattracker import ftp_backfill
from wattracker.metrics.power import intensity_factor, training_stress_score

FTP = 250.0
NP = 263.0
DURATION = 600
# What the fixture rows currently store: scored against the 200 W default that
# every batch import fell back to, consistent with NP and DURATION.
STALE_IF = 1.315
STALE_TSS = 28.8


def _correct(ftp=FTP, np_value=NP, duration=DURATION):
    """The IF/TSS the repair should land on, computed the app's own way."""
    return (
        round(intensity_factor(np_value, ftp), 3),
        round(training_stress_score(duration, np_value, ftp), 1),
    )


def _database(tmp_path, *, rows=1, ftp_date="2026-08-01", ftp=FTP):
    path = tmp_path / "history.db"
    db.init_db(str(path))
    uid = db.create_user("rider", "hash", path=str(path))
    db.add_ftp_entry(uid, ftp_date, ftp, "manual", path=str(path))
    ids = []
    for index in range(rows):
        activity_id = db.insert_activity(
            uid,
            {
                "dedup_hash": f"ride-{index}",
                "filename": f"ride-{index}.fit",
                "start_time": f"2026-08-01T10:{index:02d}:00",
                "duration_s": DURATION,
                "avg_power": NP,
                "np": NP,
                # Scored against the 200 W default, not this user's 250 W.
                "if_": STALE_IF,
                "tss": STALE_TSS,
                "streams": {"power": [NP] * DURATION},
            },
            path=str(path),
        )
        ids.append(activity_id)
    return path, uid, ids


def _set(path, uid, activity_id, **columns):
    conn = db.connect(str(path))
    try:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(
            f"UPDATE activities SET {assignments} WHERE user_id = ? AND id = ?",
            (*columns.values(), uid, activity_id),
        )
        conn.commit()
    finally:
        conn.close()


def _args(path, state, backup, *extra):
    return [
        "--db", str(path), "--write", "--state", str(state),
        "--backup", str(backup), *extra,
    ]


# --- safety machinery recovered unchanged from #59 ----------------------------

def test_dry_run_is_default_and_does_not_write(tmp_path, capsys):
    path, uid, ids = _database(tmp_path)
    before = db.get_activity(uid, ids[0], path=str(path))
    digest_before = ftp_backfill._file_digest(str(path))
    state = tmp_path / "progress.json"
    backup = tmp_path / "before.db"

    assert ftp_backfill.main(
        ["--db", str(path), "--state", str(state), "--backup", str(backup)]
    ) == 0

    assert db.get_activity(uid, ids[0], path=str(path)) == before
    assert ftp_backfill._file_digest(str(path)) == digest_before
    assert not state.exists()
    assert not backup.exists()
    assert "dry-run" in capsys.readouterr().out.lower()


def test_write_preserves_manual_power_correction_and_rebases_its_scores(tmp_path):
    path, uid, ids = _database(tmp_path)
    correction_id = db.apply_power_correction(
        uid, ids[0], 0, 0, 200.0, "bad sample",
        {"avg_power": 262.0, "np": 262.0, "if_": 1.31, "tss": 170.0},
        path=str(path),
    )
    assert correction_id is not None
    state = tmp_path / "progress.json"
    backup = tmp_path / "before.db"

    assert ftp_backfill.main(_args(path, state, backup)) == 0

    corrected = db.get_activity(uid, ids[0], path=str(path))
    assert corrected["avg_power"] == pytest.approx(262.0)
    assert corrected["np"] == pytest.approx(262.0)
    assert corrected["if_"] == pytest.approx(262.0 / FTP, abs=0.002)
    assert corrected["tss"] == pytest.approx(
        training_stress_score(DURATION, 262.0, FTP), abs=0.2
    )
    assert backup.exists()


def test_interrupted_write_resumes_from_checkpoint_without_skipping(tmp_path):
    path, uid, ids = _database(tmp_path, rows=2)
    state = tmp_path / "progress.json"
    backup = tmp_path / "before.db"
    _, expected_tss = _correct()

    ftp_backfill.run(
        str(path), write=True, state_path=str(state), backup_path=str(backup),
        chunk_size=1, stop_after_chunks=1,
    )
    checkpoint = json.loads(state.read_text())
    assert checkpoint["users"][str(uid)] == ids[0]
    assert db.get_activity(uid, ids[0], path=str(path))["tss"] == pytest.approx(
        expected_tss
    )
    assert db.get_activity(uid, ids[1], path=str(path))["tss"] == pytest.approx(STALE_TSS)

    ftp_backfill.run(
        str(path), write=True, state_path=str(state), backup_path=str(backup),
        chunk_size=1,
    )
    assert db.get_activity(uid, ids[1], path=str(path))["tss"] == pytest.approx(
        expected_tss
    )


def test_backfill_does_not_rebase_activity_without_a_valid_date(tmp_path):
    path, uid, ids = _database(tmp_path)
    db.add_ftp_entry(uid, "2026-08-02", 300.0, "manual", path=str(path))
    _set(path, uid, ids[0], start_time="not-a-date")

    assert ftp_backfill.main(["--db", str(path), "--write"]) == 0

    row = db.get_activity(uid, ids[0], path=str(path))
    assert row["if_"] == pytest.approx(STALE_IF)
    assert row["tss"] == pytest.approx(STALE_TSS)


# --- GUARD: one population per statistic -------------------------------------

def test_delta_statistics_describe_only_the_rows_that_change(tmp_path):
    """GUARD (defect 1): deltas fired for every scored row, not changed ones.

    Pre-fix, ``deltas.append()`` ran for every row it scored, so the reported
    min/median/max covered ~17,211 rows while "rows affected" counted ~15,476 -
    two different populations printed side by side.
    """
    path, uid, ids = _database(tmp_path, rows=4)
    correct_if, correct_tss = _correct()
    # Three of the four rows are already right; only one can change.
    for activity_id in ids[1:]:
        _set(path, uid, activity_id, if_=correct_if, tss=correct_tss)

    result = ftp_backfill.run(str(path))

    assert result["totals"]["rows_seen"] == 4
    assert result["totals"]["rows_changed"] == 1
    assert result["totals"]["rows_unchanged"] == 3
    ordinary = result["populations"]["ordinary"]["tss_delta"]
    assert ordinary["n"] == result["totals"]["rows_changed"]
    assert ordinary["median"] == pytest.approx(correct_tss - STALE_TSS)


def test_a_clean_database_reports_no_deltas_rather_than_zero(tmp_path, capsys):
    """GUARD (defect 1): a clean run printed "0 rows affected" beside 0.0/0.0/0.0.

    Zero is a real delta value; "nothing changed" must not be reported as if a
    population of rows had all moved by zero.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    correct_if, correct_tss = _correct()
    for activity_id in ids:
        _set(path, uid, activity_id, if_=correct_if, tss=correct_tss)

    result = ftp_backfill.run(str(path))

    assert result["totals"]["rows_changed"] == 0
    for name in ftp_backfill.POPULATIONS:
        distribution = result["populations"][name]["tss_delta"]
        assert distribution["n"] == 0
        assert distribution["min"] is None
        assert distribution["median"] is None
        assert distribution["max"] is None


# --- GUARD: the backup is identified by content, not by shape ----------------

def test_a_foreign_database_at_the_backup_path_is_refused(tmp_path, capsys):
    """GUARD (defect 2): row-count equality was the whole identity check.

    Pre-fix, a same-shape database with ``tss = 999`` everywhere was accepted
    as "the backup"; the tool then rewrote every row and reported that file as
    the rider's safety net.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    backup = tmp_path / "before.db"
    shutil.copy(str(path), str(backup))
    conn = sqlite3.connect(str(backup))
    conn.execute("UPDATE activities SET tss = 999.0")
    conn.commit()
    conn.close()
    original = db.get_activity(uid, ids[0], path=str(path))

    assert ftp_backfill.main(
        _args(path, tmp_path / "progress.json", backup)
    ) == 2

    assert "refusing to reuse" in capsys.readouterr().err.lower()
    assert db.get_activity(uid, ids[0], path=str(path)) == original
    kept = sqlite3.connect(str(backup))
    assert kept.execute("SELECT DISTINCT tss FROM activities").fetchall() == [(999.0,)]
    kept.close()


def test_a_resumed_run_refuses_a_backup_that_changed_underneath_it(tmp_path):
    """GUARD (defect 2): nothing tied the backup file to the run that made it.

    The progress file records the digest of the backup this run created, so a
    replaced or edited file cannot pass as the point the repair can be undone
    to.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    state = tmp_path / "progress.json"
    backup = tmp_path / "before.db"
    ftp_backfill.run(
        str(path), write=True, state_path=str(state), backup_path=str(backup),
        chunk_size=1, stop_after_chunks=1,
    )
    conn = sqlite3.connect(str(backup))
    conn.execute("UPDATE activities SET tss = 999.0")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="does not match"):
        ftp_backfill.run(
            str(path), write=True, state_path=str(state),
            backup_path=str(backup), chunk_size=1,
        )


# --- GUARD: the progress file does not outlive the run -----------------------

def test_progress_file_is_cleared_when_the_run_completes(tmp_path):
    """GUARD (defect 3): the checkpoint survived completion.

    A leftover progress file makes the next run a no-op that exits 0.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    state = tmp_path / "progress.json"

    assert ftp_backfill.main(_args(path, state, tmp_path / "before.db")) == 0

    assert not state.exists()


def test_restoring_the_backup_and_rerunning_repairs_the_database_again(tmp_path):
    """GUARD (defect 3): restoring from the tool's own backup left a no-op.

    Pre-fix the stale progress file made the second run report "0 rows
    affected" and exit 0 while every row was still wrong.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    state = tmp_path / "progress.json"
    backup = tmp_path / "before.db"
    _, expected_tss = _correct()

    assert ftp_backfill.main(_args(path, state, backup)) == 0
    assert db.get_activity(uid, ids[0], path=str(path))["tss"] == pytest.approx(
        expected_tss
    )

    shutil.copy(str(backup), str(path))
    assert db.get_activity(uid, ids[0], path=str(path))["tss"] == pytest.approx(STALE_TSS)

    result = ftp_backfill.run(
        str(path), write=True, state_path=str(state),
        backup_path=str(tmp_path / "before-2.db"),
    )

    assert result["totals"]["rows_changed"] == 2
    assert db.get_activity(uid, ids[0], path=str(path))["tss"] == pytest.approx(
        expected_tss
    )


def test_a_second_complete_run_is_a_genuine_no_op(tmp_path):
    """GUARD (defect 4): the old no-op test only proved the state short-circuit.

    With the progress file cleared on completion, the second run re-reads and
    re-scores every row, so "0 rows changed" now means the repair is
    idempotent rather than that it was skipped.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    state = tmp_path / "progress.json"

    assert ftp_backfill.main(_args(path, state, tmp_path / "before.db")) == 0
    assert not state.exists()
    after_first = [db.get_activity(uid, i, path=str(path)) for i in ids]

    result = ftp_backfill.run(
        str(path), write=True, state_path=str(state),
        backup_path=str(tmp_path / "before-2.db"),
    )

    assert result["totals"]["rows_seen"] == 2
    assert result["totals"]["rows_changed"] == 0
    assert [db.get_activity(uid, i, path=str(path)) for i in ids] == after_first


# --- GUARD: skipped rows are announced ---------------------------------------

def test_skipped_rows_are_reported_rather_than_silently_left_behind(tmp_path, capsys):
    """GUARD (defect 5): 20 rows were skipped with no warning at all.

    They keep their old values beside rescored neighbours, so the run has to
    say which ones.
    """
    path, uid, ids = _database(tmp_path, rows=3)
    _set(path, uid, ids[1], start_time=None)

    assert ftp_backfill.main(["--db", str(path)]) == 0

    out = capsys.readouterr().out
    assert "skipped 1" in out
    assert "WARNING" in out
    assert str(ids[1]) in out
    result = ftp_backfill.run(str(path))
    assert result["totals"]["rows_skipped"] == 1
    assert result["skips"] == [
        {"user_id": uid, "reason": ftp_backfill._SKIP_NO_DATE, "ids": [ids[1]]}
    ]


# --- GUARD: the report separates the populations the decision depends on -----

def test_corrupt_rows_are_split_out_by_implied_basis_not_by_tss(tmp_path):
    """GUARD (new): the #60 rows must not swamp the headline distribution.

    ``tss > 1000`` undercounts - a short ride scored against 0.6 W lands below
    it. The implied basis ``np / if_`` catches it regardless of duration.
    """
    path, uid, ids = _database(tmp_path, rows=3)
    # Scored against 0.64 W but short enough that its TSS stays under 1000.
    _set(path, uid, ids[0], if_=411.0, tss=900.0, duration_s=6)
    # Scored against a plausible basis, but at an impossible intensity.
    _set(path, uid, ids[1], if_=3.5, tss=1500.0)

    result = ftp_backfill.run(str(path))

    assert result["populations"]["corrupt"]["rows"] == 1
    assert result["populations"]["suspect"]["rows"] == 1
    assert result["populations"]["ordinary"]["rows"] == 1
    # The headline distribution is the ordinary one and is unpolluted by both.
    ordinary = result["populations"]["ordinary"]["tss_delta"]
    assert ordinary["min"] == ordinary["max"]
    assert ordinary["min"] == pytest.approx(_correct()[1] - STALE_TSS)


def test_load_shift_excludes_the_corrupt_rows_from_both_sides(tmp_path):
    """GUARD (new): a 1.6e7 TSS row produced a -33,091 CTL figure.

    Leaving it on the "before" side reports a shift that describes the
    corruption, not what the repair does to real rides.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    _set(path, uid, ids[0], if_=411.0, tss=16_136_334.9)

    result = ftp_backfill.run(str(path))

    assert abs(result["load_shift"]["ctl"]) < 100.0
    assert result["populations"]["corrupt"]["rows"] == 1


def test_rows_predating_ftp_history_are_counted_separately(tmp_path):
    """GUARD (new): 98.8% of the database is rescored on a back-applied FTP.

    That is the actual scoring rule for almost every row and belongs in the
    headline, not in a footnote.
    """
    path, uid, ids = _database(tmp_path, rows=2, ftp_date="2026-08-01")
    _set(path, uid, ids[0], start_time="2021-02-21T13:45:40")

    result = ftp_backfill.run(str(path))

    assert result["totals"]["pre_history_rows"] == 1
    assert result["totals"]["pre_history_pct"] == pytest.approx(50.0)
    assert result["per_user"][0]["first_ftp_date"] == "2026-08-01"


def test_report_names_the_direction_of_the_median_change(tmp_path, capsys):
    """GUARD (new): the owner approved a backfill believing TSS would go down.

    On this data most users' recorded FTP is below the 200 W default their
    rides were scored against, so TSS goes up.
    """
    path, uid, ids = _database(tmp_path, rows=2, ftp=150.0)

    assert ftp_backfill.main(["--db", str(path)]) == 0

    out = capsys.readouterr().out
    assert "stored TSS goes UP" in out


def test_a_sub_floor_ftp_history_entry_uses_the_selected_database(
    tmp_path, capsys
):
    path, uid, ids = _database(tmp_path)
    db.add_ftp_entry(uid, "2020-01-01", 40.0, "manual", path=str(path))

    assert ftp_backfill.main(["--db", str(path)]) == 0
    assert "refusing to run" not in capsys.readouterr().err.lower()


def test_a_dry_run_leaves_the_database_byte_identical(tmp_path):
    """GUARD (new): the report must be provably read-only."""
    path, uid, ids = _database(tmp_path, rows=3)
    digest = ftp_backfill._file_digest(str(path))

    ftp_backfill.run(str(path))

    assert ftp_backfill._file_digest(str(path)) == digest


def test_only_repairs_the_named_populations_and_leaves_the_rest_stored(tmp_path):
    """The owner's decision was 'repair the broken rows, leave the stale ones'.

    The corrupt and suspect rows are wrong under any FTP assumption. The
    ordinary rows are merely stale - rescoring them swaps one debatable basis
    for another and moves most rides UP, which is not what a repair was wanted
    for. ``--only`` must therefore withhold the write, not merely relabel it.
    """
    path, uid, ids = _database(tmp_path, rows=3)
    _set(path, uid, ids[0], if_=411.0, tss=900.0, duration_s=6)   # corrupt
    _set(path, uid, ids[1], if_=3.5, tss=1500.0)                  # suspect
    state = tmp_path / "progress.json"
    backup = tmp_path / "before.db"

    result = ftp_backfill.run(
        str(path), write=True, state_path=str(state), backup_path=str(backup),
        only=("corrupt", "suspect"),
    )

    assert result["repaired_populations"] == ["corrupt", "suspect"]
    # The ordinary row was examined and reported...
    assert result["populations"]["ordinary"]["rows"] == 1
    assert result["withheld"] == {"ordinary": 1}
    # ...but its stored value is untouched, while the broken two were repaired.
    assert db.get_activity(uid, ids[2], path=str(path))["tss"] == pytest.approx(
        STALE_TSS
    )
    assert db.get_activity(uid, ids[0], path=str(path))["tss"] != pytest.approx(900.0)
    assert db.get_activity(uid, ids[1], path=str(path))["tss"] != pytest.approx(1500.0)


def test_only_still_reports_every_population(tmp_path, capsys):
    """GUARD: narrowing the repair must not narrow the picture of what is wrong.

    A tool that hid the rows it declined to touch would let the next reader
    conclude the database is healthier than it is.
    """
    path, uid, ids = _database(tmp_path, rows=2)
    _set(path, uid, ids[0], if_=411.0, tss=900.0, duration_s=6)

    result = ftp_backfill.run(str(path), only=("corrupt",))
    ftp_backfill._print_report(result)
    out = capsys.readouterr().out

    assert result["populations"]["ordinary"]["rows"] == 1
    assert "SCOPE: rewriting only corrupt" in out
    assert "NOT rewritten" in out


def test_only_rejects_an_unknown_population(tmp_path):
    path, _uid, _ids = _database(tmp_path)
    with pytest.raises(ValueError, match="unknown population"):
        ftp_backfill.run(str(path), only=("typo",))
    with pytest.raises(ValueError, match="at least one"):
        ftp_backfill.run(str(path), only=())
