"""The authorisation policy, with no HTTP and no database.

`RBACPolicy` is a pure function of (role, permission). These pin its answers
exhaustively — every role against every permission — because an authorisation
matrix is exactly the kind of table where one wrong cell is a privilege
escalation and nothing else in the suite would notice.

Whether routes *consult* the policy, and return 403 when it refuses, is
`tests/integration/test_authorization.py`.
"""

import itertools

import pytest

from backend.security.rbac import Permission, RBACPolicy
from shared.models.user import UserRole

POLICY = RBACPolicy()

# The matrix, written out by hand rather than read from the code under test. A
# test that derived its expectations from `RBACPolicy.PERMISSIONS` would agree
# with any change to it, which is the one thing this file exists to prevent.
EXPECTED: dict[UserRole, set[Permission]] = {
    UserRole.ADMIN: set(Permission),
    UserRole.RESEARCHER: {
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_WRITE,
        Permission.AGENT_RUN,
        Permission.SEARCH_QUERY,
        Permission.WORKSPACE_READ,
        Permission.WORKSPACE_WRITE,
    },
    UserRole.VIEWER: {
        Permission.DOCUMENT_READ,
        Permission.SEARCH_QUERY,
        Permission.WORKSPACE_READ,
    },
}


class TestThePermissionVocabulary:
    def test_the_permissions_are_exactly_the_scaffolds_six(self) -> None:
        """No invented permissions.

        The original `rbac.py` spelled these out as bare strings. They became
        an enum in M2/S2.5 so that a typo fails at import; the values did not
        change, and none were added.
        """
        assert {p.value for p in Permission} == {
            "document:read",
            "document:write",
            "agent:run",
            "search:query",
            "workspace:read",
            "workspace:write",
        }

    def test_every_role_has_an_entry(self) -> None:
        """A role added to `UserRole` without a policy row would be refused everything.

        That fails closed, which is the safe direction — but silently, and the
        first report would come from a user. This makes it a test failure.
        """
        assert set(RBACPolicy.PERMISSIONS) == set(UserRole)


class TestHasPermission:
    @pytest.mark.parametrize(
        ("role", "permission"),
        list(itertools.product(UserRole, Permission)),
        ids=lambda value: value.value,
    )
    def test_every_cell_of_the_matrix(
        self, role: UserRole, permission: Permission
    ) -> None:
        """All eighteen (role, permission) pairs, each asserted individually."""
        expected = permission in EXPECTED[role]

        assert POLICY.has_permission(role, permission) is expected

    def test_a_viewer_cannot_write_anything(self) -> None:
        writes = {Permission.DOCUMENT_WRITE, Permission.WORKSPACE_WRITE}

        assert not any(POLICY.has_permission(UserRole.VIEWER, p) for p in writes)

    def test_a_viewer_cannot_run_agents(self) -> None:
        """Agent runs spend LLM tokens — the costliest operation in the system."""
        assert POLICY.has_permission(UserRole.VIEWER, Permission.AGENT_RUN) is False

    def test_it_is_deterministic(self) -> None:
        """No clock, no randomness, no I/O: the same question, the same answer."""
        answers = {
            POLICY.has_permission(role, permission)
            for _ in range(50)
            for role, permission in [(UserRole.VIEWER, Permission.DOCUMENT_WRITE)]
        }

        assert answers == {False}

    def test_separate_policy_instances_agree(self) -> None:
        """No per-instance state that one caller could change for another."""
        for role, permission in itertools.product(UserRole, Permission):
            assert RBACPolicy().has_permission(
                role, permission
            ) == POLICY.has_permission(role, permission)


class TestGetPermissions:
    @pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
    def test_each_role_gets_exactly_its_permissions(self, role: UserRole) -> None:
        assert POLICY.get_permissions(role) == EXPECTED[role]

    def test_it_agrees_with_has_permission(self) -> None:
        """Two views of one matrix must not disagree."""
        for role, permission in itertools.product(UserRole, Permission):
            assert (permission in POLICY.get_permissions(role)) is (
                POLICY.has_permission(role, permission)
            )

    def test_the_result_cannot_be_used_to_grant_a_permission(self) -> None:
        """A frozenset: a caller cannot add to the policy through the value it got."""
        granted = POLICY.get_permissions(UserRole.VIEWER)

        with pytest.raises(AttributeError):
            granted.add(Permission.DOCUMENT_WRITE)  # type: ignore[attr-defined]

        assert (
            POLICY.has_permission(UserRole.VIEWER, Permission.DOCUMENT_WRITE) is False
        )

    def test_the_administrator_holds_every_permission_including_future_ones(
        self,
    ) -> None:
        """The scaffold's `["*"]`, kept in meaning.

        Computed from the enum rather than listed, so a permission added later
        is an administrator's without anyone remembering to add it.
        """
        assert POLICY.get_permissions(UserRole.ADMIN) == frozenset(Permission)


class TestNoPrivilegeEscalation:
    def test_the_roles_are_strictly_nested(self) -> None:
        """Viewer ⊂ Researcher ⊂ Admin.

        Not a property the scaffold states, but one it implies and one worth
        pinning: if it ever stops holding, some role has quietly gained a
        permission a *more* privileged role lacks, and that is almost certainly
        a mistake.
        """
        viewer = POLICY.get_permissions(UserRole.VIEWER)
        researcher = POLICY.get_permissions(UserRole.RESEARCHER)
        admin = POLICY.get_permissions(UserRole.ADMIN)

        assert viewer < researcher <= admin

    @pytest.mark.parametrize("raw_role", ["admin", "ADMIN", "researcher", "viewer"])
    def test_a_role_given_as_a_plain_string_is_refused_everything(
        self, raw_role: str
    ) -> None:
        """The concrete hazard, measured before it was guarded.

        `UserRole` is a `str` enum, so `"admin" == UserRole.ADMIN` and a plain
        dict lookup returns the administrator's permissions for the *string*
        `"admin"`. Nothing passes a raw string today; this makes sure a
        deserialised payload or an unconverted MCP argument never can.
        """
        for permission in Permission:
            assert POLICY.has_permission(raw_role, permission) is False  # type: ignore[arg-type]
        assert POLICY.get_permissions(raw_role) == frozenset()  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bogus", [None, "", "superuser", 0, 1, True, object()], ids=repr
    )
    def test_anything_that_is_not_a_role_is_refused(self, bogus: object) -> None:
        for permission in Permission:
            assert POLICY.has_permission(bogus, permission) is False  # type: ignore[arg-type]
        assert POLICY.get_permissions(bogus) == frozenset()  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "raw_permission", ["document:write", "*", "", "document:*", None]
    )
    def test_a_permission_that_is_not_a_permission_is_refused_even_for_admin(
        self, raw_permission: object
    ) -> None:
        """Including `"*"`, which used to be a literal value in this very policy.

        With the wildcard gone from the representation, there is no string an
        attacker-influenced value could take that matches everything.
        """
        for role in UserRole:
            assert POLICY.has_permission(role, raw_permission) is False  # type: ignore[arg-type]
