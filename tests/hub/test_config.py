"""Configuration, logging and the hub commands before anything connects: a missing or malformed variable is
named, a missing extra names the pip command, secrets never reach a log line, and the core CLI loads
without the server stack."""

import base64
import io
import logging
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import log as hub_log
from evo_agents.hub.config import ConfigError, HubConfig, load_config, load_dsn, load_log_level
from evo_agents.hub.log import JsonFormatter, dsn_password, redact_dsn, register_secret, scrub, unregister_secret

SERVER_MODULES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "psycopg",
    "psycopg_pool",
    "alembic",
    "sqlalchemy",
    "sentry_sdk",
    "cryptography",
)


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
        ("EVO_HUB_PUBLIC_URL", "hub.example.org"),
        ("EVO_HUB_PUBLIC_URL", "https://hub.example.org/?next=/"),
        ("EVO_HUB_GITHUB_URL", "ftp://github.com"),
        ("EVO_HUB_GITHUB_API_URL", "https://"),
        ("EVO_HUB_GITHUB_TIMEOUT", "0"),
        ("EVO_HUB_SESSION_SECRET", "too-short"),
        ("EVO_HUB_ADMINS", "octo, not a login"),
        ("EVO_HUB_BLOB_CONCURRENCY", "0"),
        ("EVO_HUB_BLOB_CONCURRENCY", "257"),
        ("EVO_HUB_BLOB_CONCURRENCY", "many"),
        ("EVO_HUB_KG_KEEP_ARTIFACTS", "0"),
        ("EVO_HUB_KG_KEEP_ARTIFACTS", "1001"),
        ("EVO_HUB_KG_KEEP_ARTIFACTS", "all"),
        ("EVO_HUB_FORWARDED_ALLOW_IPS", "proxy.internal"),
        ("EVO_HUB_FORWARDED_ALLOW_IPS", "10.0.0.1/8"),  # host bits set: uvicorn would read it as a literal
        ("EVO_HUB_FORWARDED_ALLOW_IPS", " , "),
        ("EVO_HUB_FORWARDED_ALLOW_IPS", "*, 10.0.0.1"),
        ("EVO_HUB_SECRETS_KEY", "too-short"),
        ("EVO_HUB_SECRETS_KEY", "A" * 44),  # 33 bytes
        ("EVO_HUB_SECRETS_KEY", "A" * 42 + "+/"),  # base64, not base64url
        ("EVO_HUB_GITHUB_APP_ID", "my app"),
        ("EVO_HUB_GITHUB_APP_PRIVATE_KEY", "not a key"),
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
    assert config.kg_keep_artifacts == 3  # the retention of built graphs keeps each project's 3 newest
    assert load_config({**env, "EVO_HUB_KG_KEEP_ARTIFACTS": "1"}).kg_keep_artifacts == 1
    assert load_config({"EVO_HUB_DSN": "host=db dbname=hub"}).data_dir == Path("~/.evo/hub-server/cache").expanduser()
    assert config.forwarded_allow_ips is None  # uvicorn's default: no proxy beyond the loopback is believed
    proxies = load_config({**env, "EVO_HUB_FORWARDED_ALLOW_IPS": " 10.0.0.0/8, 172.18.0.5 ,fd00::/8"})
    assert proxies.forwarded_allow_ips == "10.0.0.0/8,172.18.0.5,fd00::/8"
    assert load_config({**env, "EVO_HUB_FORWARDED_ALLOW_IPS": "*"}).forwarded_allow_ips == "*"


def test_serve_hands_uvicorn_the_proxies_it_may_believe(monkeypatch, tmp_path):
    import argparse
    import os

    import uvicorn

    from evo_agents.hub import cli as hub_cli

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    for name in [name for name in os.environ if name.startswith("EVO_HUB_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("EVO_HUB_DSN", "postgresql://hub@db/hub")
    monkeypatch.setenv("EVO_HUB_DATA_DIR", str(tmp_path))
    args = argparse.Namespace(dsn=None, data_dir=None, host=None, port=None, hub_command="serve")
    assert hub_cli.cmd_serve.__wrapped__(args) == 0
    monkeypatch.setenv("EVO_HUB_FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    assert hub_cli.cmd_serve.__wrapped__(args) == 0
    assert [call["forwarded_allow_ips"] for call in calls] == [None, "10.0.0.0/8"]


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


def rsa_pem(traditional: bool = True) -> str:
    """A private key of 2048 bits in PEM, as GitHub gives an App's (PKCS#1), or as PKCS#8."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    form = serialization.PrivateFormat.TraditionalOpenSSL if traditional else serialization.PrivateFormat.PKCS8
    return key.private_bytes(serialization.Encoding.PEM, form, serialization.NoEncryption()).decode()


def test_credential_settings_load_and_their_secrets_never_reach_a_log():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = secrets.token_urlsafe(32)
    pem = rsa_pem()
    one_line = pem.strip().replace("\n", "\\n")  # as an environment file holds it
    env = {
        "EVO_HUB_DSN": "postgresql://hub@db/hub",
        "EVO_HUB_SECRETS_KEY": key,
        "EVO_HUB_GITHUB_APP_ID": "123456",
        "EVO_HUB_GITHUB_APP_PRIVATE_KEY": one_line,
    }
    config = load_config(env)
    assert config.secrets_key == base64.urlsafe_b64decode(key + "=") and len(config.secrets_key) == 32
    assert load_config({**env, "EVO_HUB_SECRETS_KEY": key + "="}).secrets_key == config.secrets_key  # padded
    assert config.github_app_id == "123456" and config.github_app_private_key == pem
    assert load_config({**env, "EVO_HUB_GITHUB_APP_PRIVATE_KEY": pem}).github_app_private_key == pem
    load_pem_private_key(config.github_app_private_key.encode(), password=None)
    assert config.credentials_missing() == [] and config.github_app_missing() == []
    pkcs8 = rsa_pem(traditional=False)
    client_id = load_config(
        {**env, "EVO_HUB_GITHUB_APP_ID": "Iv23liAbCdEf01234567", "EVO_HUB_GITHUB_APP_PRIVATE_KEY": pkcs8}
    )
    assert client_id.github_app_id == "Iv23liAbCdEf01234567" and client_id.github_app_private_key == pkcs8

    assert key not in repr(config) and "PRIVATE" not in repr(config)
    standard = base64.b64encode(config.secrets_key).decode()  # the same key as another tool may print it
    lines = [line for line in pem.strip().splitlines()[1:-1] if len(line) >= 16]  # the key's base64, line by line
    assert len(lines) >= 20
    for leaked in (key, standard, pem, one_line, repr(pem), *lines):
        line = scrub(f"loaded {leaked} at start")
        assert "***" in line and not any(part in line for part in (key, standard, *lines)), line

    nothing = load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub"})
    assert (nothing.secrets_key, nothing.github_app_id, nothing.github_app_private_key) == (None, None, None)
    assert nothing.credentials_missing() == ["EVO_HUB_SECRETS_KEY"]
    assert nothing.github_app_missing() == ["EVO_HUB_GITHUB_APP_ID", "EVO_HUB_GITHUB_APP_PRIVATE_KEY"]


def test_the_github_app_needs_both_variables_and_no_error_shows_a_secret():
    pem = rsa_pem()
    base = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    for present, missing in (
        ({"EVO_HUB_GITHUB_APP_ID": "123456"}, "EVO_HUB_GITHUB_APP_PRIVATE_KEY"),
        ({"EVO_HUB_GITHUB_APP_PRIVATE_KEY": pem}, "EVO_HUB_GITHUB_APP_ID"),
    ):
        with pytest.raises(ConfigError) as caught:
            load_config({**base, **present})
        assert caught.value.variable == missing and "together, or neither" in str(caught.value)
        assert pem.splitlines()[1] not in str(caught.value)
    # a malformed key or PEM is named, never shown
    for variable, value, shown in (
        ("EVO_HUB_SECRETS_KEY", "Secret-Key-Value-13+" * 2, "Secret-Key-Value"),
        ("EVO_HUB_GITHUB_APP_PRIVATE_KEY", pem.replace("PRIVATE KEY", "PUBLIC KEY"), pem.splitlines()[1]),
        ("EVO_HUB_GITHUB_APP_PRIVATE_KEY", pem.splitlines()[1], pem.splitlines()[1]),
    ):
        with pytest.raises(ConfigError) as caught:
            load_config({**base, variable: value})
        assert caught.value.variable == variable
        assert shown not in str(caught.value)


def test_the_telegram_variables_turn_the_channel_on_are_secrets_and_either_missing_turns_it_off():
    from tests.hub.fake_telegram import SECRET, TOKEN

    base = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    config = load_config({**base, "EVO_HUB_TELEGRAM_BOT_TOKEN": TOKEN, "EVO_HUB_TELEGRAM_WEBHOOK_SECRET": SECRET})
    assert (config.telegram_bot_token, config.telegram_webhook_secret) == (TOKEN, SECRET)
    assert config.telegram_api_url == "https://api.telegram.org" and config.telegram_missing() == []
    for leaked in (TOKEN, TOKEN.partition(":")[2], SECRET):
        line = scrub(f"calling https://api.telegram.org/bot{leaked}/getMe")
        assert leaked not in line and "***" in line, line
    assert TOKEN not in repr(config) and SECRET not in repr(config)

    # Either one missing turns the channel off; the hub loads all the same.
    only_secret = load_config({**base, "EVO_HUB_TELEGRAM_WEBHOOK_SECRET": SECRET})
    assert only_secret.telegram_missing() == ["EVO_HUB_TELEGRAM_BOT_TOKEN"]
    assert load_config(base).telegram_missing() == ["EVO_HUB_TELEGRAM_BOT_TOKEN", "EVO_HUB_TELEGRAM_WEBHOOK_SECRET"]
    fake_api = load_config({**base, "EVO_HUB_TELEGRAM_API_URL": "http://127.0.0.1:9/"})
    assert fake_api.telegram_api_url == "http://127.0.0.1:9"

    # A malformed value is named, never shown.
    for variable, value in (
        ("EVO_HUB_TELEGRAM_BOT_TOKEN", "Tele-Gram-Token-Shape-Wrong-123456"),
        ("EVO_HUB_TELEGRAM_WEBHOOK_SECRET", "Webhook secret with spaces"),
        ("EVO_HUB_TELEGRAM_WEBHOOK_SECRET", "x" * 257),
    ):
        with pytest.raises(ConfigError) as caught:
            load_config({**base, variable: value})
        assert caught.value.variable == variable and value not in str(caught.value)


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


def test_a_secret_stays_masked_until_each_registration_of_it_is_undone():
    value = "Held Secret/" + secrets.token_hex(8)  # its URL-encoded form is masked with it
    encoded = quote(value, safe="")
    register_secret(value)
    register_secret(value)  # two runs of a worker hold the same lease
    unregister_secret(value)
    assert scrub(f"a {value} b {encoded}") == "a *** b ***", "the other registration still masks it"
    unregister_secret(value)
    assert scrub(f"a {value} b {encoded}") == f"a {value} b {encoded}"
    unregister_secret(value)  # once more than registered: nothing to undo, nothing breaks
    register_secret(value)
    assert scrub(value) == "***"
    unregister_secret(value)
    assert scrub(value) == value


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


CALLBACK_URL = "https://hub.test/v1/auth/web/callback?code=c93b0aa1d2e4f5&state=St4te-Value_9"


def test_query_secrets_are_masked_whichever_logger_writes_the_url():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("tests.hub.some_http_client")
    logger.addHandler(handler)
    try:  # the line httpx and httpx2 write for every request
        logger.warning('HTTP Request: %s %s "%s %d %s"', "GET", CALLBACK_URL, "HTTP/1.1", 303, "See Other")
    finally:
        logger.removeHandler(handler)
    (line,) = pg.log_lines(stream.getvalue())
    callback = "https://hub.test/v1/auth/web/callback?code=***&state=***"
    assert line["msg"] == f'HTTP Request: GET {callback} "HTTP/1.1 303 See Other"'
    assert "c93b0aa1d2e4f5" not in stream.getvalue() and "St4te-Value_9" not in stream.getvalue()

    masked = {
        "/token?client_id=Iv1.abc&client_secret=Cs-1&code=Cd-2&code_verifier=Cv-3#frag": (
            "/token?client_id=Iv1.abc&client_secret=***&code=***&code_verifier=***#frag"
        ),
        "a?access_token=At-4&token_type=bearer&refresh_token=Rt-5&scope=repo": (
            "a?access_token=***&token_type=bearer&refresh_token=***&scope=repo"
        ),
        "<a href='/x?TOKEN=Tk-6&amp;State=St-7'>": "<a href='/x?TOKEN=***&amp;State=***'>",
        "s3?X-Amz-Credential=AKIA%2F20261004%2Fauto&X-Amz-Security-Token=Sec-8&x-amz-signature=Sig-9": (
            "s3?X-Amz-Credential=***&X-Amz-Security-Token=***&x-amz-signature=***"
        ),
    }
    for text, expected in masked.items():
        assert scrub(text) == expected
    for kept in ("exit code=3", "/x?barcode=12&next=/home&mystate=on", "token_type=bearer state of the run"):
        assert scrub(kept) == kept


def test_url_logging_clients_stay_quiet():
    stream, root = io.StringIO(), logging.getLogger()
    saved = root.level, {name: logging.getLogger(name).level for name in hub_log.QUIET_LOGGERS}
    hub_log.configure_logging("DEBUG", stream)
    try:
        for name in ("httpx", "httpx2", "httpcore.http11", "httpcore2.http11", "urllib3.connectionpool"):
            logger = logging.getLogger(name)
            assert logger.getEffectiveLevel() == logging.WARNING, name
            logger.info('HTTP Request: GET %s "HTTP/1.1 303 See Other"', CALLBACK_URL)
            logger.debug("send_request_headers.started request=<Request [b'GET']> %s", CALLBACK_URL)
        logging.getLogger("tests.hub.still_heard").debug("debug lines of the hub itself still reach the log")
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, hub_log._HubHandler):
                root.removeHandler(handler)
        root.setLevel(saved[0])
        for name, level in saved[1].items():
            logging.getLogger(name).setLevel(level)
        logging.captureWarnings(False)
    (line,) = pg.log_lines(stream.getvalue())
    assert line["logger"] == "tests.hub.still_heard"


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


def test_sentry_gets_no_request_body_query_string_or_pairing_code(monkeypatch, tmp_path):
    import sentry_sdk

    from evo_agents.hub.server.app import init_sentry

    calls = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    config = HubConfig(dsn="postgresql://h@db/h", data_dir=tmp_path, sentry_dsn="https://key@o1.ingest.example/1")
    assert init_sentry(config) is True
    (kwargs,) = calls
    assert kwargs["max_request_body_size"] == "never"
    event = {
        "message": "join failed for ABCD-EF12",
        "request": {
            "url": "https://hub.test/v1/worker/join",
            "method": "POST",
            "data": {"code": "abcd-ef12", "hostname": "box"},
            "query_string": "code=oauth-code-7&state=state-8",
        },
    }
    sent = kwargs["before_send"](event, {})
    assert sent["request"] == {"url": "https://hub.test/v1/worker/join", "method": "POST"}
    assert sent["message"] == "join failed for ***"
    assert kwargs["before_breadcrumb"]({"message": "code ABCD-EF12"}, {}) == {"message": "code ***"}


def test_load_dsn_prefers_the_flag():
    assert (
        load_dsn({"EVO_HUB_DSN": "postgresql://env@db/hub"}, "postgresql://flag@db/hub") == "postgresql://flag@db/hub"
    )
