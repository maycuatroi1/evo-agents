"""Blob stores other than R2: the provider presets of the configuration (region, addressing style, deletes one key at a
time), and ``evo-agents hub storage``, which checks a store and copies every blob of one store to another, checked
against its SHA-256 and resumable. A fake S3 (moto, ``tests.hub.s3``); no Postgres."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("boto3")
pytest.importorskip("moto")

from evo_agents.hub.blob_migrate import check, migrate, read_env_file, store_from_env
from evo_agents.hub.blobs import BlobStore, blob_key
from evo_agents.hub.config import (
    S3_ADDRESSING_STYLE,
    S3_PROVIDER,
    S3_PROVIDERS,
    S3_REGION,
    ConfigError,
    load_blob_store,
    load_config,
)

DSN = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def settings(s3, **env) -> dict:
    return load_blob_store({**s3.env(), **env})


def other_bucket(s3, name: str) -> dict[str, str]:
    """The environment of a second bucket on the same fake S3, created empty."""
    s3.client().create_bucket(Bucket=name)
    return {**s3.env(), "EVO_HUB_S3_BUCKET": name}


def test_without_a_provider_the_store_is_r2_as_before(s3):
    config = load_config({**DSN, **s3.env()})
    assert (config.s3_provider, config.s3_region, config.s3_addressing_style) == ("r2", "auto", "path")
    store = BlobStore.from_config(config)
    try:
        assert store.region == "auto" and store.batch_delete
        assert urlsplit(store.presign_get(sha(b"x"))).path.startswith(f"/{s3.bucket}/")
    finally:
        store.close()
    unconfigured = load_config(DSN)
    assert unconfigured.blob_store_missing() and unconfigured.s3_provider == "r2"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({S3_PROVIDER: "seaweedfs"}, ("seaweedfs", "us-east-1", "path")),
        ({S3_PROVIDER: "MinIO"}, ("minio", "us-east-1", "path")),
        ({S3_PROVIDER: "gcs", "EVO_HUB_S3_ENDPOINT": "https://storage.googleapis.com"}, ("gcs", "auto", "path")),
        (
            {S3_PROVIDER: "aws", "EVO_HUB_S3_ENDPOINT": "https://s3.ap-southeast-1.amazonaws.com"},
            ("aws", "ap-southeast-1", "virtual"),
        ),
        (
            {S3_PROVIDER: "aws", "EVO_HUB_S3_ENDPOINT": "https://s3.dualstack.eu-west-1.amazonaws.com"},
            ("aws", "eu-west-1", "virtual"),
        ),
        (
            {S3_PROVIDER: "b2", "EVO_HUB_S3_ENDPOINT": "https://s3.us-west-004.backblazeb2.com"},
            ("b2", "us-west-004", "path"),
        ),
        (
            {S3_PROVIDER: "aws", "EVO_HUB_S3_ENDPOINT": "https://s3.amazonaws.com", S3_REGION: "US-EAST-2"},
            ("aws", "us-east-2", "virtual"),
        ),
        ({S3_PROVIDER: "other", S3_REGION: "hn-1", S3_ADDRESSING_STYLE: "virtual"}, ("other", "hn-1", "virtual")),
        ({S3_ADDRESSING_STYLE: "PATH"}, ("r2", "auto", "path")),
    ],
)
def test_the_provider_sets_the_region_and_the_addressing_style_and_the_variables_override_it(s3, env, expected):
    found = settings(s3, **env)
    assert (found["s3_provider"], found["s3_region"], found["s3_addressing_style"]) == expected


@pytest.mark.parametrize(
    ("env", "variable", "problem"),
    [
        ({S3_PROVIDER: "dropbox"}, S3_PROVIDER, "must be one of r2, aws, gcs"),
        ({S3_PROVIDER: "aws"}, S3_REGION, "provider aws signs for the bucket's region"),
        ({S3_PROVIDER: "b2", "EVO_HUB_S3_ENDPOINT": "https://b2.example.org"}, S3_REGION, "is not set"),
        ({S3_REGION: "us east"}, S3_REGION, "must be a region name"),
        ({S3_ADDRESSING_STYLE: "subdomain"}, S3_ADDRESSING_STYLE, "must be path or virtual"),
    ],
)
def test_a_provider_the_hub_cannot_sign_for_is_a_config_error_naming_the_variable(s3, env, variable, problem):
    with pytest.raises(ConfigError, match=re.escape(problem)) as caught:
        load_config({**DSN, **s3.env(), **env})
    assert caught.value.variable == variable


@pytest.mark.parametrize("variable", [S3_PROVIDER, S3_REGION, S3_ADDRESSING_STYLE])
def test_a_provider_setting_without_the_store_is_a_config_error(variable):
    with pytest.raises(ConfigError, match="is set but the blob store is not") as caught:
        load_config({**DSN, variable: "seaweedfs" if variable == S3_PROVIDER else "path"})
    assert caught.value.variable == variable


def test_every_provider_loads_and_its_store_signs_for_its_region(s3):
    for name, provider in S3_PROVIDERS.items():
        env = {S3_PROVIDER: name} if provider.region else {S3_PROVIDER: name, S3_REGION: "ap-southeast-1"}
        # A host name: botocore keeps the path style for an IP address. Presigning calls nothing.
        store = BlobStore.from_settings(settings(s3, EVO_HUB_S3_ENDPOINT="https://s3.example.org", **env))
        try:
            url = urlsplit(store.presign_get(sha(name.encode())))
            credential = parse_qs(url.query)["X-Amz-Credential"][0]
            assert f"/{store.region}/s3/aws4_request" in credential
            assert store.batch_delete == provider.batch_delete
            on_host = url.netloc.startswith(f"{s3.bucket}.")
            assert on_host == (provider.addressing_style == "virtual"), name
        finally:
            store.close()


def test_a_store_without_delete_objects_deletes_one_key_a_call(s3):
    store = BlobStore.from_settings(settings(s3, **{S3_PROVIDER: "gcs"}))
    try:
        assert not store.batch_delete
        keys = [blob_key(sha(bytes([n]))) for n in range(5)]
        for key in keys:
            s3.put(key, b"x")

        def refused(**_):
            raise AssertionError("DeleteObjects called on a store without it")

        store._s3.delete_objects = refused
        store.delete([*keys, blob_key(sha(b"never there"))])
        assert s3.keys("blobs/") == []
    finally:
        store.close()


def test_the_listing_yields_every_blob_and_leaves_other_names_out(s3):
    data = [os.urandom(n + 1) for n in range(1500)]  # two pages of the listing
    for blob in data:
        s3.put(blob_key(sha(blob)), blob)
    s3.put("blobs/sha256/not-a-hash", b"x")
    s3.put("uploads/00000000-0000-0000-0000-000000000000", b"y")
    store = BlobStore.from_settings(settings(s3))
    try:
        assert dict(store.blobs()) == {sha(blob): len(blob) for blob in data}
    finally:
        store.close()


def test_ensure_bucket_creates_a_missing_bucket_once(s3):
    store = BlobStore.from_settings(settings(s3, EVO_HUB_S3_BUCKET="evo-hub-created", **{S3_PROVIDER: "minio"}))
    try:
        assert store.ensure_bucket() is True
        assert store.ensure_bucket() is False
        store.check()
    finally:
        store.close()


def test_check_puts_copies_reads_lists_and_deletes_one_object_and_leaves_nothing(s3):
    store = BlobStore.from_settings(settings(s3))
    try:
        steps = check(store)
    finally:
        store.close()
    assert [step.name for step in steps if step.ok] == [
        "HeadBucket",
        "presigned PUT",
        "seal (CopyObject, GetObject)",
        "publish (CopyObject)",
        "presigned GET",
        "ranged GetObject",
        "ListObjectsV2",
        "delete (DeleteObjects)",
    ]
    assert all(step.ok for step in steps) and s3.keys() == []


def test_check_stops_at_a_missing_bucket_unless_it_may_create_it(s3):
    # minio: moto takes a bucket without a location only for us-east-1, as AWS does.
    store = BlobStore.from_settings(settings(s3, EVO_HUB_S3_BUCKET="evo-hub-missing", **{S3_PROVIDER: "minio"}))
    try:
        steps = check(store)
        assert [(step.name, step.ok) for step in steps] == [("HeadBucket", False)]
        created = check(store, create_bucket=True)
        assert created[0].name == "CreateBucket" and all(step.ok for step in created)
    finally:
        store.close()


def test_migrate_copies_each_blob_once_checked_against_its_hash_and_reports_a_damaged_one(s3, tmp_path):
    data = [os.urandom(n * 97 + 1) for n in range(40)]
    for blob in data:
        s3.put(blob_key(sha(blob)), blob)
    damaged = sha(b"what the name promises")
    s3.put(blob_key(damaged), b"other bytes")
    source = store_from_env(s3.env())
    target = store_from_env(other_bucket(s3, "evo-hub-target"))
    total = sum(len(blob) for blob in data)
    try:
        dry = migrate(source, target, scratch=tmp_path, dry_run=True)
        assert (dry.listed, dry.copied, dry.copied_bytes) == (41, 41, total + len(b"other bytes"))
        assert target.size(blob_key(sha(data[0]))) is None

        first = migrate(source, target, scratch=tmp_path, parallel=4)
        assert (first.listed, first.copied, first.present, first.copied_bytes) == (41, 40, 0, total)
        assert first.failed == [(damaged, "the source's bytes do not have the SHA-256 the blob is named after")]
        assert dict(target.blobs()) == {sha(blob): len(blob) for blob in data}

        again = migrate(source, target, scratch=tmp_path)
        assert (again.copied, again.present, len(again.failed)) == (0, 40, 1)

        s3.client().put_object(Bucket=target.bucket, Key=blob_key(sha(data[5])), Body=b"\0" * len(data[5]))
        unverified = migrate(source, target, scratch=tmp_path)
        assert unverified.copied == 0  # same size: taken as present without --verify
        verified = migrate(source, target, scratch=tmp_path, verify=True)
        assert (verified.copied, verified.present) == (1, 39)
        assert s3.client().get_object(Bucket=target.bucket, Key=blob_key(sha(data[5])))["Body"].read() == data[5]
        assert list(tmp_path.iterdir()) == []  # every temporary file is gone

        with pytest.raises(ValueError, match="the same bucket"):
            migrate(source, store_from_env(s3.env()))
    finally:
        source.close()
        target.close()


def test_read_env_file_takes_compose_lines_and_never_shows_a_malformed_one(tmp_path):
    path = tmp_path / "target.env"
    path.write_text(
        "# the new store\n\nexport EVO_HUB_S3_BUCKET=evo-hub\nEVO_HUB_S3_ENDPOINT = 'https://s3.example.org'\n"
        'EVO_HUB_S3_SECRET_ACCESS_KEY="a=b#c"\n'
    )
    assert read_env_file(path) == {
        "EVO_HUB_S3_BUCKET": "evo-hub",
        "EVO_HUB_S3_ENDPOINT": "https://s3.example.org",
        "EVO_HUB_S3_SECRET_ACCESS_KEY": "a=b#c",
    }
    path.write_text("EVO_HUB_S3_BUCKET=evo-hub\nsecret-value-without-a-name\n")
    with pytest.raises(ValueError, match="line 2 is not NAME=value") as caught:
        read_env_file(path)
    assert "secret-value" not in str(caught.value)


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVO_HUB_")}
    return subprocess.run(
        [sys.executable, "-m", "evo_agents", "hub", "storage", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def env_file(path: Path, env: dict[str, str]) -> Path:
    path.write_text("".join(f"{name}={value}\n" for name, value in env.items()))
    return path


def test_the_storage_commands_check_and_migrate_from_env_files_and_show_no_key(s3, tmp_path):
    blob = os.urandom(3000)
    s3.put(blob_key(sha(blob)), blob)
    source = env_file(tmp_path / "source.env", s3.env())
    target = env_file(tmp_path / "target.env", other_bucket(s3, "evo-hub-moved"))

    checked = run_cli("check", "--env", str(target))
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.count("\nok\t") == 8

    moved = run_cli("migrate", "--from", str(source), "--to", str(target))
    assert moved.returncode == 0, moved.stderr
    assert json.loads(moved.stdout.splitlines()[-1]) == {
        "listed": 1,
        "copied": 1,
        "present": 0,
        "copied_bytes": 3000,
        "failed": 0,
        "dry_run": False,
    }
    assert s3.client().get_object(Bucket="evo-hub-moved", Key=blob_key(sha(blob)))["Body"].read() == blob
    for output in (checked, moved):
        assert s3.secret_access_key not in output.stdout + output.stderr
        assert s3.access_key_id not in output.stdout + output.stderr

    s3.put(blob_key(sha(b"promised")), b"damaged")
    damaged = run_cli("migrate", "--from", str(source), "--to", str(target))
    assert damaged.returncode == 1 and f"FAILED\t{sha(b'promised')}\t" in damaged.stdout

    partial = env_file(tmp_path / "partial.env", {"EVO_HUB_S3_BUCKET": "evo-hub-moved"})
    refused = run_cli("migrate", "--from", str(source), "--to", str(partial))
    assert refused.returncode == 2 and "EVO_HUB_S3_ENDPOINT is not set" in refused.stderr
    missing = run_cli("check", "--env", str(tmp_path / "nowhere.env"))
    assert missing.returncode == 2 and "nowhere.env" in missing.stderr
