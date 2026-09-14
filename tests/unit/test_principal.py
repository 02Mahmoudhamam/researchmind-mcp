"""The authenticated-identity type.

These test the *model's* guarantees. That a real request produces a correct
Principal through the real resolver is `tests/integration/test_authentication.py`
— §18 of the sprint brief asks for both, and they prove different things: this
file says an inactive principal cannot be built, that one says the only code
that builds one is authentication.
"""

import typing

import pytest
from pydantic import ValidationError

from shared.models.principal import Principal
from shared.models.user import User, UserRole


def _principal(**overrides: object) -> Principal:
    fields: dict[str, object] = {
        "user_id": "11111111-1111-1111-1111-111111111111",
        "email": "researcher@example.com",
        "role": UserRole.RESEARCHER,
    }
    fields.update(overrides)
    return Principal(**fields)  # type: ignore[arg-type]


class TestPrincipalCarriesTheAuthenticatedIdentity:
    def test_it_holds_the_four_fields_an_authorisation_decision_needs(self) -> None:
        principal = _principal()

        assert principal.user_id == "11111111-1111-1111-1111-111111111111"
        assert principal.email == "researcher@example.com"
        assert principal.role is UserRole.RESEARCHER
        assert principal.is_active is True

    def test_it_carries_nothing_else(self) -> None:
        """Four fields, and the set is pinned.

        A Principal is passed down every call path and will reach the SQL
        ownership predicate in S2.5. Anything added here becomes something
        every layer can read and therefore something a layer might start
        trusting — `full_name` is not an authorisation input.
        """
        assert set(Principal.model_fields) == {"user_id", "email", "role", "is_active"}

    @pytest.mark.parametrize("role", list(UserRole))
    def test_it_accepts_every_role_the_user_model_defines(self, role: UserRole) -> None:
        assert _principal(role=role).role is role

    def test_it_reuses_the_user_role_enum_rather_than_defining_its_own(self) -> None:
        """A second role enum would drift from the first.

        `UserORM.role` persists these values under a CHECK constraint and the
        RBAC policy keys off them. Three definitions of "what roles exist" is
        two too many.
        """
        assert Principal.model_fields["role"].annotation is UserRole
        assert User.model_fields["role"].annotation is UserRole

    def test_an_unknown_role_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _principal(role="superuser")

    @pytest.mark.parametrize("missing", ["user_id", "email", "role"])
    def test_every_identity_field_is_required(self, missing: str) -> None:
        """None of these may be defaulted or inferred.

        A Principal with an empty `user_id` that still type-checks is the
        ownership predicate matching nothing — or, worse, matching everything
        if a later query is written with a NULL-tolerant comparison.
        """
        fields = {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "email": "researcher@example.com",
            "role": UserRole.RESEARCHER,
        }
        del fields[missing]

        with pytest.raises(ValidationError):
            Principal(**fields)  # type: ignore[arg-type]


class TestAnInactivePrincipalIsUnrepresentable:
    """principles.md §1 — a token whose subject is not *active* is rejected."""

    def test_is_active_false_is_refused_at_construction(self) -> None:
        """Not "the resolver never passes False" — the type cannot hold it.

        This is the difference between a rule someone follows and a rule that
        holds. If a future code path forgets the `is_active` check, it does not
        silently mint a principal for a disabled account; it raises here.
        """
        with pytest.raises(ValidationError):
            _principal(is_active=False)

    def test_is_active_is_declared_as_literal_true_not_bool(self) -> None:
        """The static half of the same guarantee.

        Declared `bool`, `Principal(is_active=user.is_active)` type-checks and
        the runtime check above is the only defence. Declared `Literal[True]`,
        mypy rejects passing a plain bool, so the resolver is *forced* to branch
        on `user.is_active` before it can construct one. Verified under mypy:
        that exact call is an `arg-type` error.
        """
        annotation = Principal.model_fields["is_active"].annotation

        assert typing.get_origin(annotation) is typing.Literal
        assert typing.get_args(annotation) == (True,)

    def test_a_truthy_non_boolean_does_not_slip_through(self) -> None:
        """`1` is not `True` here, though Python would usually let it be.

        Pydantic coerces `1` to `True` for a `bool` field. Against `Literal[True]`
        in the default (non-strict) mode it must not, or the "unrepresentable"
        claim would be satisfiable by anything truthy.
        """
        with pytest.raises(ValidationError):
            _principal(is_active="yes")


class TestPrincipalIsImmutable:
    def test_a_field_cannot_be_reassigned(self) -> None:
        """Identity is settled once, before the handler runs.

        A mutable Principal means "who is this request" depends on where you
        ask, and the answer that reaches the ownership predicate need not be
        the answer authentication produced.
        """
        principal = _principal()

        with pytest.raises(ValidationError):
            principal.user_id = "22222222-2222-2222-2222-222222222222"

    def test_the_role_cannot_be_escalated_after_authentication(self) -> None:
        """The case the frozen model exists to prevent."""
        principal = _principal(role=UserRole.VIEWER)

        with pytest.raises(ValidationError):
            principal.role = UserRole.ADMIN

        assert principal.role is UserRole.VIEWER

    def test_the_model_declares_itself_frozen(self) -> None:
        assert Principal.model_config.get("frozen") is True
