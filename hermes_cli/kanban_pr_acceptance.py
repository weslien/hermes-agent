"""Exact-head acceptance for explicitly declared PR/MR tasks: GitHub PRs via
``gh``, GitLab MRs via ``glab`` (raw API v4 fallback), the assignee profile's own GitLab credentials.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
_MR = re.compile(r"https://gitlab\.evroc\.dev/((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)/-/merge_requests/([1-9][0-9]*)")


def _match_pr(url: str | None):
    """The PR or MR URL (GitHub or GitLab), wherever it came from: repo path and number captured."""
    return _PR.fullmatch(url or "") or _MR.fullmatch(url or "")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _match_pr(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, an exact GitHub PR URL, or an exact GitLab MR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _gl_api(endpoint: str, *, profile_home: str | None = None):
    """GitLab API v4 mirror of :func:`_api`: :func:`_gh_env` supplies the assignee
    profile's served env — its own GITLAB_TOKEN when it declares one, the completing
    process's otherwise. ``glab api`` reads the endpoint the way ``gh`` reads GitHub;
    the raw REST call is the fallback for a missing or failing glab. An assigned
    profile without GitLab credentials of its own is pinned to its own
    GLAB_CONFIG_DIR (#122689, the glab mirror of _gh_env's GH_CONFIG_DIR pin), so
    glab cannot read as the launch identity and a tokenless served env classifies
    `auth`, never `infra`. Unassigned cards read with the ambient identity, as gh
    does. Credentials stay in the environment, never argv, and only the HTTP code +
    endpoint are ever persisted."""
    env = _gh_env(profile_home) or os.environ
    host = env.get("GITLAB_HOST", "gitlab.evroc.dev")
    if profile_home and "GITLAB_TOKEN" not in env and "GLAB_CONFIG_DIR" not in env:
        # glab reads its config through HOME, which stays the launch process's: pin it
        # to a profile-owned dir so glab, like gh's exit-4, fails instead of borrowing
        # the launch user's glab login.
        env = {**env, "GLAB_CONFIG_DIR": str(Path(profile_home) / "glab")}
    try:
        # Pin GITLAB_HOST: glab's own default is gitlab.com, which would verify
        # evidence from a different GitLab than the fallback reads.
        result = subprocess.run(["glab", "api", endpoint], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30, check=True,
                                env={**env, "GITLAB_HOST": host})
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass  # glab missing or failed: the raw REST call below is the fallback.
    token = env.get("GITLAB_TOKEN")
    if not token:
        raise _GateAuthError(f"GITLAB_TOKEN is not set for {endpoint.split('?')[0]}")
    base = host if "://" in host else f"https://{host}"
    request = urllib.request.Request(f"{base}/api/v4/{endpoint}",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 401/403/404 = the token cannot see this project (wrong identity or missing
        # grant), not a transient API failure; the body stays out of any receipt.
        if exc.code in (401, 403, 404):
            raise _GateAuthError(f"HTTP {exc.code} on {endpoint.split('?')[0]}") from None
        raise


_GL_PIPELINE_STATUS = {
    "success": "success",
    "failed": "failure",
    "created": "pending", "waiting_for_resource": "pending", "preparing": "pending",
    "pending": "pending", "running": "pending", "scheduled": "pending",
    # canceled/skipped/manual mirror GitHub's dropped check-run conclusions: never acceptable.
    "canceled": "infra", "skipped": "infra", "manual": "infra",
}


def _collect_gitlab(match: re.Match, receipt: dict, *, profile_home: str | None):
    """GitLab MR read mirroring the GitHub checks: MR state, the head pipeline as the
    one required check, then a re-read — an old-head success is never transferable."""
    endpoint = f"projects/{quote(match[1], safe='')}/merge_requests/{match[2]}"
    mr = _gl_api(endpoint, profile_home=profile_home)
    sha = (mr.get("diff_refs") or {}).get("head_sha")
    receipt["head_sha"] = sha
    if mr.get("state") not in {"opened", "merged"} or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("MR is closed or current head is unavailable")
    pipeline = mr.get("head_pipeline") or mr.get("pipeline")
    if not pipeline:
        # No pipeline = no evidence, the exact mirror of GitHub's no-required-checks stop.
        receipt["detail"] = "No pipeline exists for the MR; explicitly use a local-only contract for non-CI tasks."
        return
    # shortcut: merged-result/merge pipelines run on a temp merge ref whose sha is NOT the
    # source HEAD by design; only a head-ref pipeline with a differing sha is stale evidence.
    if pipeline.get("sha") == sha:
        classification = _GL_PIPELINE_STATUS.get(pipeline.get("status"), "infra")
    elif (pipeline.get("ref") or "").endswith("/head"):
        classification = "stale"
    else:
        classification = _GL_PIPELINE_STATUS.get(pipeline.get("status"), "infra")
    receipt["checks"].append({"name": f"pipeline/{pipeline.get('id')}", "url": pipeline.get("web_url"),
                              "head_sha": pipeline.get("sha"), "classification": classification})
    current = _gl_api(endpoint, profile_home=profile_home)
    if (current.get("diff_refs") or {}).get("head_sha") != sha or current.get("state") not in {"opened", "merged"}:
        receipt.update(classification="stale", detail="MR head or state changed while collecting evidence; retry.")
        return
    receipt["classification"] = classification
    receipt["ok"] = classification == "success"


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        backend = "GitHub"
        profile_home = _assignee_profile_home(assignee)
        declared = _match_pr(contract)
        url = contract if declared else published_pr
        match = _match_pr(url)
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        if match.re is not _PR:
            backend = "GitLab"
            _collect_gitlab(match, receipt, profile_home=profile_home)
            return receipt
        owner, name = repo.split("/")
        query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        cred = "gh login" if backend == "GitHub" else "GITLAB_TOKEN"
        login = f"assignee profile {assignee!r}'s {cred}" if assignee else f"the ambient {cred}"
        receipt.update(classification="auth",
                       detail=f"{backend} refused the acceptance read ({exc}) as {login}; "
                              f"fix that profile's {backend} credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr or API error bodies (credentials/host details); the failed phase is actionable.
        how = "gh authentication/API access" if backend == "GitHub" else "GitLab authentication/API access"
        receipt.update(classification="infra", detail=f"{backend} acceptance evidence unavailable or incomplete; check {how} and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
