"""Authorisation, ownership and identity propagation, end to end.

The real application, the real dependency graph, real PostgreSQL. Users are
created through `UserRepository` with the role under test, and tokens are
minted by the production `JWTHandler` — the same path S2.2's tests use — except
in the journeys that go through `/auth/register` and `/auth/login` on purpose.

Three checks, three answers, and every test here is about keeping them apart:

    authentication   who is this?            401   get_current_user
    authorisation    may this role do it?    403   require_permission
    ownership        is this row theirs?     404   the repository's SQL

The failure this file exists to prevent is any one of them standing in for
another: a 403 for a missing token, a 401 for a viewer, a permission that
lets an administrator read someone else's manuscript.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update

from backend.api.app import app
from backend.config.settings import get_settings
from backend.db.models import UserORM
from backend.db.repositories import DocumentRepository, UserRepository
from backend.security.api_security import (
    RequirePermission,
    RequireRole,
    require_permission,
    require_role,
)
from backend.security.jwt_handler import JWTHandler
from backend.security.rbac import Permission, RBACPolicy
from backend.services.document_service import DocumentService
from shared.models.document import DocumentType
from shared.models.principal import Principal
from shared.models.user import User, UserRole
from tests.pdfs import make_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

V, R, A = UserRole.VIEWER, UserRole.RESEARCHER, UserRole.ADMIN

FORBIDDEN = {"detail": "You do not have permission to perform this action."}
UNAUTHENTICATED = {"detail": "Could not validate credentials"}
NOT_FOUND = {"detail": "Document not found"}


# ------------------------------------------------------------ the route matrix


@dataclass(frozen=True)
class Route:
    """One protected route, and who may call it.

    `allowed` is written out by hand rather than computed from the policy, so
    this table is an independent statement of intent. `reached` is the status
    an authorised caller gets: 200 or 202 where the route works, 404 for a
    random document id (ownership, after authorisation), 501 where the feature
    belongs to a later milestone.
    """

    method: str
    path: str
    permission: Permission
    allowed: frozenset[UserRole]
    reached: int
    request: dict[str, Any] = field(default_factory=dict)

    def url(self) -> str:
        return self.path.replace("{document_id}", str(uuid.uuid4())).replace(
            "{session_id}", str(uuid.uuid4())
        )

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


ROUTES = [
    Route(
        "POST",
        "/api/v1/documents/upload",
        Permission.DOCUMENT_WRITE,
        frozenset({R, A}),
        # 202 since M3/S3.1, with a real PDF: each role's user is a different
        # owner, so identical bytes are a new document for each (ADR-0010).
        202,
        {"files": {"file": ("p.pdf", make_pdf(), "application/pdf")}},
    ),
    Route(
        "GET", "/api/v1/documents/", Permission.DOCUMENT_READ, frozenset({V, R, A}), 200
    ),
    Route(
        "GET",
        "/api/v1/documents/{document_id}",
        Permission.DOCUMENT_READ,
        frozenset({V, R, A}),
        404,
    ),
    Route(
        "DELETE",
        "/api/v1/documents/{document_id}",
        Permission.DOCUMENT_WRITE,
        frozenset({R, A}),
        404,
    ),
    Route(
        "POST",
        "/api/v1/agents/run",
        Permission.AGENT_RUN,
        frozenset({R, A}),
        501,
        {"json": {"task": "summarise"}},
    ),
    Route(
        "GET",
        "/api/v1/agents/status/{session_id}",
        Permission.AGENT_RUN,
        frozenset({R, A}),
        501,
    ),
    Route(
        "POST",
        "/api/v1/search/",
        Permission.SEARCH_QUERY,
        frozenset({V, R, A}),
        # 200 since M4/S4.2: the route works. What this file asserts about it
        # is unchanged — who may reach it, and that 401 and 403 stay apart.
        200,
        {"json": {"query": "grounded answers"}},
    ),
    Route(
        "GET",
        "/api/v1/workspace/sessions",
        Permission.WORKSPACE_READ,
        frozenset({V, R, A}),
        501,
    ),
    Route(
        "POST",
        "/api/v1/workspace/sessions",
        Permission.WORKSPACE_WRITE,
        frozenset({R, A}),
        501,
    ),
]

PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/health/ready",
        "/metrics",
        "/api/v1/auth/register",
        "/api/v1/auth/login",
    }
)


def _authorisation_dependencies(route: APIRoute) -> list[object]:
    """Every RequirePermission / RequireRole anywhere in a route's dependency tree."""
    found: list[object] = []

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            if isinstance(sub.call, (RequirePermission, RequireRole)):
                found.append(sub.call)
            walk(sub)

    walk(route.dependant)
    return found


# -------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _search_without_infrastructure() -> AsyncIterator[None]:
    """Give the search route a service, without a model or a Qdrant.

    Since M4/S4.2 the route resolves a `SearchService` from the resources the
    application lifespan builds, and this file's client does not run a
    lifespan — deliberately, because it tests authorisation and would otherwise
    load a 67 MB model to prove that a viewer gets 403.

    The override returns no results. Every assertion here is about *who may
    reach the route*; what retrieval returns is
    `tests/integration/test_retrieval.py` and `tests/integration/test_search_api.py`.
    """
    from backend.api.dependencies.services import get_search_service

    class _NoResults:
        async def search(self, query: object, principal: object) -> tuple[()]:
            return ()

    app.dependency_overrides[get_search_service] = lambda: _NoResults()
    yield
    app.dependency_overrides.pop(get_search_service, None)


@pytest.fixture(autouse=True)
async def _truncate_after_each_test() -> AsyncIterator[None]:
    yield
    from backend.db.engine import get_engine

    async with get_engine().begin() as connection:
        await connection.execute(
            text("TRUNCATE document_chunks, documents, users RESTART IDENTITY CASCADE")
        )


async def _account(session: Any, role: UserRole) -> tuple[User, str]:
    """A committed user with `role`, and a token from the production handler."""
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Test User", role=role
    )
    await session.commit()
    return user, JWTHandler().create_access_token(user.id, user.email, user.role)


async def _document(session: Any, owner_id: str, filename: str = "paper.pdf") -> str:
    document = await DocumentRepository(session).create(
        user_id=owner_id, filename=filename, doc_type=DocumentType.PDF
    )
    await session.commit()
    return document.id


async def _set_role(session: Any, user_id: str, role: UserRole) -> None:
    await session.execute(
        update(UserORM).where(UserORM.id == uuid.UUID(user_id)).values(role=role)
    )
    await session.commit()


async def _deleted_at(session: Any, document_id: str) -> Any:
    return (
        await session.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"),
            {"id": uuid.UUID(document_id)},
        )
    ).scalar_one()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _call(client: Any, route: Route, headers: dict[str, str]) -> Any:
    return await client.request(
        route.method, route.url(), headers=headers, **route.request
    )


# ============================================================================
# The route authorisation matrix
# ============================================================================


class TestEveryProtectedRouteIsAuthorised:
    def test_the_table_covers_exactly_the_applications_protected_routes(self) -> None:
        """A route added to the app but not to this table fails here.

        So does a row for a route that no longer exists. The matrix below is
        only evidence while it describes the application that actually ships.
        """
        in_app = {
            f"{method} {route.path}"
            for route in app.routes
            if isinstance(route, APIRoute) and route.path not in PUBLIC_PATHS
            for method in route.methods - {"HEAD", "OPTIONS"}
        }

        assert in_app == {str(route) for route in ROUTES}

    @pytest.mark.parametrize("spec", ROUTES, ids=str)
    def test_each_route_requires_exactly_the_permission_in_the_table(
        self, spec: Route
    ) -> None:
        """Structural half: the dependency is there, once, with the right permission.

        This is what stops a future route shipping with authentication but no
        authorisation — S2.2's coverage test would still pass for it, because
        `get_current_user` would still be in its tree.
        """
        route = next(
            r
            for r in app.routes
            if isinstance(r, APIRoute)
            and r.path == spec.path
            and spec.method in r.methods
        )

        guards = _authorisation_dependencies(route)

        assert len(guards) == 1, f"{spec}: {guards}"
        assert isinstance(guards[0], RequirePermission)
        assert guards[0].permission is spec.permission

    @pytest.mark.parametrize("spec", ROUTES, ids=str)
    def test_the_table_agrees_with_the_policy(self, spec: Route) -> None:
        """The hand-written `allowed` column and `RBACPolicy` must say the same thing."""
        assert spec.allowed == {
            role
            for role in UserRole
            if RBACPolicy().has_permission(role, spec.permission)
        }

    @pytest.mark.parametrize("spec", ROUTES, ids=str)
    async def test_each_caller_gets_the_right_answer(
        self, api_client: Any, committing_session: Any, spec: Route
    ) -> None:
        """Behavioural half: the whole row, for every route.

        No token and a forged token are 401 regardless of the route. Every role
        is either refused with 403 or reaches the handler — and which, is the
        `allowed` column. Asserted as one dict so a failure prints the row.
        """
        tokens = {
            role: (await _account(committing_session, role))[1] for role in UserRole
        }

        actual = {
            "no token": (await _call(api_client, spec, {})).status_code,
            "forged": (
                await _call(api_client, spec, _bearer("totally-fake-not-a-jwt"))
            ).status_code,
            **{
                role.value: (
                    await _call(api_client, spec, _bearer(tokens[role]))
                ).status_code
                for role in UserRole
            },
        }
        expected = {
            "no token": 401,
            "forged": 401,
            **{
                role.value: spec.reached if role in spec.allowed else 403
                for role in UserRole
            },
        }

        assert actual == expected


# ============================================================================
# 401 and 403 are different answers
# ============================================================================


class TestUnauthenticatedAndForbiddenAreDistinct:
    async def test_the_two_responses_differ_in_status_body_and_challenge(
        self, api_client: Any, committing_session: Any
    ) -> None:
        _, viewer = await _account(committing_session, V)
        route = "/api/v1/workspace/sessions"

        unauthenticated = await api_client.post(route)
        forbidden = await api_client.post(route, headers=_bearer(viewer))

        assert (unauthenticated.status_code, forbidden.status_code) == (401, 403)
        assert unauthenticated.json() == UNAUTHENTICATED
        assert forbidden.json() == FORBIDDEN
        assert unauthenticated.headers["www-authenticate"] == "Bearer"
        assert "www-authenticate" not in forbidden.headers

    async def test_a_bad_token_is_401_even_where_the_role_would_be_refused(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Authentication is decided first, so a viewer's *expired* token is not a 403.

        Reporting it as a permissions problem would tell the client to ask for
        access when what it needs is to sign in again.
        """
        from datetime import timedelta

        viewer, _ = await _account(committing_session, V)
        expired = JWTHandler().create_access_token(
            viewer.id, viewer.email, viewer.role, expires_delta=timedelta(minutes=-1)
        )

        response = await api_client.post(
            "/api/v1/workspace/sessions", headers=_bearer(expired)
        )

        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED

    async def test_every_refusal_is_the_same_403(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """One message for every refused route, so none of them describes the policy."""
        _, viewer = await _account(committing_session, V)

        refusals = [
            await _call(api_client, spec, _bearer(viewer))
            for spec in ROUTES
            if V not in spec.allowed
        ]

        assert len(refusals) == 5
        assert {r.status_code for r in refusals} == {403}
        assert len({r.text for r in refusals}) == 1

    async def test_a_403_does_not_describe_the_policy(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """No permission name, no role name: the caller learns *no*, not *why*."""
        _, viewer = await _account(committing_session, V)

        body = (
            await api_client.post(
                "/api/v1/agents/run", headers=_bearer(viewer), json={"task": "t"}
            )
        ).text.lower()

        vocabulary = (
            {p.value for p in Permission}
            | {r.value for r in UserRole}
            | {"role", "rbac"}
        )
        assert not [word for word in vocabulary if word in body], body


# ============================================================================
# Ownership and authorisation are separate layers
# ============================================================================


class TestOwnershipIsNotAuthorisation:
    async def test_the_owner_reads_their_document(
        self, api_client: Any, committing_session: Any
    ) -> None:
        owner, token = await _account(committing_session, R)
        document_id = await _document(committing_session, owner.id, "mine.pdf")

        response = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )

        assert response.status_code == 200
        assert response.json()["filename"] == "mine.pdf"
        assert set(response.json()) == {"id", "filename", "status", "metadata"}

    async def test_permission_to_read_documents_is_not_permission_to_read_yours(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """A researcher holds DOCUMENT_READ. That does not reach another user's rows."""
        owner, _ = await _account(committing_session, R)
        _, other = await _account(committing_session, R)
        document_id = await _document(committing_session, owner.id)

        response = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=_bearer(other)
        )

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    async def test_not_even_an_administrator_bypasses_ownership(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """ADMIN holds every permission, and every permission is still scoped to its rows.

        The case the brief names directly: RBAC must not accidentally bypass
        ownership. Reading another user's corpus is not an operation any
        permission in the policy describes, so no role can perform it.
        """
        owner, _ = await _account(committing_session, R)
        _, admin = await _account(committing_session, A)
        document_id = await _document(committing_session, owner.id, "private.pdf")

        read = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=_bearer(admin)
        )
        deleted = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=_bearer(admin)
        )
        listed = await api_client.get("/api/v1/documents/", headers=_bearer(admin))

        assert read.status_code == 404
        assert deleted.status_code == 404
        assert listed.json() == {"documents": [], "total": 0}
        assert await _deleted_at(committing_session, document_id) is None

    async def test_a_foreign_document_is_indistinguishable_from_a_missing_one(
        self, api_client: Any, committing_session: Any
    ) -> None:
        owner, _ = await _account(committing_session, R)
        _, other = await _account(committing_session, R)
        document_id = await _document(committing_session, owner.id)

        foreign = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=_bearer(other)
        )
        missing = await api_client.get(
            f"/api/v1/documents/{uuid.uuid4()}", headers=_bearer(other)
        )

        assert (foreign.status_code, foreign.text) == (
            missing.status_code,
            missing.text,
        )

    async def test_a_refused_cross_user_delete_changes_nothing(
        self, api_client: Any, committing_session: Any
    ) -> None:
        owner, _ = await _account(committing_session, R)
        _, other = await _account(committing_session, R)
        document_id = await _document(committing_session, owner.id)

        response = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=_bearer(other)
        )

        assert response.status_code == 404
        assert await _deleted_at(committing_session, document_id) is None

    async def test_owning_a_document_is_not_permission_to_delete_it(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """The other direction: correct ownership, insufficient role, 403."""
        viewer, token = await _account(committing_session, V)
        document_id = await _document(committing_session, viewer.id)

        response = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )

        assert response.status_code == 403
        assert await _deleted_at(committing_session, document_id) is None

    async def test_a_403_says_nothing_about_whether_the_document_exists(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Authorisation is decided from the role, before any row is looked up.

        So a viewer probing ids gets the same 403 for their own document, for
        someone else's, and for one that never existed.
        """
        viewer, token = await _account(committing_session, V)
        other, _ = await _account(committing_session, R)
        own = await _document(committing_session, viewer.id)
        foreign = await _document(committing_session, other.id)

        answers = {
            (r.status_code, r.text)
            for r in [
                await api_client.delete(
                    f"/api/v1/documents/{document_id}", headers=_bearer(token)
                )
                for document_id in (own, foreign, str(uuid.uuid4()))
            ]
        }

        assert answers == {
            (403, '{"detail":"You do not have permission to perform this action."}')
        }

    async def test_the_owner_deletes_and_the_row_survives_soft_deleted(
        self, api_client: Any, committing_session: Any
    ) -> None:
        owner, token = await _account(committing_session, R)
        document_id = await _document(committing_session, owner.id)

        deleted = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )
        again = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )

        assert deleted.status_code == 204
        assert deleted.content == b""
        assert again.status_code == 404
        assert await _deleted_at(committing_session, document_id) is not None

    async def test_listing_is_scoped_to_the_caller(
        self, api_client: Any, committing_session: Any
    ) -> None:
        alice, alice_token = await _account(committing_session, R)
        bob, bob_token = await _account(committing_session, R)
        await _document(committing_session, alice.id, "alice.pdf")
        await _document(committing_session, bob.id, "bob.pdf")

        alices = (
            await api_client.get("/api/v1/documents/", headers=_bearer(alice_token))
        ).json()
        bobs = (
            await api_client.get("/api/v1/documents/", headers=_bearer(bob_token))
        ).json()

        assert [d["filename"] for d in alices["documents"]] == ["alice.pdf"]
        assert [d["filename"] for d in bobs["documents"]] == ["bob.pdf"]
        assert alices["total"] == bobs["total"] == 1


# ============================================================================
# Identity reaches the service as a Principal, and only from the token
# ============================================================================


class TestIdentityComesOnlyFromTheToken:
    """principles.md §2 — never from a body, query parameter or header."""

    async def test_a_user_id_query_parameter_cannot_select_whose_documents_are_listed(
        self, api_client: Any, committing_session: Any
    ) -> None:
        alice, alice_token = await _account(committing_session, R)
        bob, _ = await _account(committing_session, R)
        await _document(committing_session, bob.id, "bob-secret.pdf")

        response = await api_client.get(
            f"/api/v1/documents/?user_id={bob.id}", headers=_bearer(alice_token)
        )

        assert response.json() == {"documents": [], "total": 0}

    async def test_a_user_id_header_cannot_select_an_identity(
        self, api_client: Any, committing_session: Any
    ) -> None:
        alice, alice_token = await _account(committing_session, R)
        bob, _ = await _account(committing_session, R)
        bobs = await _document(committing_session, bob.id, "bob-secret.pdf")

        response = await api_client.get(
            f"/api/v1/documents/{bobs}",
            headers={
                **_bearer(alice_token),
                "X-User-Id": bob.id,
                "X-Forwarded-User": bob.id,
            },
        )

        assert response.status_code == 404

    async def test_a_user_id_cannot_redirect_a_delete(
        self, api_client: Any, committing_session: Any
    ) -> None:
        alice, alice_token = await _account(committing_session, R)
        bob, _ = await _account(committing_session, R)
        bobs = await _document(committing_session, bob.id)

        response = await api_client.delete(
            f"/api/v1/documents/{bobs}?user_id={bob.id}", headers=_bearer(alice_token)
        )

        assert response.status_code == 404
        assert await _deleted_at(committing_session, bobs) is None

    async def test_the_service_receives_the_principal_the_token_resolved_to(
        self, api_client: Any, committing_session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavioural proof of the propagation, not a signature check.

        A spy on the real service method records what the route handed it, and
        the real method still runs. It must be a `Principal`, and its user id
        must be the token's subject — not anything the request carried.
        """
        user, token = await _account(committing_session, R)
        decoy, _ = await _account(committing_session, R)
        received: list[object] = []
        original = DocumentService.list_user_documents

        async def spy(self: DocumentService, principal: Principal) -> Any:
            received.append(principal)
            return await original(self, principal)

        monkeypatch.setattr(DocumentService, "list_user_documents", spy)

        await api_client.get(
            f"/api/v1/documents/?user_id={decoy.id}", headers=_bearer(token)
        )

        assert len(received) == 1
        assert isinstance(received[0], Principal)
        assert received[0].user_id == user.id
        assert received[0].role is R

    async def test_registration_cannot_choose_a_role(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """The obvious escalation: ask for `admin` in the sign-up body."""
        response = await api_client.post(
            "/api/v1/auth/register",
            json={
                "email": "ambitious@example.com",
                "password": "correct-horse-battery",
                "full_name": "Ambitious",
                "role": "admin",
                "is_active": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["role"] == "researcher"
        stored = (
            await committing_session.execute(
                text("SELECT role FROM users WHERE email = 'ambitious@example.com'")
            )
        ).scalar_one()
        assert stored == "researcher"


# ============================================================================
# A token's role claim is never the authority
# ============================================================================


class TestRolesAreReadFreshOnEveryRequest:
    async def test_a_demoted_user_loses_write_access_with_the_same_token(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """The scenario in the sprint brief, through a real registration and login.

        The token still says `researcher`, correctly signed and unexpired. The
        database says `viewer`. Authorisation must follow the database.
        """
        await api_client.post(
            "/api/v1/auth/register",
            json={
                "email": "demoted@example.com",
                "password": "correct-horse-battery",
                "full_name": "D",
            },
        )
        token = (
            await api_client.post(
                "/api/v1/auth/login",
                json={
                    "email": "demoted@example.com",
                    "password": "correct-horse-battery",
                },
            )
        ).json()["access_token"]
        user_id = pyjwt.decode(token, get_settings().JWT_SECRET, algorithms=["HS256"])[
            "sub"
        ]
        document_id = await _document(committing_session, user_id)

        await _set_role(committing_session, user_id, V)

        claims = pyjwt.decode(token, get_settings().JWT_SECRET, algorithms=["HS256"])
        delete = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )
        read = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )

        assert claims["role"] == "researcher", "the token itself is unchanged"
        assert delete.status_code == 403
        assert read.status_code == 200
        assert await _deleted_at(committing_session, document_id) is None

    async def test_a_promoted_user_gains_access_with_the_same_token(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """The same rule the other way: a stale claim cannot withhold a grant either."""
        viewer, token = await _account(committing_session, V)
        document_id = await _document(committing_session, viewer.id)
        assert (
            await api_client.delete(
                f"/api/v1/documents/{document_id}", headers=_bearer(token)
            )
        ).status_code == 403

        await _set_role(committing_session, viewer.id, R)

        response = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=_bearer(token)
        )

        assert response.status_code == 204

    async def test_a_demoted_administrator_is_refused_immediately(
        self, api_client: Any, committing_session: Any
    ) -> None:
        admin, token = await _account(committing_session, A)
        run = {"json": {"task": "summarise"}}
        assert (
            await api_client.post("/api/v1/agents/run", headers=_bearer(token), **run)
        ).status_code == 501

        await _set_role(committing_session, admin.id, V)

        assert (
            await api_client.post("/api/v1/agents/run", headers=_bearer(token), **run)
        ).status_code == 403

    async def test_a_token_claiming_a_higher_role_grants_nothing(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """Correctly signed, claiming `admin`, for a user who is a viewer.

        What a token issued before a demotion looks like — and, equally, what
        a leaked signing key would produce. The claim is not consulted.
        """
        viewer, _ = await _account(committing_session, V)
        token = JWTHandler().create_access_token(viewer.id, viewer.email, A)

        response = await api_client.post(
            "/api/v1/workspace/sessions", headers=_bearer(token)
        )

        assert response.status_code == 403


# ============================================================================
# Inactive users never become Principals, so never reach authorisation
# ============================================================================


class TestInactiveUsersAreUnauthenticatedNotForbidden:
    @pytest.mark.parametrize("role", [V, R, A], ids=lambda r: r.value)
    async def test_a_deactivated_user_gets_401_not_403(
        self, api_client: Any, committing_session: Any, role: UserRole
    ) -> None:
        """Whatever their role — including routes their role would be refused."""
        user, token = await _account(committing_session, role)
        await committing_session.execute(
            update(UserORM)
            .where(UserORM.id == uuid.UUID(user.id))
            .values(is_active=False)
        )
        await committing_session.commit()

        answers = {
            (await _call(api_client, spec, _bearer(token))).status_code
            for spec in ROUTES
        }

        assert answers == {401}

    async def test_authorisation_is_never_consulted_for_them(
        self, api_client: Any, committing_session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The policy is not even asked. A Principal was never built to ask about."""
        calls: list[tuple[object, object]] = []
        original = RBACPolicy.has_permission

        def spy(self: RBACPolicy, role: UserRole, permission: Permission) -> bool:
            calls.append((role, permission))
            return original(self, role, permission)

        monkeypatch.setattr(RBACPolicy, "has_permission", spy)
        user, token = await _account(committing_session, R)
        await committing_session.execute(
            update(UserORM)
            .where(UserORM.id == uuid.UUID(user.id))
            .values(is_active=False)
        )
        await committing_session.commit()

        response = await api_client.get("/api/v1/documents/", headers=_bearer(token))

        assert response.status_code == 401
        assert calls == []

    async def test_an_active_user_does_reach_the_policy(
        self, api_client: Any, committing_session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control for the test above — otherwise the spy might never fire at all."""
        calls: list[tuple[object, object]] = []
        original = RBACPolicy.has_permission

        def spy(self: RBACPolicy, role: UserRole, permission: Permission) -> bool:
            calls.append((role, permission))
            return original(self, role, permission)

        monkeypatch.setattr(RBACPolicy, "has_permission", spy)
        _, token = await _account(committing_session, R)

        await api_client.get("/api/v1/documents/", headers=_bearer(token))

        assert calls == [(R, Permission.DOCUMENT_READ)]


# ============================================================================
# require_role
# ============================================================================


def _role_probe() -> FastAPI:
    """Two routes guarded by `require_role`, over the real authentication chain.

    No route in the application uses `require_role` — all nine are authorised
    by permission — so the only way to test it behaviourally is to mount it.
    Everything beneath it is real: `get_current_user`, the session dependency,
    PostgreSQL, the production token.
    """
    probe = FastAPI()

    @probe.get("/admin-only")
    async def admin_only(
        principal: Principal = Depends(require_role(A)),
    ) -> dict[str, str]:
        return {"user_id": principal.user_id}

    @probe.get("/staff")
    async def staff(
        principal: Principal = Depends(require_role(A, R)),
    ) -> dict[str, str]:
        return {"user_id": principal.user_id}

    return probe


class TestRequireRole:
    async def test_the_401_403_success_contract(self, committing_session: Any) -> None:
        admin, admin_token = await _account(committing_session, A)
        _, researcher = await _account(committing_session, R)
        _, viewer = await _account(committing_session, V)

        async with AsyncClient(
            transport=ASGITransport(app=_role_probe()), base_url="http://probe"
        ) as client:
            assert (await client.get("/admin-only")).status_code == 401
            assert (
                await client.get("/admin-only", headers=_bearer("forged"))
            ).status_code == 401
            assert (
                await client.get("/admin-only", headers=_bearer(viewer))
            ).status_code == 403
            assert (
                await client.get("/admin-only", headers=_bearer(researcher))
            ).status_code == 403
            granted = await client.get("/admin-only", headers=_bearer(admin_token))
            assert (granted.status_code, granted.json()) == (200, {"user_id": admin.id})

            assert (
                await client.get("/staff", headers=_bearer(researcher))
            ).status_code == 200
            assert (
                await client.get("/staff", headers=_bearer(viewer))
            ).status_code == 403
            assert (
                await client.get("/staff", headers=_bearer(viewer))
            ).json() == FORBIDDEN

    async def test_it_follows_the_database_role_too(
        self, committing_session: Any
    ) -> None:
        admin, token = await _account(committing_session, A)
        await _set_role(committing_session, admin.id, V)

        async with AsyncClient(
            transport=ASGITransport(app=_role_probe()), base_url="http://probe"
        ) as client:
            assert (
                await client.get("/admin-only", headers=_bearer(token))
            ).status_code == 403

    def test_it_refuses_to_be_configured_wrongly(self) -> None:
        """Mistakes surface when a router is defined, not at the first request."""
        with pytest.raises(ValueError):
            require_role()
        with pytest.raises(TypeError):
            require_role("admin")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            require_permission("document:read")  # type: ignore[arg-type]


# ============================================================================
# The whole journey
# ============================================================================


class TestTheWholeJourney:
    async def test_register_login_authorise_own_and_be_refused(
        self, api_client: Any, committing_session: Any
    ) -> None:
        """register → login → Principal → authorisation → service → ownership.

        Each layer's answer, in order, for one user whose role changes midway:
        a working read, a permitted delete refused after demotion, restored
        after promotion, and a stranger who can see none of it.
        """
        credentials = {
            "email": "journey@example.com",
            "password": "correct-horse-battery",
        }
        registered = await api_client.post(
            "/api/v1/auth/register", json={**credentials, "full_name": "Journey"}
        )
        assert registered.status_code == 201
        user_id = registered.json()["id"]

        token = (await api_client.post("/api/v1/auth/login", json=credentials)).json()[
            "access_token"
        ]
        me = _bearer(token)
        assert (await api_client.get("/api/v1/documents/", headers=me)).json() == {
            "documents": [],
            "total": 0,
        }

        document_id = await _document(committing_session, user_id, "thesis.pdf")
        assert (
            await api_client.get(f"/api/v1/documents/{document_id}", headers=me)
        ).status_code == 200

        _, stranger = await _account(committing_session, R)
        assert (
            await api_client.get(
                f"/api/v1/documents/{document_id}", headers=_bearer(stranger)
            )
        ).status_code == 404

        await _set_role(committing_session, user_id, V)
        assert (
            await api_client.delete(f"/api/v1/documents/{document_id}", headers=me)
        ).status_code == 403
        assert (
            await api_client.get(f"/api/v1/documents/{document_id}", headers=me)
        ).status_code == 200

        await _set_role(committing_session, user_id, R)
        assert (
            await api_client.delete(f"/api/v1/documents/{document_id}", headers=me)
        ).status_code == 204
        assert (
            await api_client.get(f"/api/v1/documents/{document_id}", headers=me)
        ).status_code == 404
        assert (await api_client.get("/api/v1/documents/", headers=me)).json()[
            "total"
        ] == 0

        assert (await api_client.get("/api/v1/documents/")).status_code == 401
