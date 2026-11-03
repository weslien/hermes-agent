"""Tests for decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kbc.connect() as conn:
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn)
        child_ids = decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kbc.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert comments
    assert any(ev.kind == "decomposed" for ev in events)


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


def test_decompose_refused_while_hourly_recheck_hold_open(kanban_home):
    """The fan-out exit respects the hourly recheck hold (ESC-024 pick B):
    a needs_input block-loop card under the machine-stamped hour fans out
    to nothing and keeps the same block-loop fields, so the second triage
    exit cannot promote around the hold either."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="waiting on a human")
        _mark_needs_input_block_loop(conn, tid, claim_age_seconds=60)

    with kbc.connect() as conn:
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is None

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert task.status == "triage"
    assert not any(ev.kind == "decomposed" for ev in events)


def test_decompose_allowed_once_hourly_recheck_window_elapsed(kanban_home):
    """The fan-out exit releases when the machine-stamped hour is over."""
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="waiting on a human")
        _mark_needs_input_block_loop(
            conn, tid, claim_age_seconds=kb.NEEDS_INPUT_RECHECK_SECONDS + 300,
        )

    with kbc.connect() as conn:
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None and len(child_ids) == 1

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "todo"
