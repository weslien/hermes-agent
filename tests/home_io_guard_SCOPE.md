# Home IO Guard — manifest.json scoping decision

## Decision
Direction (a): one file-precise guard exemption.

`tests/home_io_guard.py` exempts exactly `Path(__file__).resolve().parent.parent.parent / "manifest.json"`
(the checkout's sibling `manifest.json`).

## Rationale
PM's `_payload_manifest` / `_resident_runtime` probes (`pm/environments.py`, `pm/runtime.py`) stat
`<checkout>/../manifest.json` to detect a packaged payload. For a checkout inside `HERMES_HOME`,
that candidate resolves to `<home>/manifest.json`. This is an existence probe of a packaging file,
not a read of Hermes runtime state. Exempting it lets tests of the PM payload probe run from a
kanban workspace checkout without weakening guard isolation for real home files.

## Constraints
- No `allow_real_home_io` marker.
- Sibling real-home I/O (config.yaml, state.db, install-stamp.json, …) stays refused.
- The exemption is file-precise; a namesake `manifest.json` in another workspace is still refused.
- Tripwire regression: `tests/hermes_cli/test_real_home_tripwire.py` exercises the boundary.

## Sign-off
Two-pass review APPROVE / APPROVE.
Parent implementation: t_512dbb45 (2026-10-23).
QA verification: t_badacd75 (this card).
