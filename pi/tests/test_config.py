"""Tests for config.py — loading, validation, masking, and defaults."""

import os
import sys
from contextlib import contextmanager

import pytest

# Baseline valid env that _load() will accept without error.
_VALID = {
    "API_URL": "http://100.64.0.1:8080/sync",
    "API_KEY": "test-key-abcdefghijklmnop",
    "TAILSCALE_IP": "100.64.0.1",
    "DB_PATH": "/tmp/t.db",
    "LOG_PATH": "/tmp/t.log",
    "OBD_PORT": "/dev/rfcomm0",
    "SYNC_BATCH_SIZE": "500",
}

_OPT_KEYS = {"DB_PATH", "LOG_PATH", "OBD_PORT", "SYNC_BATCH_SIZE"}


@pytest.fixture(autouse=True)
def _restore_config_module():
    """Reinstate the original config module after each test.

    Several tests here reload/pop 'config' from sys.modules to exercise _load().
    Production modules (health, logger, ...) cache the singleton via
    `from config import config` at import time, so a left-over reloaded module
    would desync those cached references and break unrelated tests downstream
    (e.g. test_health patching a config instance health no longer uses). Snapshot
    and restore guarantees isolation regardless of what a test does to sys.modules.
    """
    saved = sys.modules.get("config")
    yield
    if saved is not None:
        sys.modules["config"] = saved
    else:
        sys.modules.pop("config", None)


@contextmanager
def _fresh_config(overrides=None):
    """Reload config in isolation with controlled env vars.

    Pops 'config' from sys.modules so _load() re-executes. Restores the
    original env and the original config module in the finally block so other
    tests continue to use conftest's valid config.

    The original module object MUST be reinstated (not just popped): production
    modules cache their singleton via `from config import config` at import
    time, so leaving config popped would let a later import build a different
    object and desync those cached references.
    """
    all_keys = set(_VALID) | (set(overrides) if overrides else set())
    saved = {k: os.environ.get(k) for k in all_keys}
    saved_mod = sys.modules.get("config")
    sys.modules.pop("config", None)

    try:
        for k, v in _VALID.items():
            os.environ[k] = v
        if overrides:
            for k, v in overrides.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        import config as cfg
        yield cfg

    finally:
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
        if saved_mod is not None:
            sys.modules["config"] = saved_mod
        else:
            sys.modules.pop("config", None)


# ---------------------------------------------------------------------------
# Happy-path loading
# ---------------------------------------------------------------------------

class TestValidConfig:
    def test_required_fields_present(self):
        with _fresh_config() as cfg:
            assert cfg.config.API_URL == "http://100.64.0.1:8080/sync"
            assert cfg.config.API_KEY == "test-key-abcdefghijklmnop"
            assert cfg.config.TAILSCALE_IP == "100.64.0.1"

    def test_optional_fields_loaded(self):
        with _fresh_config() as cfg:
            assert cfg.config.OBD_PORT == "/dev/rfcomm0"
            assert cfg.config.SYNC_BATCH_SIZE == 500
            assert cfg.config.DB_PATH == "/tmp/t.db"
            assert cfg.config.LOG_PATH == "/tmp/t.log"

    def test_sync_batch_size_is_int(self):
        with _fresh_config({"SYNC_BATCH_SIZE": "250"}) as cfg:
            assert cfg.config.SYNC_BATCH_SIZE == 250
            assert isinstance(cfg.config.SYNC_BATCH_SIZE, int)

    def test_api_key_masked_in_str(self):
        with _fresh_config() as cfg:
            s = str(cfg.config)
            assert "test-key-abcdefghijklmnop" not in s
            assert "API_KEY=***" in s

    def test_str_contains_non_sensitive_fields(self):
        with _fresh_config() as cfg:
            s = str(cfg.config)
            assert "100.64.0.1" in s
            assert "OBD_PORT" in s
            assert "DB_PATH" in s


# ---------------------------------------------------------------------------
# Optional defaults
# ---------------------------------------------------------------------------

class TestOptionalDefaults:
    def test_obd_port_defaults(self):
        with _fresh_config({"OBD_PORT": None}) as cfg:
            assert cfg.config.OBD_PORT == "/dev/rfcomm0"

    def test_sync_batch_size_defaults_to_500(self):
        with _fresh_config({"SYNC_BATCH_SIZE": None}) as cfg:
            assert cfg.config.SYNC_BATCH_SIZE == 500

    def test_db_path_defaults(self):
        with _fresh_config({"DB_PATH": None}) as cfg:
            assert cfg.config.DB_PATH == "/mnt/usb/data/obd.db"

    def test_log_path_defaults(self):
        with _fresh_config({"LOG_PATH": None}) as cfg:
            assert cfg.config.LOG_PATH == "/mnt/usb/logs/obd.log"


# ---------------------------------------------------------------------------
# Required key validation — each missing key causes sys.exit(1)
# ---------------------------------------------------------------------------

class TestMissingRequiredKeys:
    def test_missing_api_url_exits(self):
        with pytest.raises(SystemExit) as exc:
            with _fresh_config({"API_URL": None}):
                pass
        assert exc.value.code == 1

    def test_missing_api_key_exits(self):
        with pytest.raises(SystemExit) as exc:
            with _fresh_config({"API_KEY": None}):
                pass
        assert exc.value.code == 1

    def test_missing_tailscale_ip_exits(self):
        with pytest.raises(SystemExit) as exc:
            with _fresh_config({"TAILSCALE_IP": None}):
                pass
        assert exc.value.code == 1

    def test_all_missing_exits_and_names_all(self, capsys):
        with pytest.raises(SystemExit):
            with _fresh_config({"API_URL": None, "API_KEY": None, "TAILSCALE_IP": None}):
                pass
        err = capsys.readouterr().err
        assert "API_URL" in err
        assert "API_KEY" in err
        assert "TAILSCALE_IP" in err


# ---------------------------------------------------------------------------
# SYNC_BATCH_SIZE validation
# ---------------------------------------------------------------------------

class TestBatchSizeValidation:
    def test_non_integer_exits(self):
        with pytest.raises(SystemExit) as exc:
            with _fresh_config({"SYNC_BATCH_SIZE": "notanumber"}):
                pass
        assert exc.value.code == 1

    def test_zero_exits(self):
        with pytest.raises(SystemExit) as exc:
            with _fresh_config({"SYNC_BATCH_SIZE": "0"}):
                pass
        assert exc.value.code == 1

    def test_negative_exits(self):
        with pytest.raises(SystemExit) as exc:
            with _fresh_config({"SYNC_BATCH_SIZE": "-5"}):
                pass
        assert exc.value.code == 1

    def test_valid_value_accepted(self):
        with _fresh_config({"SYNC_BATCH_SIZE": "1000"}) as cfg:
            assert cfg.config.SYNC_BATCH_SIZE == 1000


# ---------------------------------------------------------------------------
# Config file present — reads keys from file into env
# ---------------------------------------------------------------------------

class TestConfigFileReading:
    """Tests for the config file reading path in _load().

    Calls _load() directly rather than reimporting the module, because
    CONFIG_PATH is a module-level constant that cannot be changed in a
    freshly-imported module without patching the source.
    """

    def _call_load(self, cfg_file, extra_env=None, clear_keys=None):
        """Call _load() with CONFIG_PATH patched to a temp file."""
        import config as cfg_mod
        from unittest.mock import patch as _patch
        keys = clear_keys or []
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            if extra_env:
                for k, v in extra_env.items():
                    os.environ[k] = v
            with _patch.object(cfg_mod, "CONFIG_PATH", str(cfg_file)):
                result = cfg_mod._load()
            return result
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_reads_required_keys_from_file(self, tmp_path):
        """Keys absent from env but present in file are loaded via the file path (line 94)."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text(
            "API_URL=http://from-file:8080/sync\n"
            "API_KEY=file-api-key\n"
            "TAILSCALE_IP=100.1.2.3\n"
        )
        result = self._call_load(
            cfg_file,
            clear_keys=["API_URL", "API_KEY", "TAILSCALE_IP"],
        )
        assert result.API_URL == "http://from-file:8080/sync"
        assert result.API_KEY == "file-api-key"
        assert result.TAILSCALE_IP == "100.1.2.3"

    def test_env_wins_over_file_value_and_logs(self, tmp_path, capsys):
        """When key is in both env and file, env wins and a message is logged (lines 89-93)."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text("TAILSCALE_IP=from-file-ip\n")
        # Keep TAILSCALE_IP in env (conftest already set it) so env wins
        result = self._call_load(cfg_file)
        assert result.TAILSCALE_IP != "from-file-ip"
        err = capsys.readouterr().err
        assert "environment" in err
        assert "TAILSCALE_IP" in err

    def test_comment_lines_and_blanks_in_file_ignored(self, tmp_path):
        """Lines starting with # or blank lines are skipped."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text(
            "# This is a comment\n"
            "\n"
            "API_URL=http://100.64.0.1:8080/sync\n"
            "API_KEY=test-key\n"
            "TAILSCALE_IP=100.64.0.1\n"
        )
        result = self._call_load(
            cfg_file,
            clear_keys=["API_URL", "API_KEY", "TAILSCALE_IP"],
        )
        assert result.API_URL == "http://100.64.0.1:8080/sync"


# ---------------------------------------------------------------------------
# Config file absent — falls back to env-only
# ---------------------------------------------------------------------------

class TestConfigFileMissing:
    def test_loads_from_env_when_file_missing(self, capsys):
        """_load() warns but succeeds when config file is absent."""
        with _fresh_config() as cfg:
            sys.modules.pop("config", None)
            saved_path = None
            try:
                import config as cfg_mod
                # Patch CONFIG_PATH to a non-existent path inside the module
                original = cfg_mod.CONFIG_PATH
                cfg_mod.CONFIG_PATH = "/nonexistent/config.env"
                sys.modules.pop("config", None)

                import config as cfg2
                assert cfg2.config.API_KEY == "test-key-abcdefghijklmnop"
            finally:
                sys.modules.pop("config", None)


# ---------------------------------------------------------------------------
# Env var wins over config file value
# ---------------------------------------------------------------------------

class TestEnvOverridesFile:
    def test_env_var_priority_logged(self, tmp_path, capsys):
        """When env var is set, _load() logs that env wins and uses env value."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text("SYNC_BATCH_SIZE=999\n")

        sys.modules.pop("config", None)
        saved = os.environ.get("SYNC_BATCH_SIZE")
        try:
            os.environ["SYNC_BATCH_SIZE"] = "123"
            # Use a module-level patch to point CONFIG_PATH to our temp file
            import config as cfg_mod
            cfg_mod.CONFIG_PATH = str(cfg_file)
            sys.modules.pop("config", None)

            import config as cfg2
            # env var "123" wins over file "999"
            assert cfg2.config.SYNC_BATCH_SIZE == 123
            err = capsys.readouterr().err
            assert "environment" in err
        finally:
            if saved is None:
                os.environ.pop("SYNC_BATCH_SIZE", None)
            else:
                os.environ["SYNC_BATCH_SIZE"] = saved
            sys.modules.pop("config", None)
