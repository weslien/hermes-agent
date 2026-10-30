"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


# --- GitLab MR acceptance: same gate, GitLab API v4 + the head pipeline as the required check ---

@pytest.fixture
def gitlab(tmp_path, monkeypatch):
    state = {"pipeline": "success", "head": "a" * 40, "reads": 0, "requests": [], "auth": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            state["reads"] += 1
            state["auth"] = self.headers.get("Authorization")
            if "/merge_requests/" not in self.path:
                self.send_error(404)
                return
            pipeline = None if state.get("missing") else {
                "id": 269000, "sha": state.get("pipeline_sha", state["head"]),
                "status": state["pipeline"], "ref": state.get("ref", "refs/merge-requests/1867/head"),
                "web_url": "https://gitlab.evroc.dev/engineering/think/-/pipelines/269000"}
            value = {"state": state.get("mr_state", "opened"),
                     "diff_refs": {"head_sha": state["head"]},
                     "head_pipeline": pipeline, "pipeline": pipeline}
            if state.get("head_change") and state["reads"] > 1:
                value["diff_refs"] = {"head_sha": "b" * 40}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GITLAB_HOST", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GITLAB_TOKEN", "gl-token")
    # A glab shim shadows any real glab: the gate's primary transport must
    # lend itself to a fixture exactly like gh. It dials the same local API as
    # the raw fallback and logs every endpoint the gate routed through it.
    shim = tmp_path / "bin"
    shim.mkdir()
    calls = tmp_path / "glab_calls"
    glab = shim / "glab"
    glab.write_text(f"#!{sys.executable}\nimport os, sys, urllib.request\n"
                    f"open(r'{calls}', 'a').write(sys.argv[2] + '\\n')\n"
                    "url = os.environ['GITLAB_HOST'] + '/api/v4/' + sys.argv[2]\n"
                    "print(urllib.request.urlopen(urllib.request.Request(url, "
                    "headers={'Authorization': 'Bearer ' + os.environ.get('GITLAB_TOKEN', '')}))"
                    ".read().decode())\n")
    glab.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    state["glab_calls"] = lambda: calls.read_text().splitlines() if calls.exists() else []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_mr_completion_requires_current_pipeline_evidence(gitlab):
    with connect() as conn:
        mr = "https://gitlab.evroc.dev/engineering/think/-/merge_requests/1867"
        for pipeline in ("failed", "running", "canceled", "skipped", "success"):
            gitlab.update(pipeline=pipeline, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="engineering/think")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": mr})
            assert ok is (pipeline == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipt = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
            assert receipt["head_sha"] == "a" * 40
            assert receipt["checks"][-1]["url"].endswith("/pipelines/269000")
            assert receipt["ok"] is ok
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipt["recovery"]
        assert gitlab["auth"] == "Bearer gl-token"
        assert gitlab["glab_calls"]()  # the reads above went through glab, not the raw fallback
        for fault, classification, key, value in (
            ("missing", "missing", "missing", True),
            ("stale_pipeline", "stale", "pipeline_sha", "b" * 40),
            ("head_change", "stale", "head_change", True),
            ("mr_closed", "infra", "mr_state", "closed"),
        ):
            gitlab.update(pipeline="success", head="a" * 40)
            gitlab[key] = value
            tid = kb.create_task(conn, title=fault, completion_contract="engineering/think")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": mr})
            receipt = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
            assert receipt["classification"] == classification
            if fault == "missing":
                assert "No pipeline" in receipt["detail"]
            assert kb.get_task(conn, tid).status != "done"
            gitlab.pop(key)
        # A sibling project and an omitted published_pr cannot satisfy the declaration,
        # and the mismatch is refused before any API read.
        before = len(gitlab["requests"])
        tid = kb.create_task(conn, title="sibling", completion_contract="engineering/think")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://gitlab.evroc.dev/other/repo/-/merge_requests/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert receipt["classification"] == "missing"
        assert receipt["detail"] == "Supply metadata.published_pr matching the persisted completion contract."
        assert len(gitlab["requests"]) == before
        assert not kb.complete_task(conn, tid, result="done")
        # A merged-result pipeline (temp merge ref, sha != source HEAD) must satisfy the gate.
        gitlab.update(pipeline="success", head="a" * 40)
        gitlab["pipeline_sha"] = "c" * 40
        gitlab["ref"] = "refs/merge-requests/1867/merge"
        tid = kb.create_task(conn, title="merged-result", completion_contract="engineering/think")
        assert kb.complete_task(conn, tid, result="done", metadata={"published_pr": mr})
        assert kb.get_task(conn, tid).status == "done"
        gitlab.pop("pipeline_sha")
        gitlab.pop("ref")
        # Nested groups declare the exact MR URL itself.
        nested = "https://gitlab.evroc.dev/group/sub/repo/-/merge_requests/12"
        gitlab.update(pipeline="success", head="a" * 40)
        tid = kb.create_task(conn, title="nested", completion_contract=nested)
        assert kb.complete_task(conn, tid, result="done", metadata={"published_pr": nested})
        assert kb.get_task(conn, tid).status == "done"
        # Local-only contracts never touch the network, MR URL in prose or not.
        before = len(gitlab["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary=f"{mr} is background context")
        assert len(gitlab["requests"]) == before


@pytest.mark.platforms("posix")
def test_mr_acceptance_without_a_token_is_auth_not_infra(tmp_path, monkeypatch):
    """No GITLAB_TOKEN in the completing scope classifies `auth` — the mirror of
    gh's exit-4 no-login path: whatever glab may try against the dead host, the
    raw fallback refuses to dial, so the host can never report `infra`."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_HOST", "http://127.0.0.1:1")
    project = "engineering/think"
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="tokenless", completion_contract=project)
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://gitlab.evroc.dev/engineering/think/-/merge_requests/1867"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "GITLAB_TOKEN" in receipt["detail"]


@pytest.mark.platforms("linux")
def test_mr_reads_fall_back_to_raw_rest_when_glab_is_missing_or_fails(gitlab, monkeypatch, tmp_path):
    """A missing glab and a failing glab both fall back to the raw API v4 read:
    the same evidence completes the card, and no glab runs."""
    absent = tmp_path / "absent-bin"
    broken = tmp_path / "broken-bin"
    absent.mkdir()
    broken.mkdir()
    (broken / "glab").write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    (broken / "glab").chmod(0o755)
    mr = "https://gitlab.evroc.dev/engineering/think/-/merge_requests/1867"
    with connect() as conn:
        for path_env in (str(absent), str(broken)):
            monkeypatch.setenv("PATH", path_env)
            gitlab.update(pipeline="success", head="a" * 40)
            before = len(gitlab["glab_calls"]())
            tid = kb.create_task(conn, title="fallback", completion_contract="engineering/think")
            assert kb.complete_task(conn, tid, result="done", metadata={"published_pr": mr})
            assert kb.get_task(conn, tid).status == "done"
            assert len(gitlab["glab_calls"]()) == before  # glab served no read: the raw fallback produced the evidence
            assert gitlab["auth"] == "Bearer gl-token"


@pytest.mark.platforms("posix")
def test_assigned_profile_without_gitlab_credentials_never_completes_as_the_ambient_glab(tmp_path, monkeypatch):
    """The glab mirror of the pinned gh config dir (#122689): a profile that declares
    no GitLab credentials of its own must not read as the launch user's glab login —
    glab runs against its own config dir, finds no login, and the raw fallback
    classifies auth."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_HOST", "http://127.0.0.1:1")  # the raw fallback must refuse before it dials
    env_dump = tmp_path / "glab_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    glab = shim / "glab"
    # glab semantics: a GLAB_CONFIG_DIR without a login means no credentials.
    glab.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                    f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                    "if 'GLAB_CONFIG_DIR' in os.environ and not pathlib.Path(os.environ['GLAB_CONFIG_DIR']).exists():\n"
                    "    sys.exit(1)\n"
                    "print(json.dumps({'state': 'opened', 'diff_refs': {'head_sha': 'a' * 40},\n"
                    " 'head_pipeline': {'id': 269000, 'sha': 'a' * 40, 'status': 'success',\n"
                    " 'ref': 'refs/merge-requests/1867/head', 'web_url': 'u'}}))\n")
    glab.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="engineering/think", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://gitlab.evroc.dev/engineering/think/-/merge_requests/1867"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "GITLAB_TOKEN" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GLAB_CONFIG_DIR"] == str(assignee_home / "glab")
    assert "GITLAB_TOKEN" not in captured
