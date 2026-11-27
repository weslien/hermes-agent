"""Dual-listed plugins fail validation instead of a silent 'disable wins'.

Regression for #131980's follow-up class: a plugin left in both ``plugins.enabled`` and
``plugins.disabled`` loaded nowhere for a month while every spawn only logged a WARNING.
Membership in the two lists is exclusive — every surface (config load gate, ``hermes plugins
validate``, the loader's gate, doctor, ``plugins list``) must name the plugin and refuse
instead of quietly preferring one side.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli.config import InvalidUserConfigError, require_parseable_user_config, validate_config_structure
from hermes_cli.config_value_checks import exclusive_membership_error
from hermes_cli.plugins_manifest import PluginManifest


def _write_config(home: Path, plugins: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = ["plugins:"]
    for key in ("enabled", "disabled"):
        if key in plugins:
            lines.append(f"  {key}: [{', '.join(plugins[key])}]")
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def dual_list_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    _write_config(home, {"enabled": ["mnemosyne"], "disabled": ["mnemosyne"]})
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    return home


def test_config_structure_reports_dual_listed_plugin_as_error(tmp_path) -> None:
    issues = validate_config_structure({"plugins": {"enabled": ["mnemosyne"],
                                                    "disabled": ["mnemosyne", "other"]}})
    dual = [i for i in issues if "mnemosyne" in i.message]
    assert len(dual) == 1 and dual[0].severity == "error"
    assert dual[0].message == exclusive_membership_error("mnemosyne")
    # A plugin on only one list is not flagged.
    assert not [i for i in issues if "other" in i.message]


def test_require_parseable_user_config_fails_fast_on_dual_listing(dual_list_home) -> None:
    with pytest.raises(InvalidUserConfigError) as excinfo:
        require_parseable_user_config()
    message = str(excinfo.value)
    assert "mnemosyne" in message
    assert "plugins.enabled" in message and "plugins.disabled" in message
    assert "remove it from one list" in message


def test_require_parseable_user_config_accepts_single_listed_plugins(tmp_path, monkeypatch) -> None:
    for only in ("enabled", "disabled"):
        home = tmp_path / f"{only}-only"
        _write_config(home, {only: ["mnemosyne"]})
        monkeypatch.setenv("HERMES_HOME", str(home))
        require_parseable_user_config()  # must not raise


def test_gate_manifest_refuses_dual_listing_at_error_level(tmp_path) -> None:
    from hermes_cli.plugins_discovery import IN_BOTH_LISTS, gate_manifest

    manifest = PluginManifest(name="langfuse", source="user", path=str(tmp_path), key="langfuse")
    verdict = gate_manifest(manifest, {"langfuse"}, {"langfuse"})
    assert verdict.action == "placeholder"
    assert verdict.error == IN_BOTH_LISTS
    assert "wins" not in IN_BOTH_LISTS  # no disable-wins state remains in the error contract
    assert verdict.log is not None
    assert verdict.log[0] == logging.ERROR  # was logging.WARNING before this regression fix
    assert "listed in both plugins.enabled and plugins.disabled" in verdict.log[1] % verdict.log[2]
    # Single-list pass cases: enable-only loads, disable-only skips quietly.
    assert gate_manifest(manifest, set(), {"langfuse"}).action == "load"
    assert gate_manifest(manifest, {"langfuse"}, None).error == "disabled via config"


def test_plugins_validate_reports_dual_listed_config(dual_list_home) -> None:
    """The validator engine fails the config itself: the refused config read becomes a check
    row naming the plugin and both keys, before any plugin-directory check runs."""
    from hermes_cli.plugin_validate import validate_plugin_dir

    report = validate_plugin_dir(dual_list_home / "plugin-under-test")

    assert report.exit_code != 0
    assert any(
        "mnemosyne" in f and "plugins.enabled" in f and "plugins.disabled" in f
        for f in report.failures
    )


def test_load_config_refuses_dual_listing(dual_list_home) -> None:
    """The config read itself fails fast: membership is exclusive, nothing wins (#131980)."""
    from hermes_cli.config import _LOAD_CONFIG_CACHE, InvalidUserConfigError, load_config

    _LOAD_CONFIG_CACHE.clear()
    with pytest.raises(InvalidUserConfigError, match="mnemosyne"):
        load_config()


def test_load_config_allows_dual_listing_under_ignore_flag(dual_list_home, monkeypatch) -> None:
    """--ignore-user-config keeps the rescue path: the read succeeds so repair sessions can run;
    the loader's ERROR backstop (covered by the gate tests) refuses the plugin itself."""
    from hermes_cli.config import _LOAD_CONFIG_CACHE, load_config

    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")
    _LOAD_CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg["plugins"]["enabled"] == ["mnemosyne"]


def test_warm_cache_revalidates_after_edit_to_dual_listing(tmp_path, monkeypatch) -> None:
    """A cached clean load must not mask a later contradictory edit: the signature-keyed
    cache re-reads the changed file and the load raises again (Kimi pass-2 info note)."""
    from hermes_cli.config import _LOAD_CONFIG_CACHE, InvalidUserConfigError, load_config

    home = tmp_path / "hermes-home"
    _write_config(home, {"enabled": ["mnemosyne"]})
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    _LOAD_CONFIG_CACHE.clear()
    assert load_config()["plugins"]["enabled"] == ["mnemosyne"]  # warm the cache cleanly

    _write_config(home, {"enabled": ["mnemosyne"], "disabled": ["mnemosyne"]})
    with pytest.raises(InvalidUserConfigError, match="mnemosyne"):
        load_config()
