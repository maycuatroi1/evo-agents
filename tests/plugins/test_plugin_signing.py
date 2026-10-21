"""Signatures of plugin bundles: the publishers an operator lists, an Ed25519 signature made and checked, each reason
a hub refuses a signature for (unsigned, malformed, altered, untrusted, invalid), the workflow of a Sigstore
certificate matched against the publisher's, and the claims read from a Fulcio certificate.

The Sigstore checks go through ``FakeSigstore`` (``tests.plugins.samples``) or a stand-in for sigstore-python's
Verifier: a bundle that a GitHub Actions workflow really signed cannot be made offline."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.plugins import signing
from evo_agents.plugins.signing import (
    GITHUB_ISSUER,
    MAX_SIGNATURE,
    ClaimsPolicy,
    Publisher,
    SignatureError,
    SignatureUnavailable,
    SigstoreIdentity,
    SigstoreVerifier,
    Verified,
    certificate_claims,
    key_id,
    parse_publishers,
    read_signature,
    sigstore_envelope,
    verify,
)
from tests.plugins.samples import (
    OTHER_SEED,
    PUBLISHER,
    SIGNER,
    WORKFLOW,
    FakeSigstore,
    ed25519_publishers,
    ed25519_signature,
    public_key,
    sigstore_signature,
)

pytest.importorskip("cryptography")

DATA = b"the bytes of a bundle"


def publishers(text: str) -> tuple[Publisher, ...]:
    return parse_publishers(text)


def refused(reason: str, call, *args, **kwargs) -> str:
    with pytest.raises(SignatureError) as caught:
        call(*args, **kwargs)
    assert caught.value.reason == reason, str(caught.value)
    return str(caught.value)


def test_plugin_signing_publishers_are_read_from_the_operators_list():
    key = public_key()
    other = "Acme/Other/.github/workflows/x.yaml"
    text = f" acme=sigstore:{WORKFLOW}, beta=ed25519:{key},acme=ed25519:{key}, ,acme=sigstore:{other}"
    acme, beta = parse_publishers(text)
    assert acme.id == "acme" and beta.id == "beta"
    assert acme.sigstore == (
        SigstoreIdentity("acme/evo-plugin-sandbox", ".github/workflows/release.yml"),
        SigstoreIdentity("Acme/Other", ".github/workflows/x.yaml"),
    )
    assert acme.ed25519 == beta.ed25519 == (base64.b64decode(key),)
    assert acme.identities() == [
        f"sigstore:{WORKFLOW}",
        "sigstore:Acme/Other/.github/workflows/x.yaml",
        key_id(base64.b64decode(key)),
    ]
    urlsafe = base64.urlsafe_b64encode(bytes([251] * 32)).decode().rstrip("=")
    assert parse_publishers(f"c=ed25519:{urlsafe}")[0].ed25519 == (bytes([251] * 32),)
    assert parse_publishers("") == parse_publishers(None) == ()


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("acme", "lists ID=sigstore:OWNER/REPO/.github/workflows/FILE.yml or ID=ed25519:KEY entries"),
        ("Acme=ed25519:AAAA", "not 'Acme=ed25519:AAAA'"),
        ("acme=gpg:ABCD", "not 'acme=gpg:ABCD'"),
        ("acme=sigstore:acme/repo", "a workflow is OWNER/REPO/.github/workflows/FILE.yml"),
        ("acme=sigstore:acme/repo/.github/workflows/../x.yml", "a workflow is OWNER/REPO"),
        ("acme=ed25519:AAAA", "is not an Ed25519 public key, 32 bytes in base64"),
        ("acme=ed25519:not*base64", "is not an Ed25519 public key"),
    ],
)
def test_plugin_signing_publishers_that_do_not_read_name_the_entry(text, problem):
    with pytest.raises(ValueError, match=problem.replace("*", r"\*").replace(".", r"\.").replace("(", r"\(")):
        parse_publishers(text)


def test_plugin_signing_with_ed25519_checks_with_any_key_of_the_publisher():
    signature = ed25519_signature(DATA)
    assert signature["kind"] == "ed25519" and signature["sha256"] == hashlib.sha256(DATA).hexdigest()
    trusted = publishers(ed25519_publishers())
    expected = Verified(PUBLISHER, "ed25519", key_id(base64.b64decode(public_key())))
    assert verify(DATA, signature, PUBLISHER, trusted) == expected
    rotated = publishers(f"{ed25519_publishers(OTHER_SEED)},{ed25519_publishers()}")  # a key being rotated in
    assert verify(DATA, signature, PUBLISHER, rotated) == expected
    message = refused("invalid", verify, DATA, signature, PUBLISHER, publishers(ed25519_publishers(OTHER_SEED)))
    assert "no key of publisher acme" in message


def test_plugin_signing_refusals_say_why_in_the_order_the_hub_checks():
    trusted = publishers(f"{ed25519_publishers()},acme=sigstore:{WORKFLOW},solo=sigstore:{WORKFLOW}")
    good = ed25519_signature(DATA)
    assert "is not signed: publisher acme signs it in its CI" in refused(
        "unsigned", verify, DATA, None, PUBLISHER, trusted
    )
    for bad, problem in (
        ([good], "a signature is a JSON object"),
        ({**good, "kind": "gpg"}, "kind is sigstore or ed25519, not 'gpg'"),
        ({**good, "sha256": "ABC"}, "names the SHA-256 of the bundle it signs"),
        ({"kind": "sigstore", "sha256": good["sha256"]}, "a sigstore signature carries its bundle"),
        ({**good, "signature": 5}, "an ed25519 signature carries its signature"),
        ({**good, "signature": base64.b64encode(b"short").decode()}, "an Ed25519 signature is 64 bytes"),
    ):
        assert problem in refused("malformed", verify, DATA, bad, PUBLISHER, trusted)
    altered = refused("altered", verify, DATA + b"!", good, PUBLISHER, trusted)
    assert "the bundle is not the one signed" in altered and "changed after signing" in altered
    assert "is not trusted on this hub" in refused("untrusted", verify, DATA, good, "beta", trusted)
    assert "signs with Sigstore on this hub" in refused("untrusted", verify, DATA, good, "solo", trusted)
    only_keys = publishers(ed25519_publishers())
    message = refused("untrusted", verify, DATA, sigstore_signature(DATA), PUBLISHER, only_keys, FakeSigstore())
    assert "signs with Ed25519 keys on this hub, not with Sigstore" in message


def test_plugin_signing_with_sigstore_takes_a_run_of_the_publishers_workflow_only():
    trusted = publishers(f"acme=sigstore:{WORKFLOW}")
    check = FakeSigstore()
    assert verify(DATA, sigstore_signature(DATA), PUBLISHER, trusted, check) == Verified(PUBLISHER, "sigstore", SIGNER)
    tag = SIGNER.replace("refs/heads/main", "refs/tags/v1.2.0")
    assert verify(DATA, sigstore_signature(DATA, identity=tag), PUBLISHER, trusted, check).signed_by == tag
    upper = SIGNER.replace("acme/evo-plugin-sandbox", "ACME/Evo-Plugin-Sandbox")  # GitHub ignores the case of names
    assert verify(DATA, sigstore_signature(DATA, identity=upper), PUBLISHER, trusted, check).signed_by == upper
    for identity in (
        SIGNER.replace("evo-plugin-sandbox", "evo-plugin-sandbox-fork"),
        SIGNER.replace("release.yml", "Release.yml"),
        SIGNER.replace("release.yml", "release.yml.evil"),
        SIGNER.replace("https://github.com/", "https://github.com.evil.example/"),
        SIGNER.split("@")[0],
        "someone@example.org",
    ):
        message = refused(
            "untrusted", verify, DATA, sigstore_signature(DATA, identity=identity), PUBLISHER, trusted, check
        )
        assert f"which is not a workflow of publisher acme ({WORKFLOW})" in message
    message = refused(
        "untrusted",
        verify,
        DATA,
        sigstore_signature(DATA, issuer="https://accounts.google.com"),
        PUBLISHER,
        trusted,
        check,
    )
    assert "an identity of https://accounts.google.com, not of a GitHub Actions workflow" in message
    assert "does not verify" in refused(
        "invalid", verify, DATA, sigstore_signature(DATA, valid=False), PUBLISHER, trusted, check
    )
    assert refused("altered", verify, DATA + b"!", sigstore_signature(DATA), PUBLISHER, trusted, check)
    assert check.calls == 11  # each refusal of a workflow or an issuer came after the check; altered never reached it


def sigstore_bundle(data: bytes, algorithm: str = "SHA2_256") -> dict:
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {},
        "messageSignature": {"messageDigest": {"algorithm": algorithm, "digest": digest}, "signature": ""},
    }


def test_plugin_signing_file_is_an_envelope_or_a_sigstore_bundle_wrapped_into_one():
    bundle = sigstore_bundle(DATA)
    assert read_signature(json.dumps(bundle)) == {
        "kind": "sigstore",
        "sha256": hashlib.sha256(DATA).hexdigest(),
        "bundle": bundle,
    }
    envelope = ed25519_signature(DATA)
    assert read_signature(json.dumps(envelope).encode()) == envelope
    assert sigstore_envelope(bundle)["sha256"] == hashlib.sha256(DATA).hexdigest()
    for text, problem in (
        ("{", "the signature is not JSON"),
        ("[1]", "neither a Sigstore bundle nor an envelope with a kind"),
        (json.dumps({"mediaType": "text/plain"}), "neither a Sigstore bundle nor an envelope"),
        (json.dumps(sigstore_bundle(DATA, "SHA2_384")), "signs no SHA-256 message digest"),
        (json.dumps({**bundle, "messageSignature": {}}), "signs no SHA-256 message digest"),
        (" " * (MAX_SIGNATURE + 1), "a signature is at most 64 KiB"),
    ):
        assert problem in refused("malformed", read_signature, text)


def certificate(identities: list[str], issuer: str | None, *, legacy: bool = False):
    """A certificate with the subject alternative names and the issuer extension Fulcio writes."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sigstore-intermediate")])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
    )
    names = [x509.UniformResourceIdentifier(i) if "://" in i else x509.RFC822Name(i) for i in identities]
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=True)
    if issuer is not None:
        raw = issuer.encode()
        if legacy:
            oid, value = "1.3.6.1.4.1.57264.1.1", raw
        else:
            size = bytes([len(raw)]) if len(raw) < 128 else b"\x81" + bytes([len(raw)])
            oid, value = "1.3.6.1.4.1.57264.1.8", b"\x0c" + size + raw
        builder = builder.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), value), critical=False)
    return builder.sign(key, hashes.SHA256())


def test_plugin_signing_reads_the_issuer_and_identities_of_a_fulcio_certificate():
    claims = certificate_claims(certificate([SIGNER, "dev@example.org"], GITHUB_ISSUER))
    assert claims.issuer == GITHUB_ISSUER and claims.identities == (SIGNER, "dev@example.org")
    assert certificate_claims(certificate([SIGNER], GITHUB_ISSUER, legacy=True)).issuer == GITHUB_ISSUER
    long_issuer = "https://" + "a" * 150 + ".example"
    assert certificate_claims(certificate([SIGNER], long_issuer)).issuer == long_issuer
    assert certificate_claims(certificate([], None)) == signing.SigstoreClaims(None, ())
    policy = ClaimsPolicy()
    policy.verify(certificate([SIGNER], GITHUB_ISSUER))
    assert policy.claims == signing.SigstoreClaims(GITHUB_ISSUER, (SIGNER,))


class StandInVerifier:
    """sigstore-python's Verifier as SigstoreVerifier drives it: the policy sees the certificate, or the signature
    does not verify."""

    def __init__(self, cert=None, error: str | None = None, call_policy: bool = True):
        self.cert, self.error, self.call_policy = cert, error, call_policy

    def verify_artifact(self, data, bundle, policy):
        from sigstore.errors import VerificationError

        if self.call_policy:
            policy.verify(self.cert)
        if self.error:
            raise VerificationError(self.error)


def test_plugin_signing_sigstore_verifier_keeps_the_claims_and_says_what_failed(monkeypatch):
    pytest.importorskip("sigstore")
    from sigstore.models import Bundle

    stand_in = SigstoreVerifier(factory=lambda: StandInVerifier(certificate([SIGNER], GITHUB_ISSUER)))
    assert "not a Sigstore bundle" in refused("malformed", stand_in, DATA, {"mediaType": "nonsense"})
    monkeypatch.setattr(Bundle, "from_json", staticmethod(lambda text: object()))
    assert stand_in(DATA, {}) == signing.SigstoreClaims(GITHUB_ISSUER, (SIGNER,))
    failing = SigstoreVerifier(
        factory=lambda: StandInVerifier(certificate([SIGNER], GITHUB_ISSUER), "Signature is invalid for input")
    )
    assert "does not verify: Signature is invalid for input" in refused("invalid", failing, DATA, {})
    unchecked = SigstoreVerifier(factory=lambda: StandInVerifier(call_policy=False))
    assert "its certificate was not checked" in refused("invalid", unchecked, DATA, {})

    def unreachable():
        raise OSError("tuf-repo-cdn.sigstore.dev is unreachable")

    with pytest.raises(
        SignatureUnavailable, match=r"cannot load Sigstore.s trust root \(OSError: .*unreachable\); try again"
    ):
        SigstoreVerifier(factory=unreachable)(DATA, {})
    assert signing.default_sigstore() is signing.default_sigstore()
