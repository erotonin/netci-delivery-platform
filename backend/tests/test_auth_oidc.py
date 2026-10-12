"""The OIDC verifier, exercised against tokens signed with a real key pair.

A JWT validator is the one piece of this codebase where a bug is silently exploitable
rather than merely wrong: every classic JWT forgery -- `alg: none`, swapping RS256 for
HS256 so the public key becomes an HMAC secret, a token from another issuer or audience,
an expired token, a signature that simply does not verify -- looks exactly like a valid
login until someone checks. So each of them is checked here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from app.auth import AuthError, OidcAuthenticator
from app.policy.rules import Role


ISSUER = "https://id.corp.example/realms/netci"
AUDIENCE = "netci-api"
ROLE_MAP = {
    "netci-admins": Role.PLATFORM_ADMIN,
    "release-managers": Role.REVIEWER,
    "engineers": Role.DEVELOPER,
}


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64json(payload: dict) -> str:
    return b64(json.dumps(payload, separators=(",", ":")).encode())


def int_b64(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def jwks_for(rsa_key, ec_key) -> dict:
    rsa_numbers = rsa_key.public_key().public_numbers()
    ec_numbers = ec_key.public_key().public_numbers()
    return {
        "keys": [
            {"kid": "rsa-1", "kty": "RSA", "n": int_b64(rsa_numbers.n), "e": int_b64(rsa_numbers.e)},
            {"kid": "ec-1", "kty": "EC", "crv": "P-256", "x": int_b64(ec_numbers.x), "y": int_b64(ec_numbers.y)},
            {"kid": "broken", "kty": "RSA", "n": "!!!not-base64!!!", "e": "AQAB"},
        ]
    }


def authenticator(rsa_key, ec_key, monkeypatch, **overrides) -> OidcAuthenticator:
    instance = OidcAuthenticator(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        role_map=ROLE_MAP,
        **overrides,
    )
    document = jwks_for(rsa_key, ec_key)
    monkeypatch.setattr(instance, "_fetch_jwks", lambda: document)
    return instance


def claims(**overrides) -> dict:
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "u-1042",
        "name": "Dana Developer",
        "email": "dana@corp.example",
        "roles": ["engineers", "some-unrelated-group"],
        "exp": time.time() + 300,
        "iat": time.time(),
    }
    payload.update(overrides)
    return payload


def sign_rs256(rsa_key, payload: dict, *, kid: str = "rsa-1", alg: str = "RS256") -> str:
    signing_input = f"{b64json({'alg': alg, 'kid': kid, 'typ': 'JWT'})}.{b64json(payload)}"
    signature = rsa_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64(signature)}"


def sign_es256(ec_key, payload: dict) -> str:
    signing_input = f"{b64json({'alg': 'ES256', 'kid': 'ec-1', 'typ': 'JWT'})}.{b64json(payload)}"
    der = ec_key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    # JWS wants fixed-width r||s, not DER.
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{b64(raw)}"


def bearer(token: str) -> str:
    return f"Bearer {token}"


# ------------------------------------------------------------------- happy paths


def test_a_valid_rs256_token_becomes_a_principal(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    principal = verifier.authenticate(bearer(sign_rs256(rsa_key, claims())))

    assert principal.subject == "u-1042"
    assert principal.display_name == "Dana Developer"
    assert principal.email == "dana@corp.example"
    assert principal.method == "oidc"
    # Only mapped groups become roles; the unmapped one is ignored rather than guessed at.
    assert principal.roles == frozenset({Role.DEVELOPER})


def test_ec_signed_tokens_are_accepted_too(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    principal = verifier.authenticate(bearer(sign_es256(ec_key, claims(roles=["netci-admins"]))))
    assert principal.roles == frozenset({Role.PLATFORM_ADMIN})


def test_a_space_separated_roles_claim_is_understood(rsa_key, ec_key, monkeypatch):
    """Some providers emit `scope`-style space-delimited strings rather than a list."""

    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    token = sign_rs256(rsa_key, claims(roles="engineers release-managers"))
    assert verifier.authenticate(bearer(token)).roles == frozenset({Role.DEVELOPER, Role.REVIEWER})


def test_a_malformed_key_in_the_jwks_does_not_break_the_usable_ones(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    assert verifier.authenticate(bearer(sign_rs256(rsa_key, claims()))).subject == "u-1042"


# ------------------------------------------------------------------- forgeries


def test_an_unsigned_alg_none_token_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    token = f"{b64json({'alg': 'none', 'kid': 'rsa-1'})}.{b64json(claims())}."
    with pytest.raises(AuthError, match="unsupported or unsafe"):
        verifier.authenticate(bearer(token))


def test_an_hs256_token_signed_with_the_public_key_is_refused(rsa_key, ec_key, monkeypatch):
    """The algorithm-confusion attack: sign with the public key, hope it is used as HMAC."""

    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = f"{b64json({'alg': 'HS256', 'kid': 'rsa-1'})}.{b64json(claims())}"
    forged = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    with pytest.raises(AuthError, match="unsupported or unsafe"):
        verifier.authenticate(bearer(f"{signing_input}.{b64(forged)}"))


def test_a_tampered_payload_fails_the_signature(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    header, _, signature = sign_rs256(rsa_key, claims()).split(".")
    escalated = b64json(claims(roles=["netci-admins"]))
    with pytest.raises(AuthError, match="signature is not valid"):
        verifier.authenticate(bearer(f"{header}.{escalated}.{signature}"))


def test_a_token_from_another_issuer_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    token = sign_rs256(rsa_key, claims(iss="https://evil.example/realms/netci"))
    with pytest.raises(AuthError, match="different issuer"):
        verifier.authenticate(bearer(token))


def test_a_token_for_another_audience_is_refused(rsa_key, ec_key, monkeypatch):
    """A token minted for a different service must not be replayable against netCI."""

    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    with pytest.raises(AuthError, match="not issued for this audience"):
        verifier.authenticate(bearer(sign_rs256(rsa_key, claims(aud="some-other-api"))))


def test_an_expired_token_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    token = sign_rs256(rsa_key, claims(exp=time.time() - 3600))
    with pytest.raises(AuthError, match="expired"):
        verifier.authenticate(bearer(token))


def test_a_token_with_no_expiry_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    payload = claims()
    del payload["exp"]
    with pytest.raises(AuthError, match="no expiry"):
        verifier.authenticate(bearer(sign_rs256(rsa_key, payload)))


def test_a_token_signed_by_an_unknown_key_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError, match="signature is not valid"):
        verifier.authenticate(bearer(sign_rs256(other, claims())))


def test_a_token_with_an_unknown_kid_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    with pytest.raises(AuthError, match="unknown key"):
        verifier.authenticate(bearer(sign_rs256(rsa_key, claims(), kid="rotated-away")))


def test_a_token_with_no_kid_is_refused(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    signing_input = f"{b64json({'alg': 'RS256'})}.{b64json(claims())}"
    signature = rsa_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(AuthError, match="no key id"):
        verifier.authenticate(bearer(f"{signing_input}.{b64(signature)}"))


# ----------------------------------------------------------------- authorization


def test_a_token_whose_groups_map_to_nothing_is_forbidden_not_anonymous(rsa_key, ec_key, monkeypatch):
    """A real employee with no netCI entitlement must be refused, not silently downgraded."""

    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    token = sign_rs256(rsa_key, claims(roles=["finance", "everyone"]))
    with pytest.raises(AuthError) as raised:
        verifier.authenticate(bearer(token))
    assert raised.value.status == 403
    assert raised.value.code == "FORBIDDEN"


# --------------------------------------------------------------- key management


def test_an_unknown_kid_forces_a_refetch_so_key_rotation_does_not_lock_everyone_out(
    rsa_key, ec_key, monkeypatch
):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    verifier.authenticate(bearer(sign_rs256(rsa_key, claims())))  # warms the cache

    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = rotated.public_key().public_numbers()
    fetches = {"count": 0}

    def refetched() -> dict:
        fetches["count"] += 1
        return {"keys": [{"kid": "rsa-2", "kty": "RSA", "n": int_b64(numbers.n), "e": int_b64(numbers.e)}]}

    monkeypatch.setattr(verifier, "_fetch_jwks", refetched)
    principal = verifier.authenticate(bearer(sign_rs256(rotated, claims(), kid="rsa-2")))
    assert principal.subject == "u-1042"
    assert fetches["count"] == 1


def test_a_bad_bearer_header_is_rejected_before_any_crypto(rsa_key, ec_key, monkeypatch):
    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    for header in (None, "", "Token abc", "Bearer ", "Bearer not.a.jwt.at.all", "Bearer onlyonepart"):
        with pytest.raises(AuthError):
            verifier.authenticate(header)


def test_the_subject_is_the_login_name_when_the_provider_sends_one(rsa_key, ec_key, monkeypatch):
    """Keycloak's `sub` is an opaque UUID; a request list that says who asked for a
    release must show a person. `sub` stays the fallback."""

    verifier = authenticator(rsa_key, ec_key, monkeypatch)
    principal = verifier.authenticate(bearer(sign_rs256(rsa_key, claims(preferred_username="dana"))))
    assert principal.subject == "dana"
    assert verifier.authenticate(bearer(sign_rs256(rsa_key, claims()))).subject == "u-1042"


def test_the_browser_config_reads_discovery_from_the_server_side_url_when_one_is_set(monkeypatch):
    """Inside a cluster the public issuer can resolve to the pod itself; the discovery
    document is then read from an internal URL, and its public endpoints are returned."""
    import io
    import json as _json

    import app.main as main_mod

    monkeypatch.setattr(main_mod.authenticator, "mode", "oidc", raising=False)
    monkeypatch.setenv("NETCI_OIDC_BROWSER_CLIENT_ID", "netci-portal")
    monkeypatch.setenv("NETCI_OIDC_ISSUER", "http://127.0.0.1:8180/realms/netci")
    monkeypatch.setenv("NETCI_OIDC_DISCOVERY_URL", "http://172.17.0.1:8180/realms/netci/.well-known/openid-configuration")
    main_mod._oidc_discovery.update(at=0.0, value=None)
    seen = []

    def fake_urlopen(url, timeout=5):
        seen.append(url)
        body = {"authorization_endpoint": "http://127.0.0.1:8180/realms/netci/protocol/openid-connect/auth",
                "token_endpoint": "http://127.0.0.1:8180/realms/netci/protocol/openid-connect/token"}
        return io.BytesIO(_json.dumps(body).encode())

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", fake_urlopen)
    config = main_mod._oidc_browser_config()
    main_mod._oidc_discovery.update(at=0.0, value=None)
    assert seen == ["http://172.17.0.1:8180/realms/netci/.well-known/openid-configuration"]
    assert config["issuer"] == "http://127.0.0.1:8180/realms/netci"
    assert config["authorizationEndpoint"].startswith("http://127.0.0.1:8180/")
