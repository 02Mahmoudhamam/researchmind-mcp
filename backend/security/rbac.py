"""Role-Based Access Control — the policy, with no HTTP in it.

Authentication answers *who are you* and produces a `Principal`. This module
answers *may you* — and only that. It takes a role and a permission and returns
a boolean. It does not read the database, the token, the request or the clock,
so the same question always gets the same answer, and the MCP adapter can ask
it without importing FastAPI (ADR-0002 §6).

The role it is asked about must be `Principal.role`, which M2/S2.2 resolves from
the current database row on every request. The `role` claim inside a JWT is
never consulted: a token minted before a demotion still carries the old role,
and honouring it would keep revoked privileges alive until expiry.

The HTTP half — turning a refusal into 403 — is `backend/security/api_security.py`.
"""

from enum import Enum
from typing import Mapping

from shared.models.user import UserRole


class Permission(str, Enum):
    """Everything a role can be allowed to do.

    The six values the original scaffold spelled out as bare strings, and no
    others. Typed rather than left as strings for one concrete reason: a route
    asking for `"documents:read"` where the policy grants `"document:read"`
    would be refused for every role, silently, and the first sign would be a
    user report. As enum members, that typo is an `AttributeError` at import.
    """

    DOCUMENT_READ = "document:read"
    DOCUMENT_WRITE = "document:write"
    AGENT_RUN = "agent:run"
    SEARCH_QUERY = "search:query"
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_WRITE = "workspace:write"


class RBACPolicy:
    """Which permissions each role holds.

    The matrix is the scaffold's, unchanged in meaning. One representation
    changed: the administrator's `["*"]` is now every defined permission,
    spelled out as a set. The two mean the same thing for a closed set of
    permissions — and an administrator still gains any permission added to
    `Permission` later, because the set is computed from the enum — but a
    literal wildcard is something a membership check has to special-case, and
    special cases in an authorisation check are where escalations live.
    """

    PERMISSIONS: Mapping[UserRole, frozenset[Permission]] = {
        UserRole.ADMIN: frozenset(Permission),
        UserRole.RESEARCHER: frozenset(
            {
                Permission.DOCUMENT_READ,
                Permission.DOCUMENT_WRITE,
                Permission.AGENT_RUN,
                Permission.SEARCH_QUERY,
                Permission.WORKSPACE_READ,
                Permission.WORKSPACE_WRITE,
            }
        ),
        UserRole.VIEWER: frozenset(
            {
                Permission.DOCUMENT_READ,
                Permission.SEARCH_QUERY,
                Permission.WORKSPACE_READ,
            }
        ),
    }

    def has_permission(self, role: UserRole, permission: Permission) -> bool:
        """True only if `role` is a known role that holds `permission`.

        Fails closed on anything unexpected, and "unexpected" is checked by
        type rather than by value. `UserRole` and `Permission` are both `str`
        enums, so `"admin" == UserRole.ADMIN` is True and a plain dict lookup
        would happily treat the *string* `"admin"` as the administrator role.
        Nothing in the application passes a raw string today; this is what
        makes sure nothing ever can, whether by a typo, a deserialised payload,
        or a future MCP tool argument that reached here unconverted.
        """
        if not isinstance(role, UserRole) or not isinstance(permission, Permission):
            return False
        return permission in self.PERMISSIONS.get(role, frozenset())

    def get_permissions(self, role: UserRole) -> frozenset[Permission]:
        """Every permission `role` holds — empty for anything that is not a role.

        A frozenset, so a caller cannot add to the policy by mutating the value
        it was handed.
        """
        if not isinstance(role, UserRole):
            return frozenset()
        return self.PERMISSIONS.get(role, frozenset())
