"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _mark_block_loop(tid, kind="needs_input", recurrences=None):
    """Stamp a card with the block-loop fields the hourly recheck reads."""
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = ? WHERE id = ?",
                (kind, recurrences if recurrences is not None else kb.BLOCK_RECURRENCE_LIMIT, tid),
            )


def _stamp_last_claim(tid, age_seconds):
    """Write the machine-stamped ``claimed`` event the hold anchors to,
    ``age_seconds`` ago."""
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'claimed', NULL, ?)",
                (tid, int(time.time()) - age_seconds),
            )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_children_inherit_root_assignee_when_unrouted(kanban_home):
    """Unrouted children fall back to the ROOT task's assignee, not
    the decomposer's active profile (#114294). The active profile here is ``private``
    (an incognito profile with no credentials), so the old fallback spawned
    workers that deadlocked on capability blockers."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="zdr", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    # get_active_profile_name() is mocked to names[0] = "private" — the
    # global default chain would resolve there without kanban.default_assignee.
    patches = _patch_list_profiles(["private", "zdr"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "zdr"
    assert c1.assignee == "zdr"
    # Same class for the root: no ``orchestrator_profile`` must not hand the
    # orchestration card to the dispatcher's own (here: incognito) profile.
    assert root.assignee == "zdr"


def test_decompose_explicit_default_assignee_wins_over_root_assignee(kanban_home):
    """An explicitly configured ``kanban.default_assignee`` stays
    authoritative for unroutable children; the root task's assignee only
    fills in when no explicit default is set (explicit config → card
    assignee → active profile)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="engineer", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["engineer", "docs", "private"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "docs"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "docs"
    assert c1.assignee == "docs"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_load_routing_falls_back_to_defaults_when_config_unreadable(kanban_home, monkeypatch):
    """decompose_task promises ok=False on expected failures; a config read that raises (missing
    profile home, HomeInitializationError) must not escape _load_routing as an exception."""
    from hermes_cli import config as config_mod

    def _boom():
        raise FileNotFoundError("profile home is gone")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    routing = decomp._load_routing()
    assert routing.default_assignee == "default" and routing.auto_promote is True


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False


def test_decompose_holds_needs_input_block_loop_subhour(kanban_home):
    """A needs_input block-loop card waits in triage while the hour since
    its last machine-stamped dispatcher claim runs short; the aux LLM is
    never called (ESC-024 pick B: at most one re-claim per hour)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)
    _mark_block_loop(tid)
    _stamp_last_claim(tid, 60)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "promote",
        "title": "Released",
        "body": "y",
    })
    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(llm_payload),
    ) as aux:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    aux.assert_not_called()
    assert outcome.ok is False
    assert "hourly needs_input recheck" in outcome.reason

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage"


def test_decompose_promotes_needs_input_block_loop_after_hour(kanban_home):
    """Once an hour or more passed since the last claim the card promotes
    again: the board re-runs the card once per hour, not never."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)
    _mark_block_loop(tid)
    _stamp_last_claim(tid, kb.NEEDS_INPUT_RECHECK_SECONDS + 300)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "promote",
        "title": "Released",
        "body": "y",
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is True
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in ("todo", "ready")
    assert task.status != "scheduled"


def test_decompose_allows_capability_block_loop(kanban_home):
    """Cards with block kinds other than ``needs_input`` keep flowing
    through the decomposer even past the recurrence limit."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = 'capability', block_recurrences = ? WHERE id = ?",
                (kb.BLOCK_RECURRENCE_LIMIT, tid),
            )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "promote",
        "title": "Released",
        "body": "y",
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is True
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status in ("todo", "ready")
    assert task.status != "scheduled"


def test_schedule_task_refuses_triage(kanban_home):
    """``schedule_task`` keeps rejecting triage cards: pick B has no
    park-in-``scheduled`` state, so hourly-recheck cards stay in triage
    visible with their open ask (option-A rule superseded)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)
        ok = kb.schedule_task(conn, tid, reason="test park")
    assert ok is False

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage"
