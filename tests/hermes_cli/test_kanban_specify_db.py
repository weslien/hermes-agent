"""Tests for kb.specify_triage_task — the DB-layer atomic promotion
from the triage column to todo. LLM-free by design."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
    )


def test_specify_promotes_triage_to_todo(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="rough idea")
        assert kb.get_task(conn, tid).status == "triage"
    with kbc.connect() as conn:
        ok = kb.specify_triage_task(
            conn,
            tid,
            title="Refined: rough idea",
            body="**Goal**\nDo the thing.",
            author="specifier-bot",
        )
    assert ok is True
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    # No parents → recompute_ready should have flipped it past todo to ready.
    assert task.status == "ready"
    assert task.title == "Refined: rough idea"
    assert "**Goal**" in (task.body or "")


def test_specify_rejects_blank_title(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="rough")
    with kbc.connect() as conn, pytest.raises(ValueError):
        kb.specify_triage_task(conn, tid, title="   ", body="ok")


def test_specify_records_audit_comment_only_when_author_given(kanban_home):
    # With author → comment added.
    with kbc.connect() as conn:
        tid1 = _create_triage(conn, title="a")
        kb.specify_triage_task(
            conn, tid1, title="A-spec", body="b", author="ace"
        )
        comments1 = kb.list_comments(conn, tid1)
    assert len(comments1) == 1
    assert "Specified" in comments1[0].body
    assert comments1[0].author == "ace"

    # Without author → no comment (silent).
    with kbc.connect() as conn:
        tid2 = _create_triage(conn, title="b")
        kb.specify_triage_task(conn, tid2, title="B-spec", body="b")
        comments2 = kb.list_comments(conn, tid2)
    assert comments2 == []


def _mark_needs_input_block_loop(conn, tid, claim_age_seconds=None):
    """Stamp the block-loop fields plus the machine-stamped ``claimed``
    event the hourly recheck anchors to."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET block_kind = 'needs_input', "
            "block_recurrences = ? WHERE id = ?",
            (kb.BLOCK_RECURRENCE_LIMIT, tid),
        )
        if claim_age_seconds is not None:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'claimed', NULL, ?)",
                (tid, int(time.time()) - claim_age_seconds),
            )


def test_specify_refuses_promotion_while_hourly_recheck_hold_open(kanban_home):
    """The triage→todo transition refuses a needs_input block-loop card
    whose last machine-stamped claim is under an hour old (ESC-024 pick B):
    no actor promotes around the hold, so a faster re-claim is structurally
    impossible."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="waiting on a human")
        _mark_needs_input_block_loop(conn, tid, claim_age_seconds=60)

    with kbc.connect() as conn:
        ok = kb.specify_triage_task(conn, tid, title="Refined", body="b", author="ace")
    assert ok is False
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_specify_promotes_once_hourly_recheck_window_elapsed(kanban_home):
    """The hold releases when the machine-stamped hour is over: the card
    promotes in full (todo, then ready for a parent-free card)."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="waiting on a human")
        _mark_needs_input_block_loop(
            conn, tid, claim_age_seconds=kb.NEEDS_INPUT_RECHECK_SECONDS + 300,
        )

    with kbc.connect() as conn:
        ok = kb.specify_triage_task(conn, tid, title="Refined", body="b", author="ace")
    assert ok is True
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
