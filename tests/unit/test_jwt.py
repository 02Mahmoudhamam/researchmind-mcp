"""Access-token mechanics.

Real PyJWT throughout — no mocking of the library under test. Several of these
craft malicious tokens by hand, because the attacks that matter here are shapes
a well-behaved encoder will not produce.

Authentication — resolving a token's subject to an active user — is Sprint
M2/S2.2. Nothing here touches the database.
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from backend.config.settings import Settings
from backend.security.jwt_handler import JWTHandler, TokenError
from shared.models.user import TokenData, UserRole

SECRET = "a-test-signing-key-that-is-long-enough-32"
OTHER_SECRET = "a-different-signing-key-also-long-enough"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "ANTHROPIC_API_KEY": "test-key",
        "SECRET_KEY": SECRET,
        "JWT_SECRET": SECRET,
        "APP_ENV": "development",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def _handler(**overrides: object) -> JWTHandler:
    return JWTHandler(settings=_settings(**overrides))


def _unsigned(payload: dict[str, object], alg: str = "none") -> str:
    """Hand-craft a token with an arbitrary header and an empty signature."""

    def seg(data: dict[str, object]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': alg, 'typ': 'JWT'})}.{seg(payload)}."


class TestCreation:
    def test_a_token_is_issued_and_verifies(self) -> None:
        handler = _handler()

        token = handler.create_access_token("u-1", "a@example.com", UserRole.RESEARCHER)
        claims = handler.verify_token(token)

        assert isinstance(claims, TokenData)
        assert claims.user_id == "u-1"
        assert claims.email == "a@example.com"
        assert claims.role is UserRole.RESEARCHER

    def test_exp_is_always_present(self) -> None:
        """A token without an expiry never stops being valid."""
        token = _handler().create_access_token("u-1", "a@example.com", UserRole.VIEWER)

        raw = pyjwt.decode(token, options={"verify_signature": False})

        assert "exp" in raw

    def test_the_expiry_matches_the_configured_ttl(self) -> None:
        """60 minutes, per principles.md §6."""
        token = _handler().create_access_token("u-1", "a@example.com", UserRole.VIEWER)

        raw = pyjwt.decode(token, options={"verify_signature": False})
        ttl = datetime.fromtimestamp(raw["exp"], tz=timezone.utc) - datetime.now(
            timezone.utc
        )

        assert 59 <= ttl.total_seconds() / 60 <= 60

    def test_an_explicit_expiry_overrides_the_default(self) -> None:
        token = _handler().create_access_token(
            "u-1", "a@example.com", UserRole.VIEWER, expires_delta=timedelta(minutes=5)
        )

        raw = pyjwt.decode(token, options={"verify_signature": False})
        ttl = datetime.fromtimestamp(raw["exp"], tz=timezone.utc) - datetime.now(
            timezone.utc
        )

        assert 4 <= ttl.total_seconds() / 60 <= 5

    def test_the_payload_carries_nothing_it_does_not_need(self) -> None:
        """Minimal claims.

        No `iat` or `jti`: there is no revocation store to compare them against,
        and a claim nothing reads drifts out of step with whatever eventually
        does. Notably, no password, no hash, no secret.
        """
        token = _handler().create_access_token("u-1", "a@example.com", UserRole.ADMIN)

        raw = pyjwt.decode(token, options={"verify_signature": False})

        assert set(raw) == {"sub", "email", "role", "exp"}

    def test_the_subject_uses_the_registered_sub_claim(self) -> None:
        token = _handler().create_access_token("u-42", "a@example.com", UserRole.VIEWER)

        raw = pyjwt.decode(token, options={"verify_signature": False})

        assert raw["sub"] == "u-42"


class TestRejection:
    def test_an_expired_token_is_rejected(self) -> None:
        handler = _handler()
        token = handler.create_access_token(
            "u-1", "a@example.com", UserRole.VIEWER, expires_delta=timedelta(seconds=-1)
        )

        with pytest.raises(TokenError):
            handler.verify_token(token)

    def test_a_token_signed_with_another_secret_is_rejected(self) -> None:
        issued_elsewhere = _handler(JWT_SECRET=OTHER_SECRET).create_access_token(
            "u-1", "a@example.com", UserRole.ADMIN
        )

        with pytest.raises(TokenError):
            _handler().verify_token(issued_elsewhere)

    def test_a_tampered_payload_is_rejected(self) -> None:
        """Flip a claim and keep the original signature."""
        handler = _handler()
        token = handler.create_access_token("u-1", "a@example.com", UserRole.VIEWER)
        header, payload, signature = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["role"] = UserRole.ADMIN.value
        forged = (
            base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
        )

        with pytest.raises(TokenError):
            handler.verify_token(f"{header}.{forged}.{signature}")

    @pytest.mark.parametrize(
        "token",
        ["", "not-a-token", "a.b", "a.b.c", "....", "Bearer something"],
        ids=["empty", "plain", "two-parts", "garbage-segments", "dots", "with-scheme"],
    )
    def test_a_malformed_token_is_rejected(self, token: str) -> None:
        with pytest.raises(TokenError):
            _handler().verify_token(token)

    def test_a_token_without_exp_is_rejected(self) -> None:
        """`require=["exp"]` makes the expiry structural, not conventional."""
        no_expiry = pyjwt.encode(
            {"sub": "u-1", "email": "a@example.com", "role": "admin"},
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            _handler().verify_token(no_expiry)

    def test_a_token_without_sub_is_rejected(self) -> None:
        no_subject = pyjwt.encode(
            {
                "email": "a@example.com",
                "role": "admin",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            _handler().verify_token(no_subject)


class TestAlgorithmPinning:
    def test_an_alg_none_token_is_rejected(self) -> None:
        """The canonical forgery: a token that asks not to be verified.

        Hand-crafted, because PyJWT will not encode one.
        """
        unsigned = _unsigned(
            {
                "sub": "u-1",
                "email": "attacker@example.com",
                "role": "admin",
                "exp": int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                ),
            }
        )

        with pytest.raises(TokenError):
            _handler().verify_token(unsigned)

    def test_a_token_signed_with_a_different_hmac_algorithm_is_rejected(self) -> None:
        """Algorithm confusion, symmetric flavour.

        The token's header says HS512; the accepted list says HS256. The list we
        pass wins — the header is not consulted.
        """
        other_alg = pyjwt.encode(
            {
                "sub": "u-1",
                "email": "a@example.com",
                "role": "admin",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            # 64 bytes: SHA512 wants at least that (RFC 7518 §3.2), and a
            # too-short key here would make PyJWT warn about the wrong thing.
            SECRET * 2,
            algorithm="HS512",
        )

        with pytest.raises(TokenError):
            _handler(JWT_ALGORITHM="HS256").verify_token(other_alg)

    def test_a_token_claiming_an_asymmetric_algorithm_is_rejected(self) -> None:
        """The public-key-as-HMAC-secret confusion, refused at the header."""
        claiming_rs256 = _unsigned(
            {
                "sub": "u-1",
                "email": "attacker@example.com",
                "role": "admin",
                "exp": int(
                    (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
                ),
            },
            alg="RS256",
        )

        with pytest.raises(TokenError):
            _handler().verify_token(claiming_rs256)

    def test_the_configured_algorithm_cannot_be_none(self) -> None:
        """Reading the algorithm from settings is only safe because of this.

        Without the allowlist, `JWT_ALGORITHM=none` would be a supported way to
        turn signature verification off.
        """
        with pytest.raises(ValueError, match="JWT_ALGORITHM"):
            _settings(JWT_ALGORITHM="none")

    @pytest.mark.parametrize("algorithm", ["RS256", "ES256", "PS256", "HS128", ""])
    def test_asymmetric_and_unknown_algorithms_are_refused_by_config(
        self, algorithm: str
    ) -> None:
        with pytest.raises(ValueError, match="JWT_ALGORITHM"):
            _settings(JWT_ALGORITHM=algorithm)


class TestClaimIntegrity:
    def test_a_signed_token_with_an_unknown_role_is_rejected(self) -> None:
        """Signed by us is not the same as meaningful to us."""
        strange_role = pyjwt.encode(
            {
                "sub": "u-1",
                "email": "a@example.com",
                "role": "superadmin",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            _handler().verify_token(strange_role)

    def test_a_signed_token_missing_email_is_rejected(self) -> None:
        no_email = pyjwt.encode(
            {
                "sub": "u-1",
                "role": "admin",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            _handler().verify_token(no_email)


class TestNoImportTimeFreeze:
    def test_the_handler_reads_settings_lazily(self) -> None:
        """Construction must not capture configuration.

        `vector_db/qdrant/config.py` binds settings as class attributes at
        import and is untestable as a result. A JWTHandler built at module
        scope would sign with whatever secret happened to be loaded first.
        """
        handler = JWTHandler()

        assert handler._override is None

    def test_no_module_level_settings_call(self) -> None:
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[2]
            / "backend"
            / "security"
            / "jwt_handler.py"
        ).read_text(encoding="utf-8")

        assert "\nsettings = get_settings()" not in source
