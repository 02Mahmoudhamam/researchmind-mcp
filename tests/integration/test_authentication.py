"""Fail-closed authentication, end to end.

The real application, the real dependency graph, the real PostgreSQL. Tokens
are signed by the real `JWTHandler` against the settings the application itself
reads, and users are created through the real `UserRepository` and really
committed — so a token is accepted here for the same reasons it would be
accepted in production, and rejected for the same reasons.

The defect this file exists to prevent is recorded in
`docs/security/principles.md` §1 and was verified by execution on the
pre-S2.2 baseline:

    no Authorization header        -> 403 Not authenticated
    Bearer totally-fake-not-a-jwt  -> 200, user is None      <- accepted

`get_current_user` was annotated `-> User` over a body of `...`, so it returned
None for every request and every forged token reached the route.
"""

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from fastapi.exceptions import ResponseValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from backend.api.app import app
from backend.config.settings import Settings, get_settings
from backend.db.models import UserORM
from backend.db.repositories import UserRepository
from backend.security.api_security import get_current_user
from backend.security.authentication import AuthenticationError, resolve_principal
from backend.security.jwt_handler import JWTHandler
from shared.models.principal import Principal
from shared.models.user import User, UserRole

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

# Deliberately repetitive, for the reason recorded in tests/unit/test_jwt.py:
# a high-entropy string beside `SECRET` is what a leaked credential looks like,
# and CI's gitleaks scan was right to say so the first time.
WRONG_SECRET = "wrong-secret-wrong-secret-wrong-secret"


# ---------------------------------------------------------------- route table


def _dependency_names(route: APIRoute) -> set[str]:
    """Every dependency callable reachable from a route, by name.

    Walks the whole tree rather than the route's immediate dependencies,
    because `get_current_user` may be reached through another dependency — and
    a check that only looked one level down would report a route as
    unprotected the moment someone wrapped the guard.
    """
    names: set[str] = set()

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            if sub.call is not None:
                names.add(getattr(sub.call, "__name__", type(sub.call).__name__))
            walk(sub)

    walk(route.dependant)
    return names


API_ROUTES: list[APIRoute] = [r for r in app.routes if isinstance(r, APIRoute)]

# Routes that may legitimately be reached without credentials.
#
# An allowlist, not a derivation. If this were computed from "which routes lack
# the dependency", a route that lost its guard would simply drop out of the
# protected set and every test below would keep passing — the exact failure
# §16 asks to be made impossible. Written this way, a new route is protected by
# default and making one public is an edit somebody has to justify in review.
PUBLIC_PATHS = frozenset(
    {
        # Probed by container orchestration, which carries no token.
        "/health",
        "/health/ready",
        # Returns {} today. Metrics exposure is M9's to decide, and this
        # records that it is currently unauthenticated rather than hiding it.
        "/metrics",
        # The two endpoints that *issue* credentials. Requiring credentials to
        # obtain credentials is the one place fail-closed cannot apply.
        # Both are stubs until S2.4.
        "/api/v1/auth/register",
        "/api/v1/auth/login",
    }
)

PROTECTED_ROUTES = [r for r in API_ROUTES if r.path not in PUBLIC_PATHS]


def _request_args(route: APIRoute) -> tuple[str, str]:
    """A concrete method and URL for a route, with path params filled in."""
    method = next(m for m in sorted(route.methods) if m not in {"HEAD", "OPTIONS"})
    path = route.path
    for name in route.param_convertors:
        path = path.replace(f"{{{name}}}", str(uuid.uuid4()))
    return method, path


def _route_id(route: APIRoute) -> str:
    method, path = _request_args(route)
    return f"{method} {path}"


# --------------------------------------------------------------------- tokens


def _sign(**claims: object) -> str:
    """Sign arbitrary claims with the key the application actually verifies."""
    settings = get_settings()
    return pyjwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _token_for(user: User, **overrides: object) -> str:
    """A genuine access token for a user, via the production code path."""
    handler = JWTHandler()
    if overrides:
        settings = get_settings()
        payload: dict[str, object] = {
            "sub": user.id,
            "email": user.email,
            "role": user.role.value,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
        }
        payload.update(overrides)
        return pyjwt.encode(
            payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
        )
    return handler.create_access_token(user.id, user.email, user.role)


def _tamper(token: str, **claims: object) -> str:
    """Rewrite a signed token's claims and keep the original signature.

    The realistic forgery: the attacker has a valid token of their own and
    edits it. Truncating or corrupting the string tests the parser; this tests
    the signature.
    """
    header, payload, signature = token.split(".")
    padded = payload + "=" * (-len(payload) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded))
    decoded.update(claims)
    raw = json.dumps(decoded, separators=(",", ":")).encode()
    forged = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- fixtures


async def _create(
    session: Any, email: str, role: UserRole = UserRole.RESEARCHER
) -> User:
    """A committed user, created through the repository M1 already ships.

    No password of any kind: the schema has no column for one and S2.2 does not
    add it (sprint brief §24). Authentication here proves a *token*, and a
    token is minted from a user id — which is exactly why S2.2 can close the
    bypass before S2.4 implements login.
    """
    user = await UserRepository(session).create(
        email=email, full_name="Test Researcher", role=role
    )
    await session.commit()
    return user


@pytest.fixture
async def active_user(committing_session: Any) -> User:
    return await _create(committing_session, "active@example.com")


@pytest.fixture
async def inactive_user(committing_session: Any) -> User:
    """A user deactivated *after* their token was issued.

    Deactivated through the ORM rather than the repository: `UserRepository`
    has no setter for it, and M1's repositories stay unchanged this sprint.
    """
    user = await _create(committing_session, "inactive@example.com")
    row = await committing_session.get(UserORM, uuid.UUID(user.id))
    row.is_active = False
    await committing_session.commit()
    return user


@pytest.fixture
async def deleted_user(committing_session: Any) -> User:
    """A user whose row is gone after their token was issued."""
    user = await _create(committing_session, "deleted@example.com")
    row = await committing_session.get(UserORM, uuid.UUID(user.id))
    await committing_session.delete(row)
    await committing_session.commit()
    return user


@pytest.fixture
async def unauthenticated_requests(api_client: Any) -> AsyncIterator[Any]:
    """Send a set of headers to every protected route and collect the codes."""

    async def send(headers: dict[str, str]) -> dict[str, int]:
        results: dict[str, int] = {}
        for route in PROTECTED_ROUTES:
            method, path = _request_args(route)
            response = await api_client.request(
                method, path, headers=headers, json={} if method == "POST" else None
            )
            results[_route_id(route)] = response.status_code
        return results

    yield send


# ------------------------------------------------------- the route table itself


class TestTheProtectedRouteTableIsWhatWeThinkItIs:
    def test_there_are_protected_routes_to_test(self) -> None:
        """Without this, every parametrised test below could pass on zero cases."""
        assert len(PROTECTED_ROUTES) == 9, [_route_id(r) for r in PROTECTED_ROUTES]

    @pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=_route_id)
    def test_every_non_public_route_requires_authentication(
        self, route: APIRoute
    ) -> None:
        """A route is protected unless it is on the public allowlist.

        This is the structural half of the headline test. The behavioural half
        proves the guard rejects forged tokens; this one proves the guard is
        *there* — so a route added in M3 without `Depends(get_current_user)`
        fails here rather than shipping open.
        """
        assert "get_current_user" in _dependency_names(route)

    @pytest.mark.parametrize(
        "path", sorted(PUBLIC_PATHS), ids=lambda p: p.replace("/", "_")
    )
    def test_every_allowlisted_path_actually_exists(self, path: str) -> None:
        """A stale allowlist entry would silently excuse a future route.

        If `/api/v1/auth/login` were renamed, the old entry would sit here
        excusing nothing until a new route happened to claim that path.
        """
        assert path in {r.path for r in API_ROUTES}


# -------------------------------------------------- §15 A-L: the failure modes


class TestCredentialsThatAreNotTokens:
    async def test_a_missing_authorization_header_is_rejected(
        self, unauthenticated_requests: Any
    ) -> None:
        """§15 A. Previously 403 — the right refusal under the wrong code."""
        assert set((await unauthenticated_requests({})).values()) == {401}

    async def test_a_non_bearer_scheme_is_rejected(
        self, unauthenticated_requests: Any
    ) -> None:
        """§15 B. Basic credentials are credentials, but not for this scheme."""
        basic = base64.b64encode(b"user:password").decode()
        results = await unauthenticated_requests({"Authorization": f"Basic {basic}"})

        assert set(results.values()) == {401}

    @pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Bearer  "])
    async def test_a_bearer_scheme_with_no_token_is_rejected(
        self, unauthenticated_requests: Any, header: str
    ) -> None:
        """§15 C. An empty credential is not a credential."""
        assert set(
            (await unauthenticated_requests({"Authorization": header})).values()
        ) == {401}

    async def test_a_completely_fake_bearer_token_is_rejected(
        self, unauthenticated_requests: Any
    ) -> None:
        """§15 D — the central regression test.

        This exact string returned 200 on four routes before S2.2, and 422 or a
        response-validation crash on the rest: in every case authentication had
        already succeeded and the request was inside the handler.
        """
        results = await unauthenticated_requests(_bearer("totally-fake-not-a-jwt"))

        assert set(results.values()) == {401}, results

    @pytest.mark.parametrize(
        "token",
        ["aaa.bbb.ccc", "not.a.jwt.at.all", "eyJhbGciOiJIUzI1NiJ9", "..", ""],
        ids=[
            "three-segments",
            "five-segments",
            "header-only",
            "empty-segments",
            "empty",
        ],
    )
    async def test_a_malformed_jwt_is_rejected(
        self, unauthenticated_requests: Any, token: str
    ) -> None:
        """§15 E."""
        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}


class TestTokensThatFailVerification:
    async def test_an_expired_token_is_rejected(
        self, unauthenticated_requests: Any, active_user: User
    ) -> None:
        """§15 F. Correctly signed, for a real and active user — but over.

        With no server-side revocation (principles.md §6) expiry is the only
        thing that ends a session, so this is the one that must not be lenient.
        """
        token = JWTHandler().create_access_token(
            active_user.id,
            active_user.email,
            active_user.role,
            expires_delta=timedelta(minutes=-1),
        )

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}

    async def test_a_tampered_token_is_rejected(
        self, unauthenticated_requests: Any, active_user: User
    ) -> None:
        """§15 G. A real token whose holder edited themselves into an admin."""
        forged = _tamper(_token_for(active_user), role=UserRole.ADMIN.value)

        assert set((await unauthenticated_requests(_bearer(forged))).values()) == {401}

    async def test_a_token_signed_with_the_wrong_secret_is_rejected(
        self, unauthenticated_requests: Any, active_user: User
    ) -> None:
        """§15 H. Well-formed, unexpired, correct claims, wrong key."""
        handler = JWTHandler(
            settings=Settings(
                ANTHROPIC_API_KEY="test-key",
                SECRET_KEY=WRONG_SECRET,
                JWT_SECRET=WRONG_SECRET,
                APP_ENV="development",
            )
        )
        token = handler.create_access_token(
            active_user.id, active_user.email, active_user.role
        )

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}

    async def test_a_token_without_a_subject_is_rejected(
        self, unauthenticated_requests: Any, active_user: User
    ) -> None:
        """§15 I. Signed by us, and still nobody.

        `require: ["sub"]` is what makes this structural rather than a
        KeyError later on.
        """
        token = _sign(
            email=active_user.email,
            role=active_user.role.value,
            exp=datetime.now(timezone.utc) + timedelta(minutes=60),
        )

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}

    async def test_a_token_without_an_expiry_is_rejected(
        self, unauthenticated_requests: Any, active_user: User
    ) -> None:
        """Not in §15, and the same class of defect: a token that never ends."""
        token = _sign(sub=active_user.id, email=active_user.email, role="researcher")

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}


class TestTokensWhoseSubjectDoesNotResolve:
    """A valid signature proves the token's origin, not the account's standing."""

    async def test_a_token_for_a_nonexistent_user_is_rejected(
        self, unauthenticated_requests: Any
    ) -> None:
        """§15 J. Signed by us, for a subject that was never in the table."""
        token = _sign(
            sub=str(uuid.uuid4()),
            email="ghost@example.com",
            role="admin",
            exp=datetime.now(timezone.utc) + timedelta(minutes=60),
        )

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}

    async def test_a_token_for_a_deleted_user_is_rejected(
        self, unauthenticated_requests: Any, deleted_user: User
    ) -> None:
        """The same, for a row that existed when the token was minted."""
        token = _sign(
            sub=deleted_user.id,
            email=deleted_user.email,
            role=deleted_user.role.value,
            exp=datetime.now(timezone.utc) + timedelta(minutes=60),
        )

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}

    async def test_a_token_for_a_deactivated_user_is_rejected(
        self, unauthenticated_requests: Any, inactive_user: User
    ) -> None:
        """§15 K. The token is perfect; the account is not.

        This is the case that forces authentication to read the database at all
        instead of trusting the claims it has just verified.
        """
        token = _token_for(inactive_user)

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}

    async def test_a_subject_that_is_not_a_uuid_is_a_401_not_a_500(
        self, unauthenticated_requests: Any
    ) -> None:
        """A hostile `sub` must be a refusal, not a crash.

        `parse_id` returns None rather than raising precisely so that a
        subject like this is indistinguishable from "no such user".
        """
        token = _sign(
            sub="../../etc/passwd",
            email="x@example.com",
            role="admin",
            exp=datetime.now(timezone.utc) + timedelta(minutes=60),
        )

        assert set((await unauthenticated_requests(_bearer(token))).values()) == {401}


# --------------------------------------------------------- §15 L: the happy path


class TestAValidTokenForAnActiveUser:
    async def test_it_produces_a_principal(
        self, committing_session: Any, active_user: User
    ) -> None:
        """§15 L, through the real dependency with real arguments.

        `get_current_user` is called directly rather than through a synthetic
        probe route: it *is* the production function, and calling it this way
        lets the Principal itself be asserted rather than inferred from a
        status code.
        """
        principal = await get_current_user(
            committing_session,
            HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=_token_for(active_user)
            ),
        )

        assert isinstance(principal, Principal)
        assert principal.user_id == active_user.id
        assert principal.email == active_user.email
        assert principal.role is active_user.role
        assert principal.is_active is True

    @pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=_route_id)
    async def test_it_reaches_every_protected_route(
        self, api_client: Any, active_user: User, route: APIRoute
    ) -> None:
        """The negative control for the whole file.

        Every test above asserts 401. Without this one they would all pass
        against a `get_current_user` that rejected *everything* — which is
        fail-closed, and also a broken application. Bodies are still stubs, so
        the assertion is "not a 401", not "200".
        """
        method, path = _request_args(route)

        try:
            response = await api_client.request(
                method,
                path,
                headers=_bearer(_token_for(active_user)),
                json={} if method == "POST" else None,
            )
        except ResponseValidationError:
            # The handler ran and failed on its own emptiness. Two document
            # routes declare a `response_model` over a body that is still
            # `...`, so FastAPI validates None against DocumentResponse and
            # raises — *after* the dependency resolved, which is the only thing
            # this test is asking about. S2.2 adds authentication, not route
            # bodies (brief §10), so this is the expected shape of "reached".
            return

        assert response.status_code != 401, response.text

    async def test_the_role_comes_from_the_database_not_the_token(
        self, committing_session: Any, active_user: User
    ) -> None:
        """A token cannot promote its holder.

        The user is a RESEARCHER; the token says ADMIN and is signed correctly,
        which is what a token issued before a demotion looks like. PostgreSQL
        is the sole authority for what a user is (principles.md §3), so the
        Principal must carry the row's role.

        Without this, deactivating an admin would take effect immediately but
        demoting one would not take effect until their token expired.
        """
        token = _token_for(active_user, role=UserRole.ADMIN.value)

        principal = await get_current_user(
            committing_session,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )

        assert active_user.role is UserRole.RESEARCHER
        assert principal.role is UserRole.RESEARCHER

    async def test_the_email_comes_from_the_database_not_the_token(
        self, committing_session: Any, active_user: User
    ) -> None:
        """Same rule, and it matters because email is an identifier here."""
        token = _token_for(active_user, email="attacker@example.com")

        principal = await get_current_user(
            committing_session,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        )

        assert principal.email == active_user.email


# --------------------------------------------------------------- the error model


class TestTheErrorModel:
    async def test_the_refusal_is_401_with_a_bearer_challenge(
        self, api_client: Any
    ) -> None:
        """RFC 7235 §4.1 — a 401 says how to authenticate."""
        response = await api_client.get(
            "/api/v1/workspace/sessions", headers=_bearer("totally-fake-not-a-jwt")
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": "Bearer totally-fake-not-a-jwt"},
            {"Authorization": "Bearer aaa.bbb.ccc"},
        ],
        ids=["missing", "wrong-scheme", "fake", "malformed"],
    )
    async def test_every_refusal_is_byte_identical(
        self, api_client: Any, headers: dict[str, str]
    ) -> None:
        """Nothing in the response says which check failed.

        An error that distinguishes "unknown user" from "bad token" tells an
        attacker which half of their guess was right (principles.md §7).
        """
        response = await api_client.get("/api/v1/workspace/sessions", headers=headers)

        assert response.status_code == 401
        assert response.json() == {"detail": "Could not validate credentials"}

    async def test_no_response_leaks_why_authentication_failed(
        self,
        api_client: Any,
        inactive_user: User,
        deleted_user: User,
        active_user: User,
    ) -> None:
        """Unknown, deleted and deactivated must be one answer from outside."""
        tokens = {
            "unknown": _sign(
                sub=str(uuid.uuid4()),
                email="ghost@example.com",
                role="researcher",
                exp=datetime.now(timezone.utc) + timedelta(minutes=60),
            ),
            "deleted": _token_for(deleted_user),
            "inactive": _token_for(inactive_user),
            "expired": JWTHandler().create_access_token(
                active_user.id,
                active_user.email,
                active_user.role,
                expires_delta=timedelta(minutes=-1),
            ),
        }

        bodies = set()
        for token in tokens.values():
            response = await api_client.get(
                "/api/v1/workspace/sessions", headers=_bearer(token)
            )
            assert response.status_code == 401
            bodies.add(response.text)

        assert len(bodies) == 1, "the response distinguishes failure categories"

    async def test_no_response_carries_jwt_or_database_detail(
        self, api_client: Any
    ) -> None:
        """No library names, no claim names, no SQL, no paths."""
        response = await api_client.get(
            "/api/v1/workspace/sessions", headers=_bearer("aaa.bbb.ccc")
        )

        body = response.text.lower()
        for leak in (
            "jwt",
            "signature",
            "sql",
            "select",
            "traceback",
            "postgres",
            "sub",
        ):
            assert leak not in body, f"response mentions {leak!r}: {response.text}"

    async def test_the_reason_is_still_available_internally(
        self, committing_session: Any, inactive_user: User, deleted_user: User
    ) -> None:
        """Identical outside, distinguishable inside.

        The resolver knows exactly what went wrong — that is what a log needs.
        The HTTP adapter drops it. This asserts the drop is the adapter's doing
        and not an absence of information.
        """
        users = UserRepository(committing_session)

        with pytest.raises(AuthenticationError) as inactive:
            await resolve_principal(_token_for(inactive_user), users)
        with pytest.raises(AuthenticationError) as missing:
            await resolve_principal(_token_for(deleted_user), users)

        assert inactive.value.reason != missing.value.reason
        assert "not active" in inactive.value.reason

    async def test_the_http_adapter_raises_401_not_403(
        self, committing_session: Any
    ) -> None:
        """403 is for an authenticated caller without permission — S2.5's code.

        Asserted on the exception rather than the response so that a future
        exception handler cannot make this pass by rewriting the status.
        """
        with pytest.raises(HTTPException) as raised:
            await get_current_user(
                committing_session,
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="nope"),
            )

        assert raised.value.status_code == 401
        assert raised.value.headers == {"WWW-Authenticate": "Bearer"}


class TestRejectionCostsNothing:
    async def test_an_unauthenticated_request_opens_no_database_connection(
        self, api_client: Any
    ) -> None:
        """Unauthenticated traffic must not be able to exhaust the pool.

        `get_current_user` depends on `get_db_session`, so a session object is
        built for every request including the refused ones. SQLAlchemy's
        AsyncSession is lazy — it acquires a connection on first use — so a
        request rejected before any query runs costs no pool slot.

        That is a property of how the dependency is written, not a promise
        SQLAlchemy makes to us, and it would quietly stop holding the day
        someone adds `await session.begin()` to the dependency.
        """
        from sqlalchemy.pool import QueuePool

        from backend.db.engine import get_engine

        def pool() -> QueuePool:
            # Narrowed rather than cast. `Engine.pool` is typed as the abstract
            # `Pool`, and only a pool that actually pools has these counters —
            # under NullPool they would not exist and this test would be
            # asserting nothing, so the isinstance check is part of the claim.
            current = get_engine().pool
            assert isinstance(current, QueuePool)
            return current

        assert pool().checkedin() == 0 and pool().checkedout() == 0

        for _ in range(20):
            response = await api_client.get(
                "/api/v1/workspace/sessions",
                headers=_bearer("totally-fake-not-a-jwt"),
            )
            assert response.status_code == 401

        assert pool().checkedout() == 0
        assert pool().checkedin() == 0, "a refused request checked out a connection"
