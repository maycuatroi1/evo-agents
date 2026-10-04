"""Configuration, logging and the hub commands before anything connects: a missing or malformed variable is
named, a missing extra names the pip command, secrets never reach a log line, and the core CLI loads
without the server stack."""

import io
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub.config import ConfigError, HubConfig, load_config, load_dsn, load_log_level
from evo_agents.hub.log import JsonFormatter, dsn_password, redact_dsn, register_secret, scrub

SERVER_MODULES = ("fastapi", "starlette", "uvicorn", "psycopg", "psycopg_pool", "alembic", "sqlalchemy", "sentry_sdk")


@pytest.mark.parametrize("command", ["serve", "migrate"])
def test_missing_dsn_stops_the_command_and_names_the_variable(command):
    result = pg.cli(["hub", command], env=pg.clean_env())
    assert result.returncode == 2
    lines = pg.log_lines(result.stderr)
    assert [line["level"] for line in lines] == ["error"]
    assert lines[0]["variable"] == "EVO_HUB_DSN"
    assert "EVO_HUB_DSN is not set" in lines[0]["msg"]


@pytest.mark.parametrize(
    "variable, value",
    [
        ("EVO_HUB_PORT", "eighty"),
        ("EVO_HUB_PORT", "70000"),
        ("EVO_HUB_POOL_MIN_SIZE", "-1"),
        ("EVO_HUB_POOL_MAX_SIZE", "0"),
        ("EVO_HUB_POOL_TIMEOUT", "soon"),
        ("EVO_HUB_DSN", "mysql://u:p@h/db"),
        ("EVO_HUB_DSN", "just-a-word"),
        ("EVO_HUB_PUBLIC_URL", "agents.omelet.tech"),
        ("EVO_HUB_PUBLIC_URL", "https://agents.omelet.tech/?next=/"),
        ("EVO_HUB_GITHUB_URL", "ftp://github.com"),
        ("EVO_HUB_GITHUB_API_URL", "https://"),
        ("EVO_HUB_GITHUB_TIMEOUT", "0"),
        ("EVO_HUB_SESSION_SECRET", "too-short"),
        ("EVO_HUB_ADMINS", "octo, not a login"),
    ],
)
def test_a_malformed_value_names_its_variable(variable, value):
    env = {"EVO_HUB_DSN": "postgresql://hub@db/hub", variable: value}
    with pytest.raises(ConfigError) as caught:
        load_config(env)
    assert caught.value.variable == variable
    assert variable in str(caught.value)


def test_a_bad_log_level_stops_the_command_and_names_the_variable():
    with pytest.raises(ConfigError, match="EVO_HUB_LOG_LEVEL must be one of"):
        load_log_level({"EVO_HUB_LOG_LEVEL": "loud"})
    assert load_log_level({"EVO_HUB_LOG_LEVEL": "debug"}) == "DEBUG" and load_log_level({}) == "INFO"
    result = pg.cli(["hub", "migrate"], env=pg.clean_env(EVO_HUB_LOG_LEVEL="loud"))
    assert result.returncode == 2
    (line,) = pg.log_lines(result.stderr)
    assert line["variable"] == "EVO_HUB_LOG_LEVEL"


def test_flags_win_over_variables_and_defaults_fill_the_rest(tmp_path):
    env = {"EVO_HUB_DSN": "postgresql://env@db/hub", "EVO_HUB_PORT": "9000", "EVO_HUB_DATA_DIR": str(tmp_path)}
    config = load_config(env, dsn="postgresql://flag@db/hub", port=9100, host="0.0.0.0")
    assert (config.dsn, config.port, config.host, config.data_dir) == (
        "postgresql://flag@db/hub",
        9100,
        "0.0.0.0",
        tmp_path,
    )
    assert (config.pool_min_size, config.pool_max_size, config.pool_timeout) == (1, 10, 10.0)
    assert config.sentry_dsn is None
    assert load_config({"EVO_HUB_DSN": "host=db dbname=hub"}).data_dir == Path("~/.evo/hub-server/cache").expanduser()


def test_sign_in_settings_load_and_their_secrets_never_reach_a_log():
    env = {
        "EVO_HUB_DSN": "postgresql://hub@db/hub",
        "EVO_HUB_ADMINS": " Octo-Admin, hubot ,",
        "EVO_HUB_GITHUB_CLIENT_ID": "Ov23liFakeClientId",
        "EVO_HUB_GITHUB_CLIENT_SECRET": "Client-Secret-10-abcdef",
        "EVO_HUB_SESSION_SECRET": "Session-Secret-11-" + "s" * 32,
        "EVO_HUB_PUBLIC_URL": "https://agents.example.org/",
    }
    config = load_config(env)
    assert config.admins == {"octo-admin", "hubot"} and config.is_admin("OCTO-ADMIN") and not config.is_admin("x")
    assert config.public_url == "https://agents.example.org"
    assert (config.github_url, config.github_api_url) == ("https://github.com", "https://api.github.com")
    assert config.web_login_missing() == []
    assert "Client-Secret-10" not in repr(config) and "Session-Secret-11" not in repr(config)
    line = scrub(f"exchange with {env['EVO_HUB_GITHUB_CLIENT_SECRET']} signed by {env['EVO_HUB_SESSION_SECRET']}")
    assert "Client-Secret-10" not in line and "Session-Secret-11" not in line
    assert scrub("cookie evs_" + "a" * 43) == "cookie evs_***" and scrub("gho_" + "b" * 36) == "gho_***"

    device_only = load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub", "EVO_HUB_GITHUB_CLIENT_ID": "Ov23li"})
    assert device_only.web_login_missing() == [
        "EVO_HUB_GITHUB_CLIENT_SECRET",
        "EVO_HUB_SESSION_SECRET",
        "EVO_HUB_PUBLIC_URL",
    ]
    assert device_only.admins == frozenset()


def test_config_repr_keeps_the_dsns_out():
    config = HubConfig(dsn="postgresql://u:Repr-Secret-1@db/hub", data_dir=Path("/tmp/x"), sentry_dsn="https://k@s/1")
    assert "Repr-Secret-1" not in repr(config) and "https://k@s/1" not in repr(config)


@pytest.mark.parametrize(
    "dsn, password, redacted",
    [
        ("postgresql://hub:s3cr%40t@db:5432/hub", "s3cr%40t", "postgresql://hub:***@db:5432/hub"),
        ("postgresql://hub:p@ss@db/hub?sslmode=require", "p@ss", "postgresql://hub:***@db/hub?sslmode=require"),
        ("postgresql://hub@db/hub?password=qpass&sslmode=require", "qpass", None),
        ("host=db user=hub password=kvpass dbname=hub", "kvpass", "host=db user=hub password=*** dbname=hub"),
        ("host=db password='quoted pass' dbname=hub", "quoted pass", "host=db password=*** dbname=hub"),
        ("postgresql://hub@db/hub", None, "postgresql://hub@db/hub"),
    ],
)
def test_redact_dsn_hides_the_password_in_both_forms(dsn, password, redacted):
    assert dsn_password(dsn) == password
    out = redact_dsn(dsn)
    if redacted is not None:
        assert out == redacted
    if password:
        assert password not in out


def test_json_lines_carry_no_secret(caplog):
    register_secret("Registered-Secret-9")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.hub.json")
    logger.addHandler(handler)
    try:
        logger.warning(
            "connecting to %s\nsecond line",
            "postgresql://hub:Uri-Secret-2@db/hub",
            extra={"dsn": "host=db password=Kv-Secret-3", "nested": {"token": "evh_abcdefghijklmnopqrstuvwxyz"}},
        )
        try:
            raise RuntimeError("auth failed with Registered-Secret-9 and Bearer abcdefghijkl0123")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(handler)
    text = stream.getvalue()
    lines = pg.log_lines(text)
    assert len(lines) == 2 == len(text.splitlines())
    assert lines[0]["level"] == "warning" and lines[0]["logger"] == "tests.hub.json"
    assert lines[0]["msg"] == "connecting to postgresql://hub:***@db/hub\nsecond line"
    assert lines[0]["dsn"] == "host=db password=***" and lines[0]["nested"] == {"token": "evh_***"}
    assert "RuntimeError" in lines[1]["exc"]
    for secret in ("Uri-Secret-2", "Kv-Secret-3", "Registered-Secret-9", "abcdefghijkl0123", "evh_abcdefgh"):
        assert secret not in text
    assert scrub("plain words stay") == "plain words stay"


def test_a_missing_extra_names_the_pip_command(tmp_path):
    # A package that fails to import stands in for an install without the hub-server extra.
    for name in ("fastapi", "alembic"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(f"raise ModuleNotFoundError('not installed', name={name!r})\n")
    env = pg.clean_env(PYTHONPATH=str(tmp_path), EVO_HUB_DSN="postgresql://hub:Extra-Secret-4@127.0.0.1:1/hub")
    for command, module in (("serve", "fastapi"), ("migrate", "alembic")):
        result = pg.cli(["hub", command], env=env)
        assert result.returncode == 2, result.stderr
        (line,) = pg.log_lines(result.stderr)
        assert line["missing_module"] == module
        assert "python -m pip install 'evo-ak[hub-server]'" in line["msg"]
        assert "Extra-Secret-4" not in result.stderr


def test_the_core_cli_loads_without_the_server_stack():
    code = (
        "import sys; import evo_agents.cli as cli; cli.build_parser(); "
        f"print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in {SERVER_MODULES!r})))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_sentry_is_off_without_its_dsn_and_scrubs_what_it_sends(monkeypatch, tmp_path):
    import sentry_sdk

    from evo_agents.hub.server.app import init_sentry

    calls = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    assert init_sentry(HubConfig(dsn="postgresql://h@db/h", data_dir=tmp_path)) is False
    assert calls == []

    sentry_dsn = "https://Sentry-Key-5@o1.ingest.example/1"
    config = load_config({"EVO_HUB_DSN": "postgresql://h:Event-Secret-6@db/h", "EVO_HUB_SENTRY_DSN": sentry_dsn})
    assert init_sentry(config) is True
    (kwargs,) = calls
    assert kwargs["dsn"] == sentry_dsn
    assert kwargs["send_default_pii"] is False and kwargs["include_local_variables"] is False
    event = {"message": "failed on Event-Secret-6", "exception": {"values": [{"value": f"bad {sentry_dsn}"}]}}
    sent = str(kwargs["before_send"](event, {}))
    assert "Event-Secret-6" not in sent and "Sentry-Key-5" not in sent


def test_load_dsn_prefers_the_flag():
    assert (
        load_dsn({"EVO_HUB_DSN": "postgresql://env@db/hub"}, "postgresql://flag@db/hub") == "postgresql://flag@db/hub"
    )
