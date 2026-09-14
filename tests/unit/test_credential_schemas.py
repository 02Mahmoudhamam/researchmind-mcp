"""The credential contract at the API boundary.

The endpoints are still stubs — registration and login are S2.4. What S2.3
fixes is what those endpoints will receive, so that the service layer written
next sprint cannot hash a password nobody validated.
"""

import pytest
from pydantic import ValidationError

from backend.api.schemas.auth import LoginRequest, LoginResponse, RegisterRequest
from backend.security.passwords import MINIMUM_LENGTH


def _register(password: str) -> RegisterRequest:
    return RegisterRequest(
        email="researcher@example.com", password=password, full_name="A Researcher"
    )


class TestRegistrationAppliesThePolicy:
    def test_a_valid_password_is_accepted(self) -> None:
        assert _register("correct-horse-battery").password == "correct-horse-battery"

    @pytest.mark.parametrize(
        "password",
        ["", "short", "a" * (MINIMUM_LENGTH - 1), "a" * 73, "valid-enough\x00tail"],
        ids=["empty", "short", "one-under", "over-72-bytes", "null-byte"],
    )
    def test_a_password_the_policy_rejects_fails_validation(
        self, password: str
    ) -> None:
        """The policy is not restated here — it is the same function.

        `Password` is `Annotated[str, AfterValidator(validate_password)]`, so a
        rule added to the policy applies to this schema without anyone
        remembering to mirror it.
        """
        with pytest.raises(ValidationError):
            _register(password)

    def test_the_field_holds_the_normalised_form(self) -> None:
        """Normalisation happens once, at the boundary.

        Whatever S2.4 does with `request.password`, it is operating on the form
        that will be hashed rather than the form that arrived.
        """
        assert _register("ﬁre-and-motion").password == "fire-and-motion"

    def test_a_rejection_does_not_echo_the_password(self) -> None:
        """Pydantic puts the offending input in the error by default.

        For most fields that is a convenience. For this one it would copy the
        password into logs, traces and error responses.
        """
        secret = "z" * 300

        with pytest.raises(ValidationError) as raised:
            _register(secret)

        assert secret not in str(raised.value)


class TestLoginDoesNotApplyThePolicy:
    """Deliberate asymmetry, and the reasoning matters more than the code.

    A sign-in attempt is not a choice of password. Validating it would reject a
    credential that was legitimate when it was set, and would answer "too
    short" where the only safe answer is "wrong".
    """

    @pytest.mark.parametrize("password", ["", "x", "short", "a" * 500])
    def test_any_string_is_accepted_as_a_login_attempt(self, password: str) -> None:
        request = LoginRequest(email="researcher@example.com", password=password)

        assert request.password == password

    def test_a_login_attempt_is_not_normalised_by_the_schema(self) -> None:
        """`verify_password` normalises the candidate itself.

        Doing it here as well would be harmless but redundant, and it would put
        a second place in the codebase that has to agree about Unicode.
        """
        request = LoginRequest(email="a@example.com", password="ﬁre-and-motion")

        assert request.password == "ﬁre-and-motion"


class TestNoScheduleForPlaintextStorage:
    def test_no_auth_schema_returns_a_password(self) -> None:
        """Requests carry passwords; responses must not."""
        for field in LoginResponse.model_fields:
            assert "password" not in field

    def test_the_token_response_is_unchanged_by_this_sprint(self) -> None:
        """S2.1 settled this shape: access token only, no refresh."""
        assert set(LoginResponse.model_fields) == {
            "access_token",
            "token_type",
            "expires_in",
        }
