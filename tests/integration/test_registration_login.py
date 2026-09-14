"""Registration and login, end to end.

The real application over ASGI, the real dependency graph, real PostgreSQL and
real bcrypt. Accounts are created by calling `POST /auth/register` rather than
by a fixture that reaches around it, so what is tested is the thing that ships.

This is the sprint that makes authentication usable. Everything before it could
verify a token; nothing could produce one without a test harness minting it
directly.
"""

import time
import uuid
from typing import Any, AsyncIterator

import jwt as pyjwt
import pytest
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text

from backend.api.app import app
from backend.config.settings import get_settings
from backend.db.models import UserORM
from backend.db.repositories import UserRepository
from backend.security.api_security import get_current_user
from backend.security.passwords import hash_password, verify_password
from shared.models.credentials import AccessToken, UserCredentials
from shared.models.principal import Principal
from shared.models.user import UserRole

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

EMAIL = "researcher@example.com"
PASSWORD = "correct-horse-battery"
FULL_NAME = "A Researcher"

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
PROTECTED = "/api/v1/workspace/sessions"


@pytest.fixture(autouse=True)
async def _truncate_after_each_test() -> AsyncIterator[None]:
    """Registration commits for real, so each test has to clean up after itself.

    `committing_session` cannot do it here: these tests write through the
    application's own session, not the fixture's.
    """
    yield
    from backend.db.engine import get_engine

    async with get_engine().begin() as connection:
        await connection.execute(
            text("TRUNCATE document_chunks, documents, users RESTART IDENTITY CASCADE")
        )


def _registration(**overrides: str) -> dict[str, str]:
    return {"email": EMAIL, "password": PASSWORD, "full_name": FULL_NAME, **overrides}


async def _register(client: Any, **overrides: str) -> Any:
    return await client.post(REGISTER, json=_registration(**overrides))


async def _login(client: Any, email: str = EMAIL, password: str = PASSWORD) -> Any:
    return await client.post(LOGIN, json={"email": email, "password": password})


async def _stored(session: Any, email: str) -> UserORM:
    row = (
        await session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        )
    ).scalar_one()
    fetched: UserORM | None = await session.get(UserORM, row)
    assert fetched is not None
    return fetched


# --------------------------------------------------------------- registration


class TestRegistration:
    async def test_it_creates_an_account(self, api_client: Any) -> None:
        response = await _register(api_client)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["email"] == EMAIL
        assert body["full_name"] == FULL_NAME
        assert body["role"] == UserRole.RESEARCHER.value
        assert body["is_active"] is True
        assert uuid.UUID(body["id"])

    async def test_it_returns_201_and_not_200(self, api_client: Any) -> None:
        """A resource was created and the response says so.

        The scaffold declared this route with FastAPI's default 200 and a
        `LoginResponse` body — it was going to return a token from
        registration. Both are fixed here.
        """
        assert (await _register(api_client)).status_code == 201

    async def test_it_does_not_return_a_token(self, api_client: Any) -> None:
        """Creating an account and proving you can sign into it are separate.

        Keeping them apart is what lets an email-verification step slot in
        later without changing this contract.
        """
        body = (await _register(api_client)).json()

        assert "access_token" not in body
        assert "token" not in body
        assert not [k for k in body if "token" in k.lower()]

    async def test_the_response_carries_no_credential(self, api_client: Any) -> None:
        """The failure this guards is one word in a response model."""
        response = await _register(api_client)

        assert "password" not in response.text.lower()
        assert not [k for k in response.json() if "password" in k.lower()]

    async def test_the_stored_hash_is_bcrypt_and_verifies(
        self, api_client: Any, committing_session: Any
    ) -> None:
        await _register(api_client)

        row = await _stored(committing_session, EMAIL)

        assert row.password_hash is not None
        assert row.password_hash.startswith("$2b$")
        assert verify_password(PASSWORD, row.password_hash) is True

    async def test_the_plaintext_password_is_nowhere_in_the_row(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Every column of the row, not just the one we expect to be safe."""
        await _register(api_client)

        whole_row = (
            await committing_session.execute(
                text("SELECT users::text FROM users WHERE email = :e"), {"e": EMAIL}
            )
        ).scalar_one()

        assert PASSWORD not in whole_row

    async def test_two_registrations_produce_different_hashes(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Salting survives the trip through the endpoint."""
        await _register(api_client)
        await _register(api_client, email="second@example.com")

        first = await _stored(committing_session, EMAIL)
        second = await _stored(committing_session, "second@example.com")

        assert first.password_hash != second.password_hash

    async def test_the_write_is_committed_not_merely_flushed(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Read back through a *different* session than the one that wrote it.

        A flush without a commit is visible inside its own transaction and
        nowhere else, so this is what distinguishes the two.
        """
        await _register(api_client)

        count = (
            await committing_session.execute(
                text("SELECT count(*) FROM users WHERE email = :e"), {"e": EMAIL}
            )
        ).scalar_one()

        assert count == 1


class TestRegistrationReusesThePasswordPolicy:
    """S2.3's rules, not a second copy of them."""

    @pytest.mark.parametrize(
        "password",
        ["", "short", "a" * 11, "a" * 73, "valid-enough\x00tail"],
        ids=["empty", "short", "one-under-minimum", "over-72-bytes", "null-byte"],
    )
    async def test_a_password_the_policy_rejects_is_a_422(
        self, api_client: Any, password: str
    ) -> None:
        response = await _register(api_client, password=password)

        assert response.status_code == 422

    async def test_a_rejected_registration_creates_nothing(
        self, api_client: Any, committing_session: Any
    ) -> None:
        await _register(api_client, password="short")

        count = (
            await committing_session.execute(text("SELECT count(*) FROM users"))
        ).scalar_one()

        assert count == 0

    async def test_a_rejection_does_not_echo_the_password(
        self, api_client: Any
    ) -> None:
        """FastAPI puts the offending input in a 422 by default."""
        secret = "z" * 300

        response = await _register(api_client, password=secret)

        assert response.status_code == 422
        assert secret not in response.text

    async def test_the_422_says_which_rule_was_broken(self, api_client: Any) -> None:
        """Redacting the value must not also redact the reason.

        A client has to be able to tell the user *why* their password was
        refused, or the redaction has traded one bug for another.
        """
        response = await _register(api_client, password="short")

        detail = response.json()["detail"][0]
        assert detail["loc"] == ["body", "password"]
        assert "at least 12 characters" in detail["msg"]
        assert detail["input"] == "<redacted>"

    async def test_a_non_secret_field_still_reports_what_was_sent(
        self, api_client: Any
    ) -> None:
        """The control: the redaction is scoped to secrets, not applied to all 422s.

        Without this, a handler that blanked every `input` would pass the test
        above while making every other validation error useless to debug.
        """
        response = await _register(api_client, email="not-an-email")

        detail = response.json()["detail"][0]
        assert detail["loc"] == ["body", "email"]
        assert detail["input"] == "not-an-email"

    async def test_an_invalid_email_is_a_422(self, api_client: Any) -> None:
        assert (await _register(api_client, email="not-an-email")).status_code == 422

    async def test_unicode_normalisation_survives_the_round_trip(
        self, api_client: Any
    ) -> None:
        """Registered with a ligature, signed in with its expansion.

        The case that matters in the field: which Unicode form a keyboard emits
        is not something a user chooses or can see.
        """
        await _register(api_client, password="ﬁre-and-motion-xx")

        assert (
            await _login(api_client, password="fire-and-motion-xx")
        ).status_code == 200


class TestDuplicateEmail:
    async def test_a_second_registration_is_rejected(self, api_client: Any) -> None:
        await _register(api_client)

        response = await _register(api_client, full_name="Someone Else")

        assert response.status_code == 409

    async def test_it_does_not_create_a_second_account(
        self, api_client: Any, committing_session: Any
    ) -> None:
        await _register(api_client)
        await _register(api_client, full_name="Someone Else")

        count = (
            await committing_session.execute(
                text("SELECT count(*) FROM users WHERE email = :e"), {"e": EMAIL}
            )
        ).scalar_one()

        assert count == 1

    async def test_it_does_not_overwrite_the_first_account(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """The original password must still work after a failed duplicate."""
        await _register(api_client)
        await _register(api_client, password="different-password-entirely")

        row = await _stored(committing_session, EMAIL)

        assert verify_password(PASSWORD, row.password_hash) is True
        assert (
            verify_password("different-password-entirely", row.password_hash) is False
        )

    async def test_uniqueness_is_enforced_by_the_database(
        self, committing_session: Any
    ) -> None:
        """Not by a check-then-insert in the service, which is a race.

        Asserted at the constraint, because that is the only layer that can
        decide it atomically. Two concurrent registrations reach the insert
        together; only PostgreSQL can reject the second.
        """
        constraint = (
            await committing_session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'users'::regclass AND contype = 'u'"
                )
            )
        ).scalar_one()

        assert constraint == "uq_users_email"

    async def test_the_conflict_leaks_no_database_detail(self, api_client: Any) -> None:
        await _register(api_client)

        response = await _register(api_client)

        body = response.text.lower()
        for leak in (
            "constraint",
            "uq_users",
            "sql",
            "psycopg",
            "asyncpg",
            "traceback",
        ):
            assert leak not in body, f"409 mentions {leak!r}: {response.text}"


# ---------------------------------------------------------------------- login


class TestSuccessfulLogin:
    async def test_valid_credentials_return_a_token(self, api_client: Any) -> None:
        await _register(api_client)

        response = await _login(api_client)

        assert response.status_code == 200, response.text
        assert response.json()["access_token"]
        assert response.json()["token_type"] == "bearer"

    async def test_the_token_subject_is_the_registered_user(
        self, api_client: Any
    ) -> None:
        """`sub` is what S2.2 resolves against the database."""
        user_id = (await _register(api_client)).json()["id"]

        token = (await _login(api_client)).json()["access_token"]
        claims = pyjwt.decode(
            token,
            get_settings().JWT_SECRET,
            algorithms=[get_settings().JWT_ALGORITHM],
        )

        assert claims["sub"] == user_id

    async def test_login_does_not_write(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """No last-login column, no session row.

        A write on the sign-in path would let an unauthenticated request cause
        one.
        """
        await _register(api_client)
        before = await _stored(committing_session, EMAIL)
        updated_at, hashed = before.updated_at, before.password_hash

        await _login(api_client)
        committing_session.expunge_all()
        after = await _stored(committing_session, EMAIL)

        assert after.updated_at == updated_at
        assert after.password_hash == hashed

    async def test_no_response_ever_carries_the_hash(
        self, api_client: Any, committing_session: Any
    ) -> None:
        registered = await _register(api_client)
        row = await _stored(committing_session, EMAIL)
        assert row.password_hash is not None
        logged_in = await _login(api_client)

        assert row.password_hash not in registered.text
        assert row.password_hash not in logged_in.text


class TestTheTokenUsesTheExistingJwtContract:
    """S2.4 must not have grown a second token format (sprint brief §14)."""

    async def test_the_algorithm_is_hs256(self, api_client: Any) -> None:
        await _register(api_client)
        token = (await _login(api_client)).json()["access_token"]

        assert pyjwt.get_unverified_header(token)["alg"] == "HS256"

    async def test_it_carries_sub_email_role_and_exp_and_nothing_else(
        self, api_client: Any
    ) -> None:
        """Pinned in both directions. A new claim is a decision, not an accident."""
        await _register(api_client)
        token = (await _login(api_client)).json()["access_token"]

        claims = pyjwt.decode(token, get_settings().JWT_SECRET, algorithms=["HS256"])

        assert set(claims) == {"sub", "email", "role", "exp"}

    async def test_the_ttl_is_sixty_minutes(self, api_client: Any) -> None:
        """principles.md §6. No revocation store, so expiry is the only exit."""
        await _register(api_client)
        response = await _login(api_client)

        claims = pyjwt.decode(
            response.json()["access_token"],
            get_settings().JWT_SECRET,
            algorithms=["HS256"],
        )
        lifetime = claims["exp"] - time.time()

        assert response.json()["expires_in"] == 3600
        assert 3500 < lifetime <= 3600

    async def test_the_advertised_lifetime_matches_the_real_one(
        self, api_client: Any
    ) -> None:
        """`expires_in` and `exp` are derived from one setting, so they agree.

        Two reads of the same config could still drift if one were hard-coded;
        this is the test that would catch it.
        """
        await _register(api_client)
        response = await _login(api_client)

        claims = pyjwt.decode(
            response.json()["access_token"],
            get_settings().JWT_SECRET,
            algorithms=["HS256"],
        )

        assert abs((claims["exp"] - time.time()) - response.json()["expires_in"]) < 5

    async def test_no_refresh_token_is_issued(self, api_client: Any) -> None:
        """Removed in S2.1 and it stays removed."""
        await _register(api_client)
        body = (await _login(api_client)).json()

        assert "refresh_token" not in body
        assert set(body) == {"access_token", "token_type", "expires_in"}


class TestFailedLogin:
    """Every one of these is the same answer, from the same helper."""

    async def test_a_wrong_password_is_rejected(self, api_client: Any) -> None:
        await _register(api_client)

        assert (
            await _login(api_client, password="wrong-horse-battery")
        ).status_code == 401

    async def test_an_unknown_email_is_rejected(self, api_client: Any) -> None:
        assert (await _login(api_client, email="nobody@example.com")).status_code == 401

    async def test_an_account_with_no_password_is_rejected(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Every account created before S2.4 is in exactly this state.

        Created through the repository without a password, which is the path
        that produced them.
        """
        await UserRepository(committing_session).create(
            email="legacy@example.com", full_name="Legacy"
        )
        await committing_session.commit()

        response = await _login(api_client, email="legacy@example.com")

        assert response.status_code == 401

    async def test_a_malformed_stored_hash_is_rejected(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Not reachable through any write path, and still must not 500."""
        await _register(api_client)
        row = await _stored(committing_session, EMAIL)
        row.password_hash = "$2b$12$not-a-real-bcrypt-digest"
        await committing_session.commit()

        assert (await _login(api_client)).status_code == 401

    async def test_an_inactive_user_is_rejected(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Deactivation takes effect at the next sign-in, not at token expiry."""
        await _register(api_client)
        row = await _stored(committing_session, EMAIL)
        row.is_active = False
        await committing_session.commit()

        assert (await _login(api_client)).status_code == 401

    async def test_an_inactive_user_is_issued_no_token(
        self, api_client: Any, committing_session: Any
    ) -> None:
        await _register(api_client)
        row = await _stored(committing_session, EMAIL)
        row.is_active = False
        await committing_session.commit()

        assert "access_token" not in (await _login(api_client)).json()

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"email": EMAIL},
            {"password": PASSWORD},
            {"email": "not-an-email", "password": PASSWORD},
        ],
        ids=["empty", "no-password", "no-email", "bad-email"],
    )
    async def test_a_malformed_request_is_a_422_not_a_500(
        self, api_client: Any, body: dict[str, str]
    ) -> None:
        assert (await api_client.post(LOGIN, json=body)).status_code == 422


class TestFailedLoginRevealsNothing:
    async def test_every_failure_is_byte_identical(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """The central enumeration test.

        A client that could tell these apart could enumerate which addresses
        have accounts, which of those have passwords set, and which are
        disabled — without ever guessing a password.
        """
        await _register(api_client)
        await UserRepository(committing_session).create(
            email="nopassword@example.com", full_name="No Password"
        )
        disabled = await UserRepository(committing_session).create(
            email="disabled@example.com", full_name="Disabled"
        )
        await committing_session.commit()
        row = await committing_session.get(UserORM, uuid.UUID(disabled.id))
        row.password_hash = hash_password(PASSWORD)
        row.is_active = False
        await committing_session.commit()

        responses = [
            await _login(api_client, email="nobody@example.com"),
            await _login(api_client, password="wrong-horse-battery"),
            await _login(api_client, email="nopassword@example.com"),
            await _login(api_client, email="disabled@example.com"),
        ]

        assert {r.status_code for r in responses} == {401}
        assert len({r.text for r in responses}) == 1, [r.text for r in responses]

    async def test_the_failure_matches_a_forged_token_exactly(
        self, api_client: Any
    ) -> None:
        """One helper, so the two paths cannot drift apart."""
        credential_failure = await _login(api_client, email="nobody@example.com")
        token_failure = await api_client.get(
            PROTECTED, headers={"Authorization": "Bearer totally-fake-not-a-jwt"}
        )

        assert credential_failure.status_code == token_failure.status_code == 401
        assert credential_failure.text == token_failure.text
        assert (
            credential_failure.headers["www-authenticate"]
            == token_failure.headers["www-authenticate"]
        )

    async def test_no_failure_leaks_internal_detail(self, api_client: Any) -> None:
        response = await _login(api_client, email="nobody@example.com")

        body = response.text.lower()
        for leak in (
            "bcrypt",
            "hash",
            "sql",
            "select",
            "traceback",
            "inactive",
            "exist",
        ):
            assert leak not in body, f"401 mentions {leak!r}: {response.text}"


class TestFailedLoginCostsWhatSuccessCosts:
    """Uniform responses are worthless if the timing gives it away."""

    async def test_an_unknown_email_still_performs_a_bcrypt_round(
        self, api_client: Any
    ) -> None:
        """A lower bound, not an equality — equality would be flaky.

        Without the throwaway verification this path returns in well under a
        millisecond while a real account spends ~250ms at cost 12, which is
        trivially measurable over a network. 50ms is far below one bcrypt round
        and far above a bare database miss, so it separates the two without
        depending on how fast the machine is.
        """
        await _login(api_client, email="warmup@example.com")  # prime the dummy hash

        started = time.perf_counter()
        await _login(api_client, email="nobody@example.com")
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert elapsed_ms > 50, f"unknown-email login returned in {elapsed_ms:.1f}ms"

    async def test_an_account_without_a_password_also_costs_a_round(
        self, api_client: Any, committing_session: Any
    ) -> None:
        await UserRepository(committing_session).create(
            email="legacy@example.com", full_name="Legacy"
        )
        await committing_session.commit()
        await _login(api_client, email="warmup@example.com")

        started = time.perf_counter()
        await _login(api_client, email="legacy@example.com")
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert elapsed_ms > 50, f"null-hash login returned in {elapsed_ms:.1f}ms"


# ------------------------------------------------------- the headline journey


class TestRegisterThenLoginThenUseTheToken:
    """The whole point of S2.4, in one test (sprint brief §13).

    It proves the token login issues is the token S2.2 already knows how to
    verify — that this sprint plugged into the existing authentication
    architecture rather than building a parallel one beside it.
    """

    async def test_a_registered_user_can_sign_in_and_call_a_protected_route(
        self, api_client: Any
    ) -> None:
        assert (await api_client.get(PROTECTED)).status_code == 401

        registered = await _register(api_client)
        assert registered.status_code == 201

        token = (await _login(api_client)).json()["access_token"]

        response = await api_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code != 401, response.text

    async def test_the_token_resolves_to_the_correct_principal(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Through the real `get_current_user`, not a re-implementation.

        This is the join: S2.4 issues, S2.2 resolves, and the identity that
        comes out is the account that was registered.
        """
        registered = (await _register(api_client)).json()
        token = (await _login(api_client)).json()["access_token"]

        principal = await get_current_user(
            committing_session,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )

        assert isinstance(principal, Principal)
        assert principal.user_id == registered["id"]
        assert principal.email == EMAIL
        assert principal.role is UserRole.RESEARCHER
        assert principal.is_active is True

    async def test_deactivating_the_user_invalidates_an_issued_token(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """S2.2's active-user rule, reached through a real login.

        The token is still perfectly valid and correctly signed. The account is
        not — and with no revocation store, this database read is the only
        thing that can end the session early.
        """
        await _register(api_client)
        token = (await _login(api_client)).json()["access_token"]
        assert (
            await api_client.get(
                PROTECTED, headers={"Authorization": f"Bearer {token}"}
            )
        ).status_code != 401

        row = await _stored(committing_session, EMAIL)
        row.is_active = False
        await committing_session.commit()

        response = await api_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 401

    async def test_a_forged_token_is_still_refused_after_all_this(
        self, api_client: Any
    ) -> None:
        """S2.4 added a way in; it must not have added a way around."""
        await _register(api_client)

        for header in [
            "Bearer totally-fake-not-a-jwt",
            "Bearer aaa.bbb.ccc",
            "Basic x",
        ]:
            response = await api_client.get(
                PROTECTED, headers={"Authorization": header}
            )
            assert response.status_code == 401, header


# ----------------------------------------------------- the credential boundary


class TestTheCredentialTypeStaysInternal:
    async def test_no_route_declares_a_credential_as_its_response(self) -> None:
        """The one way a `password_hash` could still reach a client.

        `UserCredentials` exists so the hash has somewhere to travel that is not
        `User`. That only helps while nothing serialises it.
        """
        leaking = [
            f"{sorted(route.methods)} {route.path}"
            for route in app.routes
            if isinstance(route, APIRoute)
            and route.response_model in (UserCredentials, AccessToken)
        ]

        assert not leaking, leaking

    async def test_the_credential_lookup_is_the_only_source_of_the_hash(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """`get_by_email` and `get_by_id` must not have grown one."""
        await _register(api_client)
        users = UserRepository(committing_session)

        by_email = await users.get_by_email(EMAIL)
        assert by_email is not None
        by_id = await users.get_by_id(by_email.id)
        assert by_id is not None
        credentials = await users.get_credentials_by_email(EMAIL)
        assert credentials is not None

        assert not hasattr(by_email, "password_hash")
        assert not hasattr(by_id, "password_hash")
        assert credentials.password_hash is not None

    async def test_the_credential_carries_only_what_authentication_needs(
        self,
    ) -> None:
        """A credential lookup must not become a user read with a secret on it."""
        assert set(UserCredentials.model_fields) == {
            "user_id",
            "email",
            "password_hash",
            "role",
            "is_active",
        }

    async def test_a_credential_cannot_be_mutated_after_it_is_read(self) -> None:
        credentials = UserCredentials(
            user_id=str(uuid.uuid4()),
            email=EMAIL,
            password_hash=None,
            role=UserRole.VIEWER,
            is_active=True,
        )

        with pytest.raises(Exception):
            credentials.role = UserRole.ADMIN
