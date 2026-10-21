"""Secrets out of a repo's files before they enter the corpus (``evo_agents.kg.redact``, the git connector).

Each fixture value is built from parts, so no file of this repository holds one whole and the git connector, reading
this repository, finds none here. A value is replaced by a marker that names its kind and the rest of the file stays;
references to a secret, empty values, placeholders and descriptive words stay too. The sync's audit line counts the
markers by item and kind, and neither the corpus, the graph nor kg_search hold a value afterwards."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.connectors import git as git_connector
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorRun
from evo_agents.kg.redact import MIN_LENGTH, PEM_MIN_BODY, is_literal_secret, name_kind, redact_file
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import SyncResult, count_item, sync_project
from tests.kg.projects import commit, make_project, write


def _joined(*parts: str) -> str:
    return "".join(parts)


COMPOSE_PASSWORD = _joined("Vq7r", "T2mLp", "9sKw")
URI_PASSWORD = _joined("Zr8-", "Kq2v-", "Hn5c")
PEM_BODY = _joined("MIIEvgIBADANBgkqhkiG9w0BAQEFAASC", "BKgwggSkAgEAAoIBAQC7", "Qm4Zr" * 12)
PEM = _joined("-----BEGIN ", "PRIVATE KEY-----\\n", PEM_BODY, "\\n-----END ", "PRIVATE KEY-----\\n")
GITHUB = _joined("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
AWS_KEY_ID = _joined("AK", "IA", "Z7QW3RT5YU8IO2PL")
GOOGLE = _joined("AI", "za", "SyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R")
ANTHROPIC = _joined("sk-", "ant-", "api03-", "Wq3" * 30)
OPENAI = _joined("sk-", "proj-", "Hb7" * 16)
SLACK = _joined("xo", "xb-", "4815162342-", "Rt7Gh2Kq9Lm4Wn")
HUB = _joined("ev", "h_", "Mz4Q" * 11)
LITERAL = _joined("d9F", "!r2W", "q8Lz")
SECRETS = (COMPOSE_PASSWORD, URI_PASSWORD, PEM_BODY, GITHUB, AWS_KEY_ID, GOOGLE)

COMPOSE = f"""services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: {COMPOSE_PASSWORD}
      REPLICA_PASSWORD: ${{REPLICA_PASSWORD}}
      ADMIN_PASSWORD: ""
  web:
    environment:
      DATABASE_URL: postgres://app:{URI_PASSWORD}@db:5432/app
      CACHE_URL: redis://:${{REDIS_PASSWORD}}@cache:6379/0
"""
SERVICE_ACCOUNT = f"""{{
  "type": "service_account",
  "project_id": "acme-app",
  "private_key": "{PEM}",
  "client_email": "app@acme-app.iam.gserviceaccount.com"
}}
"""
SETTINGS = f"""import os

GITHUB_TOKEN = "{GITHUB}"
AWS_ACCESS_KEY_ID = "{AWS_KEY_ID}"
FIREBASE = {{"apiKey": "{GOOGLE}", "projectId": "acme-app"}}
API_TOKEN = os.environ["API_TOKEN"]
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
LABELS = {{"password": "Mật khẩu", "token": "Token"}}


def connect(password=None):
    return password
"""
CLIENT = "export const config = { apiKey: process.env.API_KEY, secret: `${process.env.APP_SECRET}` };\n"
SETUP = _joined(
    "# Setup\n\nPut `token: <your-token>` in `.env`, and `DB_PASSWORD=${DB_PASSWORD}` in the shell.\n\n```\n",
    "-----BEGIN ",
    "PRIVATE KEY-----\n...\n-----END ",
    "PRIVATE KEY-----\n```\n",
)


def add_secrets(app: Path) -> None:
    write(app / "docker-compose.yml", COMPOSE)
    write(app / "config/service-account.json", SERVICE_ACCOUNT)
    write(app / "app/settings.py", SETTINGS)
    write(app / "web/client.ts", CLIENT)
    write(app / "docs/setup.md", SETUP)
    commit(app, "secrets")


@pytest.fixture
def secret_project(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    add_secrets(tmp_path / "demo-app")
    return project


# The patterns, file by file


REDACTED = {
    "docker-compose password": ("compose.yml", f"POSTGRES_PASSWORD: {LITERAL}\n", LITERAL, "password"),
    "env file in a compose list": ("compose.yml", f'  - "API_TOKEN={LITERAL}"\n', LITERAL, "token"),
    "shell export": ("deploy.sh", f"export JWT_SECRET={LITERAL}\n", LITERAL, "secret"),
    "dotenv example": (".env.example", f"STRIPE_API_KEY={LITERAL}\n", LITERAL, "api_key"),
    "json key": ("config.json", f'{{"client_secret": "{LITERAL}"}}\n', LITERAL, "secret"),
    "python constant": ("settings.py", f'SECRET_KEY = "{LITERAL}"\n', LITERAL, "secret"),
    "python keyword": ("seed.py", f'create_user(email="a@b.c", password="{LITERAL}")\n', LITERAL, "password"),
    "typescript camelCase": ("env.ts", f"export const env = {{ apiKey: '{LITERAL}' }};\n", LITERAL, "api_key"),
    "php array": ("config.php", f"'password' => '{LITERAL}',\n", LITERAL, "password"),
    "go short assignment": ("main.go", f'authToken := "{LITERAL}"\n', LITERAL, "token"),
    "dotted key": ("app.yaml", f"spring.datasource.password: {LITERAL}\n", LITERAL, "password"),
    "private key name": ("app.yaml", f"signing_private_key: '{LITERAL}'\n", LITERAL, "private_key"),
    "uri password in code": ("db.py", f'DSN = "postgresql://app:{LITERAL}@db/app"\n', LITERAL, "uri_password"),
    "uri password, no user": ("app.yaml", f"url: redis://:{LITERAL}@cache:6379\n", LITERAL, "uri_password"),
    "pem in json": ("sa.json", f'{{"private_key": "{PEM}"}}\n', PEM_BODY, "pem"),
    "pem in a document": ("key.md", PEM.replace("\\n", "\n"), PEM_BODY, "pem"),
    "anthropic key": ("notes.txt", f"key {ANTHROPIC} here\n", ANTHROPIC, "anthropic_key"),
    "openai key": ("client.py", f"client = OpenAI({OPENAI!r})\n", OPENAI, "openai_key"),
    "aws access key id": ("ci.yml", f"aws configure set aws_access_key_id {AWS_KEY_ID}\n", AWS_KEY_ID, "aws_key_id"),
    "slack token": ("notify.sh", f"curl -H 'Authorization: Bearer {SLACK}' x\n", SLACK, "slack_token"),
    "google api key": ("firebase.ts", f'const firebase = {{ apiKey: "{GOOGLE}" }};\n', GOOGLE, "google_api_key"),
    "github token": ("push.md", f"git push https://x:{GITHUB}@github.com/o/r\n", GITHUB, "github_token"),
    "hub token": ("hub.md", f"evo-agents hub login --token {HUB}\n", HUB, "hub_token"),
}


@pytest.mark.parametrize("path, text, secret, kind", REDACTED.values(), ids=REDACTED.keys())
def test_redact_replaces_each_kind_of_secret_with_its_marker(path, text, secret, kind):
    redacted, found = redact_file(text, path)
    assert secret not in redacted, redacted
    assert f"[redacted:{kind}]" in redacted, redacted
    assert found == {kind: 1}


def test_redact_keeps_the_rest_of_the_line():
    assert redact_file(f"POSTGRES_PASSWORD: {LITERAL}  # dev\n", "compose.yml")[0] == (
        "POSTGRES_PASSWORD: [redacted:password]  # dev\n"
    )
    assert redact_file(f'DSN = "postgresql://app:{LITERAL}@db/app"\n', "db.py")[0] == (
        'DSN = "postgresql://app:[redacted:uri_password]@db/app"\n'
    )
    text, found = redact_file(f'{{"private_key": "{PEM}", "type": "service_account"}}', "sa.json")
    assert json.loads(text) == {"private_key": "[redacted:pem]\n", "type": "service_account"}
    assert found == {"pem": 1}, "the private_key name does not count the block a second time"


KEPT = {
    "compose reference": ("compose.yml", "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n"),
    "shell reference": ("deploy.sh", 'export API_TOKEN="$API_TOKEN"\n'),
    "workflow secret": ("ci.yml", "token: ${{ secrets.GITHUB_TOKEN }}\n"),
    "python environ": ("settings.py", 'API_TOKEN = os.environ["API_TOKEN"]\nDB_PASSWORD = os.getenv("DB_PASSWORD")\n'),
    "process env": ("env.ts", "const apiKey = process.env.API_KEY;\n"),
    "format string": ("db.py", 'DSN = "postgresql://%(user)s:%(password)s@db/app"\n'),
    "f-string": ("db.py", 'DSN = f"postgresql://{user}:{password}@db/app"\n'),
    "empty": ("compose.yml", 'ADMIN_PASSWORD: ""\nREDIS_PASSWORD:\n'),
    "placeholder": ("README.md", "token: <your-token>\nAPI_KEY=xxxxxxxxxxxx\nDB_PASSWORD=changeme\n"),
    "label": ("i18n.json", '{"password": "Mật khẩu", "token": "Token", "newPassword": "New password"}\n'),
    "type annotation": ("models.py", "class User:\n    password: str\n    api_key: str | None = None\n"),
    "expression": ("auth.py", "token = make_token(user)\nself.password = password\n"),
    "plain word in code": ("roles.py", 'PASSWORD = "Password"\nACCESS_KEY = "access"\n'),
    "path": ("app.yaml", "private_key: /run/secrets/signing.pem\nsecret_key: ./keys/dev.key\n"),
    "number": ("results.json", '{"lech_token": 0.9666916728019714, "MAX_TOKENS": 4096}\n'),
    "word that says what it is": ("api.py", 'TOKEN = "access_token"\nfield = {"api_key": "x-api-key-header"}\n'),
    "maven coordinate": ("deps.yaml", "- io.jsonwebtoken:jjwt-api:0.12.6\n"),
    "concatenated prefix": ("run.py", 'env = {"ANTHROPIC_API_KEY": "sk-ant-" + suffix}\n'),
    "made up for a test": ("test_auth.py", 'login(password="testpass123")\nTOKEN = "fake-0123456789"\n'),
    "well-known default": ("compose.yml", "POSTGRES_PASSWORD: postgres\nMINIO_ROOT_PASSWORD: minioadmin\n"),
    "uri default": ("app.yaml", "url: amqp://guest:guest@localhost:5672/\n"),
    "uri short in code": ("test_env.py", 'URL = "postgres://app:pw@db/lms"\n'),
    "short value": ("app.py", 'csrf_token = "csrf-1"\n'),
    "aws documentation key": ("aws.md", _joined("aws_access_key_id = AKIA", "IOSFODNN7EXAMPLE\n")),
    "pem example": ("README.md", SETUP),
    "provider prefix only": ("docs.md", "Anthropic keys start with sk-ant-api03- and OpenAI keys with sk-proj-.\n"),
}


@pytest.mark.parametrize("path, text", KEPT.values(), ids=KEPT.keys())
def test_redact_keeps_references_placeholders_and_descriptions(path, text):
    assert redact_file(text, path) == (text, {})


def test_redact_reads_a_bare_value_as_a_literal_only_outside_code():
    assert redact_file(f"API_TOKEN={LITERAL}\n", "deploy.sh")[1] == {"token": 1}
    assert redact_file(f"API_TOKEN={LITERAL}\n", "notes.md")[1] == {"token": 1}
    assert redact_file(f"api_token = {LITERAL}\n", "settings.py")[1] == {}, "an expression in code"
    assert redact_file("POSTGRES_PASSWORD: lmsdev\n", "compose.yml")[1] == {"password": 1}
    assert redact_file("POSTGRES_PASSWORD: lmsdev\n", "notes.md")[1] == {}, "a plain word counts in config only"


def test_is_literal_secret_and_name_kind():
    assert is_literal_secret(LITERAL) and len(LITERAL) >= MIN_LENGTH
    assert not is_literal_secret(LITERAL[: MIN_LENGTH - 1])
    assert is_literal_secret("lmsdev", plain_ok=True) and not is_literal_secret("lmsdev")
    for value in ("", "[redacted:password]", "two words", "${X}", "{{ x }}", "/etc/app/key", "https://a.b/c"):
        assert not is_literal_secret(value, plain_ok=True), value
    kinds = {
        "POSTGRES_PASSWORD": "password",
        "db.passwd": "password",
        "passphrase": "password",
        "clientSecret": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "SECRET_KEY": "secret",
        "accessToken": "token",
        "x-api-key": "api_key",
        "ACCESS_KEY": "api_key",
        "privateKey": "private_key",
    }
    assert {name: name_kind(name) for name in kinds} == kinds


def test_pem_block_needs_a_body_of_base64():
    body = "A" * (PEM_MIN_BODY - 1)
    short = _joined("-----BEGIN RSA ", "PRIVATE KEY-----\n", body, "\n-----END RSA ", "PRIVATE KEY-----")
    assert redact_file(short, "notes.md") == (short, {})
    cut = _joined("-----BEGIN ", "PRIVATE KEY-----\n", PEM_BODY)  # no END line: left to the reader of the file
    assert redact_file(cut, "notes.md") == (cut, {})


# The git connector


def run_git(repo: Path) -> dict[str, dict]:
    source = {"id": "app", "connector": "git", "path": str(repo)}
    messages = list(ConnectorRun(ConnectorContext("p", source)))
    assert messages[-1] == {"type": "closed", "status": "ok"}
    return {m["props"]["path"]: m for m in messages if m["type"] == "item" and m["kind"] != "commit"}


def test_git_connector_redacts_secrets_before_the_item_and_keeps_the_structure(secret_project, tmp_path):
    items = run_git(tmp_path / "demo-app")
    texts = {path: item.get("body", {}).get("text", "") for path, item in items.items()}
    for secret in SECRETS:
        assert not any(secret in text for text in texts.values()), secret
        assert not any(secret in json.dumps(item) for item in items.values()), secret

    compose = texts["docker-compose.yml"]
    assert "POSTGRES_PASSWORD: [redacted:password]" in compose
    assert "postgres://app:[redacted:uri_password]@db:5432/app" in compose
    assert "REPLICA_PASSWORD: ${REPLICA_PASSWORD}" in compose and 'ADMIN_PASSWORD: ""' in compose
    assert "redis://:${REDIS_PASSWORD}@cache:6379/0" in compose
    assert json.loads(texts["config/service-account.json"])["private_key"] == "[redacted:pem]\n"
    settings = texts["app/settings.py"]
    for marker in ("[redacted:github_token]", "[redacted:aws_key_id]", "[redacted:google_api_key]"):
        assert marker in settings
    assert 'os.environ["API_TOKEN"]' in settings and '"Mật khẩu"' in settings
    assert [f["anchor"] for f in items["app/settings.py"]["fragments"]] == ["_module", "connect"]

    assert items["docker-compose.yml"]["props"]["redactions"] == {"password": 1, "uri_password": 1}
    assert items["config/service-account.json"]["props"]["redactions"] == {"pem": 1}
    assert items["app/settings.py"]["props"]["redactions"] == {"aws_key_id": 1, "github_token": 1, "google_api_key": 1}
    for path in ("web/client.ts", "docs/setup.md", "app/search.py"):
        assert "redactions" not in items[path]["props"], path
        assert texts[path] == (tmp_path / "demo-app" / path).read_text(encoding="utf-8")


def test_sync_audit_counts_redactions_by_item_and_kind_and_no_value_is_kept(secret_project):
    results = {r.source: r for r in sync_project(secret_project)}
    expected = {
        "app:file:app/settings.py": {"aws_key_id": 1, "github_token": 1, "google_api_key": 1},
        "app:file:config/service-account.json": {"pem": 1},
        "app:file:docker-compose.yml": {"password": 1, "uri_password": 1},
    }
    assert results["app"].ok and results["app"].redactions == expected
    assert results["harness"].redactions == {}
    lines = [json.loads(line) for line in (secret_project.root / "audit.jsonl").read_text().splitlines()]
    assert {line["source"]: line["redactions"] for line in lines} == {"harness": {}, "app": expected}

    report = build_project(secret_project)
    assert report.ok, report.errors
    session = Session(secret_project, "agent")
    for secret in SECRETS:
        found = session.call("kg_search", {"query": secret})["structuredContent"]["results"]
        assert found == [], secret
    structure = session.call("kg_search", {"query": "POSTGRES_PASSWORD"})["structuredContent"]["results"]
    assert "app:file:docker-compose.yml" in {r["id"] for r in structure}

    for path in secret_project.root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert not any(secret.encode() in data for secret in SECRETS), path


def test_without_redaction_kg_search_would_find_the_secret(tmp_path, kg_env, monkeypatch):
    """The control of the test above: the same project, its git connector not redacting, leaks the value."""
    monkeypatch.setattr(git_connector, "redact_file", lambda text, path: (text, {}))
    project = make_project(tmp_path, kg_env)
    add_secrets(tmp_path / "demo-app")
    sync_project(project)
    assert build_project(project).ok
    found = Session(project, "agent").call("kg_search", {"query": COMPOSE_PASSWORD})["structuredContent"]["results"]
    assert "app:file:docker-compose.yml" in {r["id"] for r in found}
    assert any(COMPOSE_PASSWORD.encode() in path.read_bytes() for path in project.root.rglob("*") if path.is_file())


def test_count_item_keeps_only_kinds_and_positive_counts():
    result = SyncResult("app", "run", "ok")
    count_item(result, {"id": "app:file:a", "props": {"redactions": {"password": 2, "pem": 1}}})
    count_item(result, {"id": "app:file:b", "props": {"redactions": {"Has Value=x": 1, "token": "abc", "pem": 0}}})
    count_item(result, {"id": "app:file:c", "props": {"redactions": ["password"]}})
    count_item(result, {"id": "app:file:d"})
    assert result.items == 4
    assert result.redactions == {"app:file:a": {"password": 2, "pem": 1}}


def test_redaction_runs_on_a_core_install_without_the_hub():
    code = (
        "import sys\n"
        "from evo_agents.kg.connectors import git\n"
        "from evo_agents.kg import sync\n"
        "server = ('evo_agents.hub', 'fastapi', 'psycopg', 'sqlalchemy')\n"
        "print(sorted(m for m in sys.modules if m.startswith(server)))\n"
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env, cwd=root)
    assert out.stdout.strip() == "[]"
