"""Auto-release capability blocks when their trigger card completes.

Kernel-side of the fix for: workers cannot unblock a capability block;
only the orchestrator CLI ``hermes kanban unblock`` can.  Instead of
granting workers a new release verb, the kernel auto-releases blocks
whose ``trigger_card_id`` points to a card that just completed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", parents=()):
    tid = kb.create_task(conn, title=title, assignee="worker")
    for parent in parents:
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Positive: capability block auto-releases on trigger completion
# ---------------------------------------------------------------------------


class TestCapabilityBlockAutoRelease:
    def test_capability_block_releases_when_trigger_completes(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            blocked = _running_task(conn, title="blocked")
            reason = f"trigger: {trigger}\nwaiting for completion"
            kb.block_task(conn, blocked, reason=reason, kind="capability")
            assert kb.get_task(conn, blocked).status == "blocked"
            assert kb.get_task(conn, blocked).trigger_card_id == trigger
            # trigger completes -> blocked is auto-released to ready
            result = kb.complete_task(conn, trigger, result="done")
            assert result is True
            after = kb.get_task(conn, blocked)
            assert after.status == "ready"
            assert after.trigger_card_id is None
            # unblock event is recorded
            unblocks = [e for e in kb.list_events(conn, blocked) if e.kind == "unblocked"]
            assert unblocks, "expected an unblocked event"

    def test_capability_block_with_open_trigger_stays_blocked(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            other = _running_task(conn, title="other")
            blocked = _running_task(conn, title="blocked")
            reason = f"trigger: {trigger}\nwaiting"
            kb.block_task(conn, blocked, reason=reason, kind="capability")
            # completing a *different* card does not release
            kb.complete_task(conn, other, result="other done")
            assert kb.get_task(conn, blocked).status == "blocked"

    def test_capability_block_without_trigger_stays_blocked(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            other = _running_task(conn, title="other")
            blocked = _running_task(conn, title="blocked")
            kb.block_task(conn, blocked, reason="no trigger line here", kind="capability")
            assert kb.get_task(conn, blocked).status == "blocked"
            # completing any card does not release
            kb.complete_task(conn, other, result="other done")
            assert kb.get_task(conn, blocked).status == "blocked"


# ---------------------------------------------------------------------------
# Negative: other block kinds never auto-release
# ---------------------------------------------------------------------------


class TestOtherKindsNeverAutoRelease:
    def test_needs_input_never_releases_on_trigger_complete(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            blocked = _running_task(conn, title="blocked")
            reason = f"trigger: {trigger}\nhuman decision needed"
            kb.block_task(conn, blocked, reason=reason, kind="needs_input")
            assert kb.get_task(conn, blocked).status == "blocked"
            kb.complete_task(conn, trigger, result="done")
            assert kb.get_task(conn, blocked).status == "blocked"

    def test_dependency_never_releases_on_trigger_complete(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            parent = kb.create_task(conn, title="parent", assignee="worker")
            blocked = _running_task(conn, title="blocked")
            # Link blocked to an open parent so dependency waits in todo.
            # A running child can only be linked retroactively with the run_id.
            kb.link_tasks(
                conn, parent_id=parent, child_id=blocked,
                expected_child_run_id=kb.get_task(conn, blocked).current_run_id,
            )
            reason = f"trigger: {trigger}\nwait for parent"
            kb.block_task(conn, blocked, reason=reason, kind="dependency")
            # dependency goes to todo, not blocked
            assert kb.get_task(conn, blocked).status == "todo"
            # completing trigger does not change it
            kb.complete_task(conn, trigger, result="done")
            assert kb.get_task(conn, blocked).status == "todo"

    def test_transient_never_releases_on_trigger_complete(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            blocked = _running_task(conn, title="blocked")
            reason = f"trigger: {trigger}\nflaky failure"
            kb.block_task(conn, blocked, reason=reason, kind="transient")
            assert kb.get_task(conn, blocked).status == "blocked"
            kb.complete_task(conn, trigger, result="done")
            assert kb.get_task(conn, blocked).status == "blocked"


# ---------------------------------------------------------------------------
# Trigger extraction from reason
# ---------------------------------------------------------------------------


class TestTriggerExtraction:
    def test_triggers_parsed_from_reason_line(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            blocked = _running_task(conn, title="blocked")
            kb.block_task(conn, blocked, reason=f"trigger: {trigger}", kind="capability")
            assert kb.get_task(conn, blocked).trigger_card_id == trigger

    def test_trigger_missing_when_reason_has_no_trigger(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            blocked = _running_task(conn, title="blocked")
            kb.block_task(conn, blocked, reason="just a reason", kind="capability")
            assert kb.get_task(conn, blocked).trigger_card_id is None

    def test_trigger_recorded_on_block_event(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            blocked = _running_task(conn, title="blocked")
            kb.block_task(conn, blocked, reason=f"trigger: {trigger}", kind="capability")
            events = [e for e in kb.list_events(conn, blocked) if e.kind == "blocked"]
            assert events
            assert events[-1].payload.get("trigger_card_id") == trigger


# ---------------------------------------------------------------------------
# Manual unblock clears trigger
# ---------------------------------------------------------------------------


class TestManualUnblockClearsTrigger:
    def test_unblock_clears_trigger_card_id(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger = _running_task(conn, title="trigger")
            blocked = _running_task(conn, title="blocked")
            kb.block_task(conn, blocked, reason=f"trigger: {trigger}", kind="capability")
            assert kb.get_task(conn, blocked).trigger_card_id == trigger
            kb.unblock_task(conn, blocked)
            assert kb.get_task(conn, blocked).trigger_card_id is None

    def test_different_triggers_release_independently(self, kanban_home: Path) -> None:
        with kbc.connect_closing() as conn:
            trigger_a = _running_task(conn, title="trigger_a")
            trigger_b = _running_task(conn, title="trigger_b")
            blocked_a = _running_task(conn, title="blocked_a")
            blocked_b = _running_task(conn, title="blocked_b")
            kb.block_task(conn, blocked_a, reason=f"trigger: {trigger_a}", kind="capability")
            kb.block_task(conn, blocked_b, reason=f"trigger: {trigger_b}", kind="capability")
            # complete trigger_a -> only blocked_a releases
            kb.complete_task(conn, trigger_a, result="done")
            assert kb.get_task(conn, blocked_a).status == "ready"
            assert kb.get_task(conn, blocked_b).status == "blocked"
            # complete trigger_b -> blocked_b releases
            kb.complete_task(conn, trigger_b, result="done")
            assert kb.get_task(conn, blocked_b).status == "ready"
