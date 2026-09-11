"""Who is calling, and what they are allowed to do.

netCI's governance claims -- production needs approval, every transition names an actor,
the audit trail is evidence -- are only worth something if the platform knows who the
caller is. An actor taken from the request body is a claim, not an identity: anyone can
send `{"actor": "the-cto"}`.

Authentication is a seam, like CI and CD. `NETCI_AUTH_MODE` selects the implementation:

    none    Every caller is an anonymous principal holding every role. Intended for a
            developer running the stack on their laptop. Requests served this way are
            refused unless they arrive from loopback -- see `Principal.is_anonymous`
            and the guard in `main.py`. An open netCI reachable from the network is not
            something to arrive at by forgetting to set a variable.
    token   Bearer tokens issued by the platform team, listed in a file that stores
            SHA-256 hashes rather than the tokens themselves.
    oidc    Bearer JWTs from a real identity provider, verified against its JWKS.

The domain never imports this module: it takes an actor string, and the composition root
is what guarantees the string came from a verified identity.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .policy.rules import Role
from .runtime_environment import require_live_mode


class AuthError(Exception):
    """A credential was missing, malformed, expired or not good enough."""

    def __init__(self, code: str, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- principal


@dataclass(frozen=True)
class Principal:
    """A verified caller.

    `subject` is what lands in the audit trail. It is stable across renames, which is why
    the display name is carried separately and never used as the actor.
    """

    subject: str
    display_name: str
    email: str
    roles: frozenset[Role]
    method: str
    # Which teams this caller belongs to. Roles say what kind of thing someone may do;
    # teams say which applications they may do it to. Both are needed: a reviewer role
    # without team scoping lets anyone approve anyone's production release.
    teams: frozenset[str] = frozenset()

    @property
    def is_anonymous(self) -> bool:
        return self.method == "none"

    def has_any(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))

    def belongs_to(self, team: str) -> bool:
        return team in self.teams

    def as_json(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "displayName": self.display_name,
            "email": self.email,
            "roles": sorted(role.value for role in self.roles),
            "teams": sorted(self.teams),
            "method": self.method,
        }


ALL_HUMAN_ROLES = frozenset({Role.VIEWER, Role.DEVELOPER, Role.REVIEWER, Role.PLATFORM_ADMIN})

ANONYMOUS = Principal(
    subject="anonymous",
    display_name="Anonymous (auth disabled)",
    email="",
    roles=ALL_HUMAN_ROLES,
    method="none",
)


def _parse_roles(raw: object, *, source: str) -> frozenset[Role]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{source}: roles must be a non-empty list")
    roles: set[Role] = set()
    for item in raw:
        try:
            roles.add(Role(str(item)))
        except ValueError as exc:
            known = ", ".join(sorted(role.value for role in Role))
            raise ValueError(f"{source}: unknown role {item!r}; known roles are {known}") from exc
    return frozenset(roles)


# ----------------------------------------------------------------------- authenticators


class Authenticator(Protocol):
    mode: str

    def authenticate(self, authorization: str | None) -> Principal: ...


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("UNAUTHENTICATED", "a bearer token is required")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise AuthError("UNAUTHENTICATED", "Authorization must be 'Bearer <token>'")
    return value.strip()


class OpenAuthenticator:
    """No authentication required by default, but recognizes demo persona tokens."""

    mode = "none"

    def authenticate(self, authorization: str | None) -> Principal:
        if authorization:
            scheme, _, value = authorization.partition(" ")
            token = value.strip().lower()
            if token in {"demo-admin", "admin", "admin-token"}:
                return Principal(
                    subject="admin",
                    display_name="Alexander Admin (Platform Lead)",
                    email="admin@netci.local",
                    roles=frozenset({Role.PLATFORM_ADMIN, Role.REVIEWER, Role.DEVELOPER, Role.VIEWER}),
                    method="demo",
                    teams=frozenset({"payments", "core", "infrastructure"}),
                )
            if token in {"demo-dev", "dev", "dev-token"}:
                return Principal(
                    subject="dev",
                    display_name="David Developer (Backend Engineer)",
                    email="dev@netci.local",
                    roles=frozenset({Role.DEVELOPER, Role.VIEWER}),
                    method="demo",
                    teams=frozenset({"payments"}),
                )
        return ANONYMOUS


class TokenAuthenticator:
    """Bearer tokens listed in a file, stored as SHA-256 hashes.

    Hashes rather than tokens: a config file, a backup of it or a copy in a ticket does
    not then hand over working credentials. The file is re-read when its mtime changes,
    so revoking a token or onboarding an engineer does not need a restart.
    """

    mode = "token"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._loaded_from_mtime: float | None = None
        self._by_hash: dict[str, Principal] = {}
        self._load()

    # The file is small and read under a lock; a request never parses it concurrently.
    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            stat = self._path.stat()
        except OSError as exc:
            raise AuthError(
                "AUTH_NOT_CONFIGURED",
                f"cannot read NETCI_AUTH_TOKENS_FILE at {self._path}: {exc}",
                status=503,
            ) from exc

        try:
            import yaml  # local import: only token mode needs it

            payload = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001 - surfaced as a 503 with the reason
            raise AuthError("AUTH_NOT_CONFIGURED", f"invalid token file: {exc}", status=503) from exc

        principals = (payload or {}).get("principals")
        if not isinstance(principals, list) or not principals:
            raise AuthError(
                "AUTH_NOT_CONFIGURED", "token file must define a non-empty 'principals' list", status=503
            )

        by_hash: dict[str, Principal] = {}
        for index, entry in enumerate(principals):
            source = f"principals[{index}]"
            if not isinstance(entry, dict):
                raise AuthError("AUTH_NOT_CONFIGURED", f"{source}: must be a mapping", status=503)
            digest = str(entry.get("tokenSha256", "")).strip().lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise AuthError(
                    "AUTH_NOT_CONFIGURED",
                    f"{source}: tokenSha256 must be a 64-character hex SHA-256 digest. "
                    "Generate one with: python -c \"import hashlib,secrets;"
                    "t=secrets.token_urlsafe(32);print(t, hashlib.sha256(t.encode()).hexdigest())\"",
                    status=503,
                )
            if digest in by_hash:
                raise AuthError("AUTH_NOT_CONFIGURED", f"{source}: duplicate tokenSha256", status=503)
            subject = str(entry.get("subject", "")).strip()
            if not subject:
                raise AuthError("AUTH_NOT_CONFIGURED", f"{source}: subject is required", status=503)
            try:
                roles = _parse_roles(entry.get("roles"), source=source)
            except ValueError as exc:
                raise AuthError("AUTH_NOT_CONFIGURED", str(exc), status=503) from exc
            raw_teams = entry.get("teams") or []
            if not isinstance(raw_teams, list):
                raise AuthError("AUTH_NOT_CONFIGURED", f"{source}: teams must be a list", status=503)
            by_hash[digest] = Principal(
                teams=frozenset(str(team).strip() for team in raw_teams if str(team).strip()),
                subject=subject,
                display_name=str(entry.get("displayName") or subject),
                email=str(entry.get("email") or ""),
                roles=roles,
                method="token",
            )

        self._by_hash = by_hash
        self._loaded_from_mtime = stat.st_mtime

    def _refresh_if_changed(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return  # keep serving the last good file rather than locking everyone out
        if mtime != self._loaded_from_mtime:
            with self._lock:
                if mtime != self._loaded_from_mtime:
                    self._load()

    def authenticate(self, authorization: str | None) -> Principal:
        self._refresh_if_changed()
        token = _bearer(authorization)
        digest = hashlib.sha256(token.encode()).hexdigest()
        # Compare against every entry so the time taken does not reveal which prefix of
        # the digest matched, and so an unknown token costs the same as a known one.
        found: Principal | None = None
        for candidate, principal in self._by_hash.items():
            if secrets.compare_digest(candidate, digest):
                found = principal
        if found is None:
            raise AuthError("UNAUTHENTICATED", "unknown or revoked token")
        return found


# ------------------------------------------------------------------------------- oidc


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class OidcAuthenticator:
    """Verify a JWT issued by a real identity provider.

    Verification is deliberately strict, because every relaxation here is a way in:
    the algorithm comes from the key, not from the token header; `none` and the HMAC
    family are refused outright; and issuer, audience and expiry are all required.
    """

    mode = "oidc"

    _SUPPORTED = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        roles_claim: str = "roles",
        role_map: dict[str, Role] | None = None,
        teams_claim: str = "groups",
        cache_seconds: float = 300.0,
        leeway_seconds: float = 60.0,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.roles_claim = roles_claim
        self.role_map = role_map or {}
        # Team membership comes from the directory, unmapped: netCI compares the group
        # names the IdP sends against the owner_team recorded on an application, so the
        # two only have to agree on a string.
        self.teams_claim = teams_claim
        self.cache_seconds = cache_seconds
        self.leeway_seconds = leeway_seconds
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    # ------------------------------------------------------------------ key material
    def _fetch_jwks(self) -> dict[str, Any]:
        try:
            request = urllib.request.Request(self.jwks_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AuthError(
                "AUTH_PROVIDER_UNAVAILABLE",
                f"cannot reach the identity provider's JWKS at {self.jwks_url}: {exc}",
                status=503,
            ) from exc

    def _key_for(self, kid: str) -> Any:
        now = time.monotonic()
        with self._lock:
            stale = now - self._fetched_at > self.cache_seconds
            # An unknown kid also forces a refetch: that is what a key rotation looks
            # like from here, and waiting out the cache would fail every login until it
            # expired.
            if stale or kid not in self._keys:
                self._keys = self._parse_jwks(self._fetch_jwks())
                self._fetched_at = now
            key = self._keys.get(kid)
        if key is None:
            raise AuthError("UNAUTHENTICATED", f"token signed by unknown key {kid!r}")
        return key

    @staticmethod
    def _parse_jwks(document: dict[str, Any]) -> dict[str, Any]:
        from cryptography.hazmat.primitives.asymmetric.ec import (
            SECP256R1,
            SECP384R1,
            SECP521R1,
            EllipticCurvePublicNumbers,
        )
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

        curves = {"P-256": SECP256R1(), "P-384": SECP384R1(), "P-521": SECP521R1()}
        keys: dict[str, Any] = {}
        for entry in document.get("keys", []):
            if not isinstance(entry, dict) or "kid" not in entry:
                continue
            try:
                if entry.get("kty") == "RSA":
                    numbers = RSAPublicNumbers(
                        e=int.from_bytes(_b64url(entry["e"]), "big"),
                        n=int.from_bytes(_b64url(entry["n"]), "big"),
                    )
                    keys[str(entry["kid"])] = numbers.public_key()
                elif entry.get("kty") == "EC" and entry.get("crv") in curves:
                    numbers = EllipticCurvePublicNumbers(
                        x=int.from_bytes(_b64url(entry["x"]), "big"),
                        y=int.from_bytes(_b64url(entry["y"]), "big"),
                        curve=curves[str(entry["crv"])],
                    )
                    keys[str(entry["kid"])] = numbers.public_key()
            except (KeyError, ValueError, TypeError):
                continue  # a malformed key must not take the whole key set down
        return keys

    # ---------------------------------------------------------------- verification
    def _verify_signature(self, key: Any, algorithm: str, signing_input: bytes, signature: bytes) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, utils
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

        digest = {"256": hashes.SHA256(), "384": hashes.SHA384(), "512": hashes.SHA512()}[algorithm[2:]]
        try:
            if algorithm.startswith("RS") and isinstance(key, RSAPublicKey):
                key.verify(signature, signing_input, padding.PKCS1v15(), digest)
            elif algorithm.startswith("PS") and isinstance(key, RSAPublicKey):
                key.verify(
                    signature,
                    signing_input,
                    padding.PSS(mgf=padding.MGF1(digest), salt_length=padding.PSS.DIGEST_LENGTH),
                    digest,
                )
            elif algorithm.startswith("ES") and isinstance(key, EllipticCurvePublicKey):
                # JWS carries r||s fixed-width; cryptography wants a DER signature.
                half = len(signature) // 2
                der = utils.encode_dss_signature(
                    int.from_bytes(signature[:half], "big"), int.from_bytes(signature[half:], "big")
                )
                key.verify(der, signing_input, ec.ECDSA(digest))
            else:
                raise AuthError("UNAUTHENTICATED", f"token algorithm {algorithm} does not match its key")
        except InvalidSignature as exc:
            raise AuthError("UNAUTHENTICATED", "token signature is not valid") from exc

    def authenticate(self, authorization: str | None) -> Principal:
        token = _bearer(authorization)
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("UNAUTHENTICATED", "token is not a signed JWT")
        try:
            header = json.loads(_b64url(parts[0]))
            claims = json.loads(_b64url(parts[1]))
            signature = _b64url(parts[2])
        except (ValueError, TypeError) as exc:
            raise AuthError("UNAUTHENTICATED", "token is not decodable") from exc

        algorithm = str(header.get("alg", ""))
        if algorithm not in self._SUPPORTED:
            # `none` and the HS family are the two classic JWT forgeries: one drops the
            # signature, the other lets a public key be used as an HMAC secret.
            raise AuthError("UNAUTHENTICATED", f"unsupported or unsafe token algorithm {algorithm!r}")
        kid = str(header.get("kid", ""))
        if not kid:
            raise AuthError("UNAUTHENTICATED", "token header has no key id")

        self._verify_signature(
            self._key_for(kid), algorithm, f"{parts[0]}.{parts[1]}".encode(), signature
        )

        now = time.time()
        issuer = str(claims.get("iss", "")).rstrip("/")
        if issuer != self.issuer:
            raise AuthError("UNAUTHENTICATED", "token was issued by a different issuer")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.audience not in [str(item) for item in audiences if item is not None]:
            raise AuthError("UNAUTHENTICATED", "token was not issued for this audience")
        expiry = claims.get("exp")
        if not isinstance(expiry, (int, float)):
            raise AuthError("UNAUTHENTICATED", "token has no expiry")
        if now > float(expiry) + self.leeway_seconds:
            raise AuthError("UNAUTHENTICATED", "token has expired")
        not_before = claims.get("nbf")
        if isinstance(not_before, (int, float)) and now < float(not_before) - self.leeway_seconds:
            raise AuthError("UNAUTHENTICATED", "token is not valid yet")

        subject = str(claims.get("sub", "")).strip()
        if not subject:
            raise AuthError("UNAUTHENTICATED", "token has no subject")

        raw_roles = claims.get(self.roles_claim) or []
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split()
        roles = {
            self.role_map[str(item)]
            for item in raw_roles
            if str(item) in self.role_map
        }
        if not roles:
            raise AuthError(
                "FORBIDDEN",
                f"no group in the token's {self.roles_claim!r} claim maps to a netCI role",
                status=403,
            )
        raw_teams = claims.get(self.teams_claim) or []
        if isinstance(raw_teams, str):
            raw_teams = raw_teams.split()
        return Principal(
            subject=subject,
            display_name=str(claims.get("name") or claims.get("preferred_username") or subject),
            email=str(claims.get("email") or ""),
            roles=frozenset(roles),
            teams=frozenset(str(team).strip() for team in raw_teams if str(team).strip()),
            method="oidc",
        )


# ------------------------------------------------------------------ composition root


def _configured_role_map() -> dict[str, Role]:
    """Map identity-provider groups to netCI roles: `platform-eng=platform-admin,...`."""

    raw = os.getenv("NETCI_OIDC_ROLE_MAP", "").strip()
    mapping: dict[str, Role] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        group, _, role = pair.partition("=")
        if not group.strip() or not role.strip():
            raise ValueError("NETCI_OIDC_ROLE_MAP entries must look like 'idp-group=netci-role'")
        try:
            mapping[group.strip()] = Role(role.strip())
        except ValueError as exc:
            known = ", ".join(sorted(item.value for item in Role))
            raise ValueError(f"NETCI_OIDC_ROLE_MAP: unknown netCI role {role!r}; known roles are {known}") from exc
    return mapping


def build_authenticator() -> Authenticator:
    """Select the authenticator from configuration. Called once, at import of main."""

    mode = os.getenv("NETCI_AUTH_MODE", "none").strip().lower()
    require_live_mode("NETCI_AUTH_MODE", mode, disabled={"", "none"})
    if mode in {"", "none"}:
        return OpenAuthenticator()
    if mode == "token":
        path = os.getenv("NETCI_AUTH_TOKENS_FILE", "").strip()
        if not path:
            raise ValueError("NETCI_AUTH_MODE=token requires NETCI_AUTH_TOKENS_FILE")
        return TokenAuthenticator(Path(path))
    if mode == "oidc":
        issuer = os.getenv("NETCI_OIDC_ISSUER", "").strip()
        audience = os.getenv("NETCI_OIDC_AUDIENCE", "").strip()
        jwks_url = os.getenv("NETCI_OIDC_JWKS_URL", "").strip()
        if not issuer or not audience:
            raise ValueError("NETCI_AUTH_MODE=oidc requires NETCI_OIDC_ISSUER and NETCI_OIDC_AUDIENCE")
        if not jwks_url:
            # The conventional location, so a standards-compliant provider needs one
            # variable rather than three.
            jwks_url = f"{issuer.rstrip('/')}/.well-known/jwks.json"
        role_map = _configured_role_map()
        if not role_map:
            raise ValueError(
                "NETCI_AUTH_MODE=oidc requires NETCI_OIDC_ROLE_MAP, e.g. "
                "'netci-admins=platform-admin,release-managers=reviewer,engineers=developer'"
            )
        return OidcAuthenticator(
            issuer=issuer,
            audience=audience,
            jwks_url=jwks_url,
            roles_claim=os.getenv("NETCI_OIDC_ROLES_CLAIM", "roles").strip() or "roles",
            teams_claim=os.getenv("NETCI_OIDC_TEAMS_CLAIM", "groups").strip() or "groups",
            role_map=role_map,
        )
    raise ValueError(f"NETCI_AUTH_MODE must be one of none, token, oidc (got {mode!r})")
