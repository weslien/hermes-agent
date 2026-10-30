"""Retention bounds for ``kanban gc``: a negative window builds a future cutoff
that matches every row; zero disables the sweep rather than deleting all."""
import argparse
import json
import os
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path

def _done_task_with_old_event(conn):
    tid = kb.create_task(conn, title="finished")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=0 WHERE task_id=?", (tid,))
    return tid

def _event_rows(conn, tid):
    return conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id=?", (tid,)
    ).fetchone()[0]

def _old_log_file() -> Path:
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "worker-1.log"
    p.write_text("log line")
    os.utime(p, (0, 0))
    return p

def _args(event_days=30, log_days=30, done_ws_days=1, dry_run=False):
    return argparse.Namespace(event_retention_days=event_days,
                              log_retention_days=log_days,
                              done_workspace_retention_days=done_ws_days,
                              dry_run=dry_run)

def test_gc_events_rejects_negative_window(board):
    with kbc.connect_closing() as conn:
        tid = _done_task_with_old_event(conn)
        with pytest.raises(ValueError, match="older_than_seconds"):
            kb.gc_events(conn, older_than_seconds=-86400)
        assert _event_rows(conn, tid) > 0

def test_gc_worker_logs_rejects_negative_window(board):
    log = _old_log_file()
    with pytest.raises(ValueError, match="older_than_seconds"):
        kb.gc_worker_logs(older_than_seconds=-86400)
    assert log.exists()

def _archived_scratch_workspace(conn) -> Path:
    tid = kb.create_task(conn, title="archived with workspace")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch' WHERE id=?",
            (tid,),
        )
    ws = kb.workspaces_root() / tid
    ws.mkdir(parents=True)
    (ws / "scratch.txt").write_text("keep me")
    return ws

@pytest.mark.parametrize(
    ("days", "expect_rc", "expect_kept"),
    [
        pytest.param(-1, 2, True, id="negative-refuses"),
        pytest.param(0, 0, True, id="zero-disables"),
        pytest.param(30, 0, False, id="positive-collects"),
    ],
)
def test_cmd_gc_retention_bounds(board, days, expect_rc, expect_kept):
    """Invalid retention must refuse before ANY sweep: the (unconditional)
    workspace collection runs first in the command body, so the archived
    scratch workspace surviving the negative case proves ordering, not just
    event/log preservation. Valid values (0 or positive) let it run."""
    with kbc.connect_closing() as conn:
        tid = _done_task_with_old_event(conn)
        ws = _archived_scratch_workspace(conn)
    log = _old_log_file()
    assert kanban_ops._cmd_gc(_args(event_days=days, log_days=days)) == expect_rc
    with kbc.connect_closing() as conn:
        assert (_event_rows(conn, tid) > 0) is expect_kept
    assert log.exists() is expect_kept
    assert (ws / "scratch.txt").exists() is (expect_rc != 0)

@pytest.mark.parametrize(
    ("days", "expected"),
    [
        pytest.param("-1", "must be >= 0", id="negative-blocked"),
        pytest.param("0", "GC complete", id="zero-disables"),
    ],
)
def test_slash_kanban_gc_retention_bounds(board, days, expected):
    """``/kanban gc`` from a chat session uses the same argparse type as the
    shell command: ``-1`` is rejected by the parser type before ``_cmd_gc``
    runs (usage error); ``0`` parses, reaches ``_cmd_gc``, and disables the
    sweep."""
    from hermes_cli import kanban
    with kbc.connect_closing() as conn:
        tid = _done_task_with_old_event(conn)
    log = _old_log_file()
    out = kanban.run_slash(f"gc --event-retention-days {days} --log-retention-days {days}")
    assert expected in out
    with kbc.connect_closing() as conn:
        assert _event_rows(conn, tid) > 0
    assert log.exists()


def test_cmd_gc_never_removes_the_workspaces_root_itself(board):
    # A scratch task whose workspace_path is the managed root itself (reachable via
    # kanban_create) must never make gc wipe every other task's scratch dir.
    root = kb.workspaces_root()
    sibling = root / "t_other"
    sibling.mkdir(parents=True)
    (sibling / "work.txt").write_text("another task's scratch", encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="archived scratch")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='archived', workspace_kind='scratch', "
                "workspace_path=? WHERE id=?",
                (str(root), tid),
            )
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (sibling / "work.txt").exists()


def _shared_scratch(conn) -> tuple[str, str, Path]:
    archived = kb.create_task(conn, title="archived sharer")
    live = kb.create_task(conn, title="live sharer")
    shared = kb.workspaces_root() / "shared-scratch"
    shared.mkdir(parents=True)
    (shared / "note.txt").write_text("still needed", encoding="utf-8")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(shared), archived),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(shared), live),
        )
    return archived, live, shared


def _deferred_reasons(conn, task_id: str) -> list:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, "workspace_cleanup_deferred_shared"),
    ).fetchall()
    return [json.loads(r["payload"])["reason"] for r in rows]


def test_cmd_gc_keeps_a_scratch_dir_a_live_task_still_uses(board):
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect_closing() as conn:
        archived, _live, shared = _shared_scratch(conn)
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (shared / "note.txt").read_text(encoding="utf-8") == "still needed"
    with kbc.connect_closing() as conn:
        assert _deferred_reasons(conn, archived) == ["shared"]
    # Once the dir is gone there is nothing to defer: no scan, no new event.
    shutil.rmtree(shared)
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
        assert _deferred_reasons(conn, archived) == ["shared"]


def test_cleanup_workspace_keeps_scratch_when_other_boards_cannot_be_listed(board, monkeypatch):
    """A named board's live task uses the dir; if boards cannot be listed, refuse the delete."""
    from hermes_cli import kanban_db_workspace as kbw
    kb.create_board("other")
    shared = kb.workspaces_root() / "cross-board"
    shared.mkdir(parents=True)
    (shared / "note.txt").write_text("still needed", encoding="utf-8")
    with kbc.connect_closing() as conn:
        archived = kb.create_task(conn, title="archived on default")
    with kbc.connect_closing(board="other") as other:
        live = kb.create_task(other, title="live on other")
    for board_slug, task_id, status in ((None, archived, "archived"), ("other", live, "ready")):
        with kbc.connect_closing(board=board_slug) as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status=?, workspace_kind='scratch', workspace_path=? WHERE id=?",
                (status, str(shared), task_id),
            )
    # Real scan: the other board's live task holds the dir, under a different
    # id and under the very id being cleaned up (boards mint ids independently).
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
    assert (shared / "note.txt").exists()
    with kbc.connect_closing(board="other") as other, kb.write_txn(other):
        other.execute("UPDATE tasks SET id=? WHERE id=?", (archived, live))
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
        assert _deferred_reasons(conn, archived) == ["shared", "shared"]
    assert (shared / "note.txt").exists()

    # Nobody holds it any more, but the boards cannot be listed: fail closed.
    with kbc.connect_closing(board="other") as other, kb.write_txn(other):
        other.execute("UPDATE tasks SET status='done' WHERE id=?", (archived,))
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "boards":
            raise PermissionError("boards dir unreadable")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with kbc.connect_closing() as conn:
        kbw._cleanup_workspace(conn, archived)
        assert _deferred_reasons(conn, archived) == ["shared", "shared", "unknown"]
    assert (shared / "note.txt").read_text(encoding="utf-8") == "still needed"


def _done_scratch_workspace(conn, *, completed_at: int, title="done with workspace") -> Path:
    tid = kb.create_task(conn, title=title)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, workspace_kind='scratch' WHERE id=?",
            (completed_at, tid),
        )
    ws = kb.workspaces_root() / tid
    ws.mkdir(parents=True)
    (ws / "done.txt").write_text("candidate", encoding="utf-8")
    return ws


def test_cmd_gc_done_workspace_retention_bounds(board):
    import time as _time

    with kbc.connect_closing() as conn:
        old = _done_scratch_workspace(conn, completed_at=0)
        fresh = _done_scratch_workspace(conn, completed_at=int(_time.time()))
        # A done row with no completion stamp must never match the cutoff.
        undated = kb.create_task(conn, title="done, no completion stamp")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', workspace_kind='scratch' WHERE id=?",
                (undated,),
            )
        undated_ws = kb.workspaces_root() / undated
        undated_ws.mkdir(parents=True)
        (undated_ws / "undated.txt").write_text("no stamp", encoding="utf-8")
        open_tid = kb.create_task(conn, title="still open")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', workspace_kind='scratch' WHERE id=?",
                (open_tid,),
            )
        open_ws = kb.workspaces_root() / open_tid
        open_ws.mkdir(parents=True)
        (open_ws / "live.txt").write_text("in use", encoding="utf-8")
    # 0 disables the done sweep: every workspace survives.
    assert kanban_ops._cmd_gc(_args(done_ws_days=0)) == 0
    assert old.is_dir() and fresh.is_dir() and undated_ws.is_dir() and open_ws.is_dir()
    # 1 day: only the done workspace past the grace window goes; a done row with
    # no completed_at stamp never matches the cutoff, and open cards are untouchable.
    assert kanban_ops._cmd_gc(_args(done_ws_days=1)) == 0
    assert not old.exists() and fresh.is_dir() and undated_ws.is_dir() and open_ws.is_dir()


def test_cmd_gc_defers_a_done_workspace_with_active_children(board):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="done parent")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=0, workspace_kind='scratch' WHERE id=?",
                (parent,),
            )
        ws = kb.workspaces_root() / parent
        ws.mkdir(parents=True)
        (ws / "handoff.md").write_text("child still needs this", encoding="utf-8")
        child = kb.create_task(conn, title="active child")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (ws / "handoff.md").exists()
    # Every child terminal: nothing reads the handoff any more, the old workspace goes.
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done', completed_at=0 WHERE id=?", (child,))
    assert kanban_ops._cmd_gc(_args()) == 0
    assert not ws.exists()


def test_cmd_gc_dry_run_lists_and_removes_nothing(board, capsys):
    with kbc.connect_closing() as conn:
        done = _done_scratch_workspace(conn, completed_at=0)
        arch = _archived_scratch_workspace(conn)
        tid = _done_task_with_old_event(conn)
        # A worktree-kind row whose path is not a linked worktree: the real
        # pass preserves it, so the preview must not list it either.
        fake_wt = kb.create_task(conn, title="done, not a worktree")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=0, workspace_kind='worktree', "
                "workspace_path=? WHERE id=?",
                (str(kb.workspaces_root() / "fake-worktree-dir"), fake_wt),
            )
        fake_dir = kb.workspaces_root() / "fake-worktree-dir"
        fake_dir.mkdir(parents=True)
        (fake_dir / "marker.txt").write_text("not a git worktree", encoding="utf-8")
    log = _old_log_file()

    assert kanban_ops._cmd_gc(_args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "would remove workspace of" in out
    assert done.name in out and arch.name in out
    assert fake_dir.name not in out
    assert done.is_dir() and arch.is_dir() and fake_dir.is_dir() and log.exists()
    with kbc.connect_closing() as conn:
        assert _event_rows(conn, tid) > 0

    # The same state is really collected: workspaces, events, and the old log.
    assert kanban_ops._cmd_gc(_args()) == 0
    assert not done.exists() and not arch.exists() and fake_dir.is_dir() and not log.exists()
    with kbc.connect_closing() as conn:
        assert _event_rows(conn, tid) == 0


def test_gc_terminal_workspaces_done_requires_an_age_bound(board):
    """Without ``completed_before`` a done sweep would collect every done card's
    workspace regardless of age; the function must refuse that call shape."""
    from hermes_cli import kanban_db_workspace as kbw

    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kbw.gc_terminal_workspaces(conn, status="done")
        # The archived sweep keeps its no-bound shape (every archived card).
        assert kbw.gc_terminal_workspaces(conn, status="archived") == []


def test_cmd_gc_dry_run_skips_a_worktree_it_cannot_evaluate(board, monkeypatch, capsys):
    """A candidate worktree the preserve predicate cannot even evaluate must
    skip its row, not abort the preview; the real pass is armed the same way."""
    from hermes_cli import kanban_db_workspace as kbw

    def _raise(wp):
        raise OSError("unreadable")

    monkeypatch.setattr(kbw, "_worktree_preserve_reason", _raise)
    with kbc.connect_closing() as conn:
        done = _done_scratch_workspace(conn, completed_at=0)
        wt_task = kb.create_task(conn, title="done with worktree")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=0, workspace_kind='worktree', "
                "workspace_path=? WHERE id=?",
                (str(kb.workspaces_root() / "opaque-worktree"), wt_task),
            )
        (kb.workspaces_root() / "opaque-worktree").mkdir(parents=True)

    assert kanban_ops._cmd_gc(_args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert done.name in out                        # scratch rows still previewed
    assert "opaque-worktree" not in out            # unreadable candidate skipped, not listed
    assert (kb.workspaces_root() / "opaque-worktree").is_dir()

    # The real pass is armored the same way: the unreadable worktree survives it.
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (kb.workspaces_root() / "opaque-worktree").is_dir()
    assert not done.exists()


def test_done_workspace_retention_config_default_and_reader(board):
    """The knob is registered (``DEFAULT_CONFIG``) and the notifier's reader
    honours a user config.yaml through the same loader the watcher uses."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from gateway.kanban_watchers_common import _done_workspace_retention_days

    default_days = DEFAULT_CONFIG["kanban"]["done_workspace_retention_days"]
    assert isinstance(default_days, int) and default_days >= 0
    # Nothing configured: the reader returns the shipped default.
    assert _done_workspace_retention_days() == default_days
    # A user-set config reaches the sweep through the real loader.
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "kanban:\n  done_workspace_retention_days: 5\n", encoding="utf-8")
    assert _done_workspace_retention_days() == 5
    # A negative value would build a future cutoff and sweep every done card
    # at once; the reader must clamp it to "off" instead of trusting it.
    (home / "config.yaml").write_text(
        "kanban:\n  done_workspace_retention_days: -3\n", encoding="utf-8")
    assert _done_workspace_retention_days() == 0
