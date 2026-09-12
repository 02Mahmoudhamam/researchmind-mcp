"""Role-Based Access Control."""

from shared.models.user import UserRole
from typing import List


class RBACPolicy:
    """Defines permission rules per role."""

    PERMISSIONS: dict = {
        UserRole.ADMIN: ["*"],
        UserRole.RESEARCHER: [
            "document:read",
            "document:write",
            "agent:run",
            "search:query",
            "workspace:read",
            "workspace:write",
        ],
        UserRole.VIEWER: [
            "document:read",
            "search:query",
            "workspace:read",
        ],
    }

    def has_permission(self, role: UserRole, permission: str) -> bool:
        """Check if a role has a specific permission."""
        ...  # TODO: implement

    def get_permissions(self, role: UserRole) -> List[str]:
        """Return all permissions for a role."""
        ...  # TODO: implement
