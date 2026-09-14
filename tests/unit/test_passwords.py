"""Password hashing and policy.

Real bcrypt throughout — no mocking of the primitive under test. Several of
these assert things about bcrypt itself rather than about our code, and they
earn their place: the behaviour they pin is the reason our code is shaped the
way it is, and it is behaviour that has changed between bcrypt releases.

Registration and login are Sprint M2/S2.4. Nothing here calls them, because
nothing here exists yet.
"""

import unicodedata

import bcrypt
import pytest

from backend.security.passwords import (
    MAXIMUM_BYTES,
    MINIMUM_LENGTH,
    PasswordPolicyError,
    hash_password,
    normalize_password,
    validate_password,
    verify_password,
)

# Twelve characters, no composition tricks, and not a real credential.
VALID = "correct-horse-battery"


class TestHashing:
    def test_a_hash_is_not_the_password(self) -> None:
        """The assertion that would have caught the worst possible bug."""
        digest = hash_password(VALID)

        assert VALID not in digest
        assert digest != VALID

    def test_a_hash_is_a_bcrypt_digest(self) -> None:
        """`$2b$` is the modern bcrypt prefix; the cost travels inside it."""
        digest = hash_password(VALID)

        assert digest.startswith("$2b$")
        assert len(digest) == 60
        assert digest.split("$")[2].isdigit(), "no cost factor in the digest"

    def test_the_cost_factor_is_at_least_twelve(self) -> None:
        """Not pinned to an exact value — bcrypt's default should be free to rise.

        A hard-coded cost is a number nobody revisits. A floor is a number that
        fails if the library ever moves it *down*.
        """
        cost = int(hash_password(VALID).split("$")[2])

        assert cost >= 12

    def test_two_hashes_of_one_password_differ(self) -> None:
        """Salting, observed rather than assumed.

        Identical digests for identical passwords would mean a stolen table
        reveals which accounts share a password before any cracking begins.
        """
        assert hash_password(VALID) != hash_password(VALID)

    def test_both_hashes_still_verify(self) -> None:
        """The other half of the same property: different, and both correct."""
        for _ in range(3):
            assert verify_password(VALID, hash_password(VALID))

    def test_the_correct_password_verifies(self) -> None:
        assert verify_password(VALID, hash_password(VALID)) is True

    @pytest.mark.parametrize(
        "wrong",
        [
            "correct-horse-batterz",
            "correct-horse-batter",
            "correct-horse-batteryy",
            "CORRECT-HORSE-BATTERY",
            "",
            " correct-horse-battery",
        ],
        ids=["one-char", "truncated", "extra-char", "case", "empty", "leading-space"],
    )
    def test_a_wrong_password_does_not_verify(self, wrong: str) -> None:
        assert verify_password(wrong, hash_password(VALID)) is False


class TestVerificationNeverRaises:
    """A failure here would be an authentication path that 500s instead of denying."""

    @pytest.mark.parametrize(
        "stored",
        [None, "", "not-a-hash", "$2b$12$too-short", "$999$12$xxxx", "null"],
        ids=["none", "empty", "garbage", "truncated", "bad-variant", "literal-null"],
    )
    def test_a_missing_or_malformed_hash_is_false_not_an_error(
        self, stored: str | None
    ) -> None:
        assert verify_password(VALID, stored) is False

    def test_a_null_hash_is_the_state_every_existing_row_is_in(self) -> None:
        """S2.3 adds the column; nothing writes it until S2.4.

        So "no password set" is not an edge case to be defended against — it is
        the only state any account is currently in, and it must deny cleanly.
        """
        assert verify_password("anything at all", None) is False

    def test_an_empty_candidate_against_a_real_hash_is_false(self) -> None:
        assert verify_password("", hash_password(VALID)) is False


class TestPolicyLength:
    def test_the_minimum_is_twelve_characters(self) -> None:
        assert MINIMUM_LENGTH == 12

    def test_exactly_twelve_characters_is_accepted(self) -> None:
        """The boundary, from the accepting side."""
        password = "a" * 12

        assert validate_password(password) == password
        assert verify_password(password, hash_password(password))

    @pytest.mark.parametrize("length", [0, 1, 11])
    def test_shorter_than_twelve_is_rejected(self, length: int) -> None:
        with pytest.raises(PasswordPolicyError):
            validate_password("a" * length)

    def test_the_rejection_names_the_rule(self) -> None:
        with pytest.raises(PasswordPolicyError, match="at least 12 characters"):
            validate_password("short")

    def test_no_error_message_contains_the_password(self) -> None:
        """An error carrying the secret puts it in logs and issue reports."""
        secret = "z" * 200

        with pytest.raises(PasswordPolicyError) as raised:
            validate_password(secret)

        assert secret not in str(raised.value)
        assert "z" * 12 not in str(raised.value)

    def test_exactly_seventy_two_bytes_is_accepted(self) -> None:
        """The other boundary, from the accepting side."""
        password = "a" * MAXIMUM_BYTES

        assert len(password.encode("utf-8")) == 72
        assert verify_password(password, hash_password(password))

    def test_seventy_three_bytes_is_rejected(self) -> None:
        with pytest.raises(PasswordPolicyError, match="at most 72 bytes"):
            validate_password("a" * 73)

    def test_hashing_refuses_an_over_long_password(self) -> None:
        """The policy is not separable from hashing.

        A `hash_password` that accepted anything would let a caller who forgot
        to validate store a truncated credential, and the policy would be
        advisory.
        """
        with pytest.raises(PasswordPolicyError):
            hash_password("a" * 100)


class TestNoSilentTruncation:
    """The property the byte limit exists to protect, proven against bcrypt itself."""

    def test_raw_bcrypt_does_truncate_and_does_not_complain(self) -> None:
        """Why an explicit check is needed rather than trusting the library.

        bcrypt 4.3.0 accepts a password of any length and ignores everything
        past byte 72 — it does not raise. So a 97-byte password verifies
        against a hash built from its first 72 bytes. Measured here so that a
        future bcrypt release which *does* raise is noticed as a change rather
        than assumed.
        """
        seventy_two = b"a" * 72
        digest = bcrypt.hashpw(seventy_two, bcrypt.gensalt())

        assert bcrypt.checkpw(seventy_two + b"-COMPLETELY-DIFFERENT-TAIL", digest)

    def test_our_policy_refuses_what_bcrypt_would_have_truncated(self) -> None:
        """The same input, through the boundary that exists to stop it."""
        with pytest.raises(PasswordPolicyError):
            hash_password("a" * 72 + "-COMPLETELY-DIFFERENT-TAIL")

    def test_an_over_long_candidate_cannot_verify_a_shorter_password(self) -> None:
        """The truncation collision, closed at verification too.

        Every stored hash came through `hash_password`, which refuses anything
        over 72 bytes — so a longer candidate cannot be the password that was
        enrolled. It can only be a prefix collision, and it is refused.
        """
        enrolled = "a" * 72
        digest = hash_password(enrolled)

        assert verify_password(enrolled, digest) is True
        assert verify_password(enrolled + "x", digest) is False

    def test_a_null_byte_is_rejected(self) -> None:
        """Never legitimate input, and a portability hazard.

        bcrypt 4.3.0 hashes the full byte string, so this is defence in depth
        rather than a live defect — but the reference C implementation stops at
        a NUL, and a stored hash should not mean different things depending on
        what verifies it.
        """
        with pytest.raises(PasswordPolicyError, match="null byte"):
            validate_password("valid-enough\x00ignored")


class TestPolicyHasNoCompositionRules:
    """NIST SP 800-63B has advised against these since 2017.

    Requiring a symbol and a capital is what produces `Password1!`: it shrinks
    the space attackers actually search far more than it grows the space they
    theoretically must.
    """

    @pytest.mark.parametrize(
        "password",
        [
            "aaaaaaaaaaaa",
            "all lower case words",
            "ALL UPPER CASE WORDS",
            "123456789012",
            "____________",
            "correct horse battery staple",
            "ЯЯЯЯЯЯЯЯЯЯЯЯ",
            "🙂🙂🙂🙂🙂🙂🙂🙂🙂🙂🙂🙂",
        ],
        ids=[
            "repeated",
            "lower-only",
            "upper-only",
            "digits-only",
            "punctuation-only",
            "passphrase",
            "non-latin",
            "emoji",
        ],
    )
    def test_a_long_enough_password_is_accepted_whatever_it_contains(
        self, password: str
    ) -> None:
        assert validate_password(password)
        assert verify_password(password, hash_password(password))


class TestUnicodeNormalisation:
    def test_normalisation_is_nfkc(self) -> None:
        raw = "ﬁre-and-motion"

        assert normalize_password(raw) == unicodedata.normalize("NFKC", raw)

    def test_the_two_ways_to_type_an_accent_are_one_password(self) -> None:
        """`é` as one code point and as `e` + combining acute.

        Which one a keyboard produces is not something a user chooses or can
        see. Without normalisation, enrolling on one device and signing in from
        another would fail with no explanation available to anyone.
        """
        composed = "café-au-lait-please"
        decomposed = "café-au-lait-please"

        assert composed != decomposed
        assert verify_password(decomposed, hash_password(composed)) is True
        assert verify_password(composed, hash_password(decomposed)) is True

    def test_a_compatibility_ligature_matches_its_expansion(self) -> None:
        """NFKC rather than NFC: `ﬁ` and `fi` are the same password."""
        assert verify_password("fire-and-motion", hash_password("ﬁre-and-motion"))

    def test_full_width_characters_match_their_ascii_forms(self) -> None:
        """An IME can produce these without the user intending anything by it."""
        assert verify_password("passphrase12", hash_password("ｐａｓｓｐｈｒａｓｅ12"))

    def test_normalisation_happens_before_the_length_check(self) -> None:
        """NFKC changes length, so the order is observable.

        Eleven `ﬁ` ligatures are 11 characters before normalisation and 22
        after. Measuring first would reject a password that is comfortably long
        in the form actually hashed.
        """
        raw = "ﬁ" * 11

        assert len(raw) < MINIMUM_LENGTH
        assert len(normalize_password(raw)) >= MINIMUM_LENGTH
        assert validate_password(raw) == "fi" * 11

    def test_the_byte_limit_is_bytes_and_not_characters(self) -> None:
        """The whole reason the policy is expressed in bytes.

        Thirty CJK characters are well under any character limit and 90 bytes
        in UTF-8 — bcrypt would keep 72 of them and silently discard the rest.
        """
        password = "研" * 30

        assert len(password) == 30
        assert len(password.encode("utf-8")) == 90

        with pytest.raises(PasswordPolicyError, match="at most 72 bytes"):
            validate_password(password)

    def test_a_multibyte_password_within_the_limit_is_accepted(self) -> None:
        """The control for the test above: bytes, not a ban on non-ASCII."""
        password = "研" * 24

        assert len(password.encode("utf-8")) == 72
        assert verify_password(password, hash_password(password))

    def test_validate_returns_the_normalised_form_that_gets_hashed(self) -> None:
        """Callers must store what was validated, not what arrived."""
        assert validate_password("ﬁre-and-motion") == "fire-and-motion"
