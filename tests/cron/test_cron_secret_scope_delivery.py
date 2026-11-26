"""Test secret-scope behavior on cron delivery and admission paths.

Proves the delivery-path fix (Workstream A): _get_home_target_chat_id and
_get_home_target_thread_id self-scope via _delivery_secret_scope when no
outer scope is active, so the receipt-reconcile lane no longer raises
UnscopedSecretError under multiplexing. Also proves the unscoped guard stays
intact: a direct _home_env_lookup without a scope still refuses. Covers the
admission-path _profile_cron_scope wrap with an equivalent matrix secret read.
"""

import pytest

from agent import secret_scope


@pytest.fixture(autouse=True)
def _reset_multiplex():
    """Multiplex state leaks across tests; restore it after each test."""
    yield
    secret_scope.set_multiplex_active(False)


@pytest.fixture()
def profile_home(tmp_path):
    home = tmp_path / "profiles" / "ops"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        'MATRIX_HOME_ROOM="!room:example.com"\n'
        'MATRIX_HOME_ROOM_THREAD_ID="t99"\n'
        'TELEGRAM_HOME_CHANNEL="123456789"\n'
        'TELEGRAM_HOME_CHANNEL_THREAD_ID="42"\n'
        'DISCORD_HOME_CHANNEL="987654321"\n',
        encoding="utf-8",
    )
    return home


class TestScopedReadSucceeds:
    def test_matrix_home_room_inside_set_secret_scope(self):
        token = secret_scope.set_secret_scope(
            {"MATRIX_HOME_ROOM": "!scoped-room:example.com"}
        )
        try:
            from cron.scheduler_delivery import _home_env_lookup

            assert _home_env_lookup("MATRIX_HOME_ROOM") == "!scoped-room:example.com"
        finally:
            secret_scope.reset_secret_scope(token)

    def test_siblings_inside_set_secret_scope(self):
        token = secret_scope.set_secret_scope(
            {
                "TELEGRAM_HOME_CHANNEL": "111",
                "DISCORD_HOME_CHANNEL": "222",
                "TELEGRAM_HOME_CHANNEL_THREAD_ID": "333",
            }
        )
        try:
            from cron.scheduler_delivery import _home_env_lookup

            assert _home_env_lookup("TELEGRAM_HOME_CHANNEL") == "111"
            assert _home_env_lookup("DISCORD_HOME_CHANNEL") == "222"
            assert (
                _home_env_lookup("TELEGRAM_HOME_CHANNEL", "_THREAD_ID") == "333"
            )
        finally:
            secret_scope.reset_secret_scope(token)


class TestUnscopedReadRefuses:
    def test_home_env_lookup_raises_when_multiplex_active(self):
        secret_scope.set_multiplex_active(True)
        from cron.scheduler_delivery import _home_env_lookup

        with pytest.raises(secret_scope.UnscopedSecretError):
            _home_env_lookup("MATRIX_HOME_ROOM")

    def test_get_secret_raises_when_multiplex_active(self):
        secret_scope.set_multiplex_active(True)
        with pytest.raises(secret_scope.UnscopedSecretError):
            secret_scope.get_secret("TELEGRAM_HOME_CHANNEL")


class TestDeliveryPathSelfScopes:
    def test_get_home_target_chat_id_self_scopes(self, monkeypatch, profile_home):
        """Without the _delivery_secret_scope wrapper this would raise
        UnscopedSecretError because no outer scope is installed."""
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        secret_scope.set_multiplex_active(True)

        from cron.scheduler_delivery import _get_home_target_chat_id

        assert secret_scope.current_secret_scope() is None
        assert _get_home_target_chat_id("matrix") == "!room:example.com"

    def test_get_home_target_thread_id_self_scopes(self, monkeypatch, profile_home):
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        secret_scope.set_multiplex_active(True)

        from cron.scheduler_delivery import _get_home_target_thread_id

        assert secret_scope.current_secret_scope() is None
        assert _get_home_target_thread_id("matrix") == "t99"

    def test_delivery_secret_scope_noops_when_scope_active(
        self, monkeypatch, profile_home
    ):
        """_delivery_secret_scope must not hide an enclosing scope."""
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        secret_scope.set_multiplex_active(True)

        from cron.scheduler_delivery import _delivery_secret_scope

        outer = secret_scope.set_secret_scope(
            {"MATRIX_HOME_ROOM": "outer-room"}
        )
        try:
            with _delivery_secret_scope():
                assert (
                    secret_scope.get_secret("MATRIX_HOME_ROOM") == "outer-room"
                )
        finally:
            secret_scope.reset_secret_scope(outer)


class TestAdmissionPathWrap:
    def test_profile_cron_scope_binds_secret(self, monkeypatch, profile_home):
        """Equivalent to the delivery-path tests: _profile_cron_scope now
        installs a secret scope so get_secret succeeds inside it."""
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        secret_scope.set_multiplex_active(True)

        from cron.scheduler_provider import _profile_cron_scope

        assert secret_scope.current_secret_scope() is None
        with _profile_cron_scope(profile_home):
            assert (
                secret_scope.get_secret("MATRIX_HOME_ROOM") == "!room:example.com"
            )
        assert secret_scope.current_secret_scope() is None
