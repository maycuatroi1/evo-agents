"""A fake S3 for the hub tests: moto's server in this process on a free port, no Docker.

Every test gets a bucket and a key pair of its own with random values, so a credential that reaches a log line is
easy to find. moto keeps its objects in the process, not in the server, so a server started again on the same port
finds them. moto checks no signature: what R2 refuses on a presigned URL (another Content-Length, an expired URL)
cannot be tested here, only that the hub signs it.

moto and boto3 are imported inside the helpers: this module loads on a core install too.
"""

from __future__ import annotations

import logging
import secrets
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field

REGION = "us-east-1"  # moto creates a bucket without a location constraint only here


@dataclass
class FakeS3:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    port: int
    servers: list = field(default_factory=list)

    def env(self) -> dict[str, str]:
        return {
            "EVO_HUB_S3_ENDPOINT": self.endpoint,
            "EVO_HUB_S3_BUCKET": self.bucket,
            "EVO_HUB_S3_ACCESS_KEY_ID": self.access_key_id,
            "EVO_HUB_S3_SECRET_ACCESS_KEY": self.secret_access_key,
        }

    def config(self) -> dict:
        """The HubConfig fields of this store."""
        return {
            "s3_endpoint": self.endpoint,
            "s3_bucket": self.bucket,
            "s3_access_key_id": self.access_key_id,
            "s3_secret_access_key": self.secret_access_key,
        }

    def client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=REGION,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )

    def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            return self.client().get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put(self, key: str, data: bytes) -> None:
        self.client().put_object(Bucket=self.bucket, Key=key, Body=data)

    def keys(self, prefix: str = "") -> list[str]:
        response = self.client().list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return sorted(item["Key"] for item in response.get("Contents", []))

    def start(self) -> None:
        from moto.server import ThreadedMotoServer

        server = ThreadedMotoServer(ip_address="127.0.0.1", port=self.port, verbose=False)
        server.start()
        self.port = server.get_host_and_port()[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.servers.append(server)

    def stop(self) -> None:
        """Stop the server: the store stops answering, as R2 going away."""
        while self.servers:
            self.servers.pop().stop()


def put_presigned(url: str, data: bytes) -> int:
    """PUT ``data`` to a presigned URL as a client does; the HTTP status."""
    request = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def get_url(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


@contextmanager
def fake_s3():
    """A running fake S3 with an empty bucket; stopped on exit."""
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # one line per request, with the presigned query string
    fake = FakeS3(
        endpoint="",
        bucket=f"evo-hub-test-{secrets.token_hex(6)}",
        access_key_id=f"AKIDTEST{secrets.token_hex(6).upper()}",
        secret_access_key=f"S3Secret{secrets.token_hex(16)}",
        port=0,
    )
    fake.start()
    try:
        fake.client().create_bucket(Bucket=fake.bucket)
        yield fake
    finally:
        fake.stop()
