"""`hermes doctor` reports a plugin listed in both plugins.enabled and plugins.disabled.

Membership in the two lists is exclusive: a dual-listed plugin is a config validation failure
that nothing resolves (no side wins). Older updates left plugins in that state when they had to
turn them off (#131216), and nothing else said so.
"""
from __future__ import annotations

from hermes_cli.doctor_state import _check_plugins_in_both_lists


def test_plugin_in_both_lists_is_an_issue_with_the_actionable_fix(tmp_path, monkeypatch):
    """Doctor sees what the loader refuses, under any spelling the loader matches."""
    home = tmp_path / ".hermes"
    for rel, name in (("obs/intake", "intake-probe"), ("kept", "kept"), ("parked", "parked")):
        plugin = home / "plugins" / rel
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(f"name: {name}\nversion: '1.0'\n", encoding="utf-8")
        (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [kept, intake-probe]\n  disabled: [obs/intake, parked]\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    finding = _check_plugins_in_both_lists(False)

    assert len(finding.manual_issues) == 1
    issue = finding.manual_issues[0]
    assert "obs/intake" in issue
    assert "listed in both plugins.enabled and plugins.disabled" in issue
    assert "remove it from one list" in issue
