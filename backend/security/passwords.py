"""Password hashing, and the policy that bounds what may be hashed.

One module, so there is exactly one answer to "what is a valid password" and
one answer to "how is it stored". S2.4 builds registration and login on top of
this and adds no rules of its own — a policy restated at two call sites is a
policy that will disagree with itself.

Nothing here touches the database, and nothing here knows about HTTP.
"""

import secrets
import unicodedata
from functools import lru_cache
from typing import Annotated

import bcrypt
from pydantic import AfterValidator

# Twelve characters, measured after normalisation, and no composition rules.
#
# Mandating an uppercase letter, a digit and a symbol is what produces
# `Password1!` — it narrows the search space far more than it widens it, and
# NIST SP 800-63B has recommended against composition rules since 2017. Length
# is the property that actually costs an attacker something.
MINIMUM_LENGTH = 12

# bcrypt's input limit, and the reason the policy is expressed in *bytes* as
# well as characters. The algorithm reads at most 72 bytes of the password;
# anything beyond that contributes nothing to the hash. A 30-character password
# of CJK text is 90 bytes in UTF-8, so a character count alone would let it
# through and silently discard a third of it.
MAXIMUM_BYTES = 72


class PasswordPolicyError(ValueError):
    """A password does not satisfy the policy.

    A ValueError subclass on purpose: pydantic turns ValueError raised inside a
    validator into a normal ValidationError, so the same function can guard a
    schema field and a direct call without a translation layer.

    The message states the rule that was broken and never the value. An error
    carrying the password would put it in logs, tracebacks and issue reports —
    which is the failure this whole module exists to avoid.
    """


def normalize_password(raw: str) -> str:
    """Return the NFKC form of a password.

    Applied before hashing *and* before verification, and it has to be both:
    normalising only on the way in means a user whose keyboard produces a
    compatibility form — a full-width character, a composed ligature, one of
    the several ways to type an accented letter — enrols successfully and can
    never sign in again.

    NFKC rather than NFC because it folds compatibility variants too, so `ﬁ`
    and `fi` are the same password. That is a deliberate loss of distinctness:
    the alternative is a password whose correctness depends on which input
    method typed it.
    """
    return unicodedata.normalize("NFKC", raw)


def validate_password(raw: str) -> str:
    """Normalise a password and check it against the policy.

    Returns the **normalised** password, so callers store and hash the form
    that was actually validated rather than the one that arrived.

    :raises PasswordPolicyError: if it is too short, or too long for bcrypt.
    """
    normalised = normalize_password(raw)

    # Length is measured after normalisation because normalisation is what
    # changes it: NFKC expands `ﬁ` into two characters and collapses `①` into
    # one. The form that gets hashed is the form that must satisfy the rule.
    if len(normalised) < MINIMUM_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MINIMUM_LENGTH} characters"
        )

    encoded = normalised.encode("utf-8")
    if len(encoded) > MAXIMUM_BYTES:
        # Rejected, never truncated. Silently cutting to 72 bytes would mean
        # two different passwords hash identically and the longer one is
        # weaker than the user believes it to be.
        raise PasswordPolicyError(
            f"password must be at most {MAXIMUM_BYTES} bytes when UTF-8 encoded "
            f"(it is {len(encoded)}); bcrypt ignores anything beyond that"
        )

    if "\x00" in normalised:
        # Defence in depth, and measured rather than assumed: bcrypt 4.3.0
        # hashes the full byte string, so `secret\x00tail` does *not* match a
        # hash of `secret` here. The reference OpenBSD implementation and
        # several bindings treat the password as a C string and stop at the
        # NUL, which would make a stored hash mean different things depending
        # on what verified it. A NUL in a password field is also never
        # legitimate input, so the cheap answer is to refuse it.
        raise PasswordPolicyError("password must not contain a null byte")

    return normalised


def hash_password(raw: str) -> str:
    """Validate a password and return its bcrypt hash.

    Validation is not optional here and not separable from it: a `hash_password`
    that accepted anything would let a caller who forgot to validate store a
    two-character password, and the policy would be advisory.

    The salt is generated per call by `bcrypt.gensalt()` and travels inside the
    returned hash, so two hashes of the same password never match each other.
    The cost factor is bcrypt's own default rather than a number chosen here —
    a hard-coded cost is a number nobody revisits, and the library's default
    moves with the hardware.

    :raises PasswordPolicyError: if the password is not acceptable.
    """
    normalised = validate_password(raw)
    return bcrypt.hashpw(normalised.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(raw: str, password_hash: str | None) -> bool:
    """Check a password against a stored hash. Never raises.

    Returns False for every failure — wrong password, malformed hash, a hash
    that is NULL because the account predates credentials. A caller's decision
    is binary, and an exception escaping here would be an authentication path
    that can 500 instead of denying.

    **The policy is deliberately not applied.** `hash_password` decides what may
    be *created*; this decides only whether a candidate matches. Enforcing the
    minimum length here would mean a user whose password predates a tightening
    of the policy could never sign in — and it would make "too short" externally
    distinguishable from "wrong", which is a free bit for an attacker.

    The 72-byte bound *is* applied, because it is the algorithm's and not the
    policy's. bcrypt 4.3.0 does not reject an over-long password: it silently
    ignores everything past byte 72, so a 97-byte candidate sharing a 72-byte
    prefix verifies against the shorter password's hash — measured, not
    assumed. Since every hash this system stores came through `hash_password`,
    which refuses anything longer, a longer candidate cannot be the password
    that was enrolled. It can only be a truncation collision, so it is False.

    Comparison is `bcrypt.checkpw`, which is constant-time over the digest. The
    hash is never compared with `==`.
    """
    if not password_hash:
        # No credentials on this account — and the expensive answer, on purpose.
        #
        # Returning here immediately would make "no such account" and "account
        # with no password" answer in under a millisecond while a real account
        # spends the bcrypt cost, which at cost 12 is a ~280ms difference
        # measurable over the network by anyone with a stopwatch. That is an
        # account-enumeration oracle that no amount of identical response
        # bodies can close.
        #
        # So a verification is performed against a throwaway hash of the same
        # cost and its result discarded. Doing it *here* rather than in the
        # caller is deliberate: a caller that forgets reintroduces the oracle,
        # and principles.md §3 asks that the safe path be the only path.
        _verify(raw, _absent_password_hash())
        return False

    return _verify(raw, password_hash)


def _verify(raw: str, password_hash: str) -> bool:
    """The bcrypt comparison itself, shared by the real and throwaway paths.

    One implementation so the two cost the same thing; two would eventually
    differ by a normalisation step or an encode, and the difference would be
    exactly the signal the throwaway path exists to hide.
    """
    candidate = normalize_password(raw).encode("utf-8")
    if len(candidate) > MAXIMUM_BYTES:
        return False

    try:
        return bcrypt.checkpw(candidate, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # An invalid salt, a truncated hash, a non-bcrypt string in the column,
        # or a candidate bcrypt refuses to consider. All of them mean the same
        # thing to the caller.
        #
        # This path *is* fast, and that is acceptable: nothing writes a
        # malformed hash, so an attacker has no way to put a row into this
        # state and no way to observe the difference.
        return False


@lru_cache(maxsize=1)
def _absent_password_hash() -> str:
    """A hash of a random value nobody knows, built once and reused.

    Random rather than a fixed string so there is no question of what happens
    if someone submits the dummy password: no one can. Built through
    `hash_password`, so its cost factor tracks the cost factor of real hashes —
    a hard-coded digest would silently stop matching the moment bcrypt's
    default moved, and the timing gap would reopen without a test failing.

    Computed lazily: doing it at import would add the cost of one bcrypt round
    to process startup, including for processes that never authenticate anyone.
    """
    return hash_password(secrets.token_urlsafe(32))


# The schema-level expression of the same policy.
#
# `AfterValidator(validate_password)` rather than pydantic's own `min_length`,
# so there is one implementation rather than two that must be kept in step —
# and so the field value is the *normalised* password, already in the form that
# will be hashed.
Password = Annotated[str, AfterValidator(validate_password)]
