"""Architectural invariants, asserted so a refactor cannot quietly undo them.

These read source text rather than behaviour, which is unusual and deliberate:
the properties here are *structural*. A service that opened its own session
would still pass every functional test in the suite, right up until two
operations that were supposed to be atomic turned out not to be.

Kept narrow on purpose. Each one encodes a decision made in S1.1–S1.4 with a
reason, not a style preference.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
REPOSITORIES = sorted((ROOT / "backend" / "db" / "repositories").glob("*.py"))
SERVICES = sorted((ROOT / "backend" / "services").glob("*.py"))


def _code(path: pathlib.Path) -> str:
    """Source with comments and docstring prose stripped of the words we grep.

    Without this, a docstring explaining *why* repositories do not commit would
    itself fail the test that checks they do not commit.
    """
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split("  #")[0])
    return "\n".join(lines)


def _statements(path: pathlib.Path) -> str:
    """Executable lines only — no docstrings at all."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)


class TestRepositoriesDoNotOwnTransactions:
    @pytest.mark.parametrize("path", REPOSITORIES, ids=lambda p: p.name)
    def test_a_repository_never_commits(self, path: pathlib.Path) -> None:
        """The caller owns the transaction.

        A repository that committed halfway through a service workflow would
        make the earlier half durable and the later half not — which is the
        precise failure the transaction contract exists to prevent. It would
        also make ADR-0003's cross-store deletion order unexpressible, since the
        service could no longer choose when the PostgreSQL half lands.
        """
        assert ".commit(" not in _statements(path)

    @pytest.mark.parametrize("path", REPOSITORIES, ids=lambda p: p.name)
    def test_a_repository_never_rolls_back(self, path: pathlib.Path) -> None:
        """Rolling back would discard work the repository did not do."""
        assert ".rollback(" not in _statements(path)

    @pytest.mark.parametrize("path", REPOSITORIES, ids=lambda p: p.name)
    def test_a_repository_never_builds_its_own_session(
        self, path: pathlib.Path
    ) -> None:
        """A hidden session would silently sit outside the caller's transaction."""
        code = _statements(path)

        assert "get_sessionmaker" not in code
        assert "create_async_engine" not in code
        assert "get_engine" not in code


class TestServicesDoNotOwnPersistence:
    @pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
    def test_a_service_never_builds_its_own_session(self, path: pathlib.Path) -> None:
        """Sessions arrive by injection, so a service can join a transaction.

        A service that constructed its own could not participate in a caller's
        unit of work — the thing that makes multi-repository operations atomic.
        """
        code = _statements(path)

        assert "get_sessionmaker" not in code
        assert "create_async_engine" not in code

    @pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
    def test_a_service_never_writes_sql(self, path: pathlib.Path) -> None:
        """Query construction belongs to repositories.

        A `select()` here would be a query with no ownership predicate and no
        test asserting one — precisely the hole S1.3 closed.
        """
        code = _statements(path)

        assert "sqlalchemy import select" not in code
        assert "sqlalchemy import text" not in code
        assert "session.execute(" not in code


class TestServiceCoreStaysAdapterFree:
    """ADR-0001 §2: no adapter type crosses into the Service Core.

    The core is shared by the REST and MCP adapters. A FastAPI type in a service
    signature means the MCP adapter has to construct one, which is how a "shared
    core" quietly becomes a REST core with an MCP wrapper.
    """

    # There used to be one recorded exemption here: DocumentService's upload
    # stub was typed `file: UploadFile`, and this class carried a KNOWN_DEBT set
    # plus a test that failed once the debt was gone, "so the exemption cannot
    # outlive the debt". M3/S3.1 implemented upload with a `BinaryIO` stream and
    # that test failed exactly as designed. Exemption and test are deleted; any
    # FastAPI import in a service now fails.

    @pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
    def test_no_fastapi_import_in_a_service(self, path: pathlib.Path) -> None:
        offenders = {
            line.strip()
            for line in _code(path).splitlines()
            if "fastapi" in line and line.strip().startswith(("import ", "from "))
        }

        assert not offenders, f"{path.name}: {offenders}"


class TestAuthenticationCannotReturnToAFailOpenContract:
    """principles.md §1: `get_current_user` returns a `Principal` or **raises**.

    Read structurally, by design. Every behavioural test in
    `tests/integration/test_authentication.py` asserts what happens for the
    tokens we thought to write down; these assert the *shape* of the contract,
    which is what stops a future signature change from making all of them
    irrelevant. The original bug was a signature — `-> User` over a body of
    `...` — and no behavioural test existed to catch it.
    """

    @staticmethod
    def _resolved_hints() -> dict[str, object]:
        import typing

        from backend.security import api_security

        return typing.get_type_hints(api_security.get_current_user)

    def test_it_is_annotated_as_returning_a_principal(self) -> None:
        from shared.models.principal import Principal

        assert self._resolved_hints()["return"] is Principal

    def test_it_is_not_annotated_as_optional(self) -> None:
        """`Optional[Principal]` is the fail-open contract with a new name.

        A route that receives one has to decide what None means, and deciding
        that in a route is how the original bypass reached production.
        """
        import typing

        returns = self._resolved_hints()["return"]

        assert typing.get_origin(returns) is not typing.Union
        assert type(None) not in typing.get_args(returns)

    def test_it_does_not_return_the_persistence_model(self) -> None:
        """`User` describes a row; `Principal` asserts an authentication.

        They have the same fields and opposite meanings. A `User` can be built
        from a request body — which is exactly the identity source
        principles.md §2 forbids.
        """
        from shared.models.user import User

        assert self._resolved_hints()["return"] is not User

    def test_its_body_contains_no_return_of_none(self) -> None:
        """No sentinel, however it is spelled.

        `return None`, a bare `return`, or a body that falls off the end all
        produce the same None the bypass depended on. The type alone would not
        catch these — mypy's `empty-body` check was reporting the original
        defect for the whole of M0 and M1, and it was not enforced.
        """
        import ast
        import inspect

        from backend.security import api_security

        source = inspect.getsource(api_security.get_current_user)
        tree = ast.parse(source.strip())

        for node in ast.walk(tree):
            if isinstance(node, ast.Return):
                assert node.value is not None, "bare `return` in get_current_user"
                assert not (
                    isinstance(node.value, ast.Constant) and node.value.value is None
                ), "`return None` in get_current_user"

    def test_every_protected_route_takes_a_principal_not_a_user(self) -> None:
        """The other half: a route may not re-widen what it was handed.

        Annotating the parameter `User` would still *work* — FastAPI does not
        validate a dependency's return against it — which is what makes it worth
        pinning. It would be a lie that reads as documentation.
        """
        routers = sorted((ROOT / "backend" / "api" / "routers").glob("*.py"))
        # Every way a route can obtain an identity. Until M2/S2.5 routes wrote
        # `Depends(get_current_user)` directly; since then they write
        # `Depends(require_permission(...))`, and a check that only knew the
        # first spelling passed on zero lines — guarding nothing, silently.
        guards = (
            "Depends(get_current_user)",
            "Depends(require_permission(",
            "Depends(require_role(",
        )
        inspected = [
            (path.name, number, line)
            for path in routers
            for number, line in enumerate(_code(path).splitlines(), start=1)
            if any(guard in line for guard in guards)
        ]
        offenders = [
            f"{name}:{number}"
            for name, number, line in inspected
            if "Principal" not in line
        ]

        # Non-vacuity. Nine protected routes, so at least nine guarded lines; a
        # renamed dependency that no spelling above matches fails here instead
        # of letting the assertion below pass on an empty list.
        assert len(inspected) >= 9, f"only {len(inspected)} guarded route lines found"
        assert not offenders, offenders


class TestPasslibIsGone:
    """M2/S2.3 removed it. These make the removal stay removed.

    passlib was declared from the scaffold onward and never imported, so this
    is not the removal of something load-bearing — it is closing a door. Left
    installed, it selects a hashing backend at import time and falls through to
    stdlib `crypt` when no bcrypt backend is present; `crypt` was removed in
    Python 3.13. A password library that silently changes algorithm depending
    on what else is installed is the wrong shape for this particular boundary.
    """

    PRODUCTION = [
        path
        for directory in (
            "backend",
            "shared",
            "agents",
            "document_processing",
            "vector_db",
            "memory_system",
            "mcp_server",
            "devops",
            "alembic",
        )
        for path in sorted((ROOT / directory).rglob("*.py"))
        if "__pycache__" not in path.parts
    ]

    def test_it_is_not_importable(self) -> None:
        """The behavioural check, and the one that cannot be fooled.

        Grepping for `import passlib` proves nobody wrote the line. This proves
        the package is not in the environment at all, so a line added later
        fails rather than quietly working.
        """
        import importlib.util

        assert importlib.util.find_spec("passlib") is None

    def test_no_production_module_imports_it(self) -> None:
        offenders = [
            f"{path.relative_to(ROOT)}"
            for path in self.PRODUCTION
            if "passlib" in path.read_text(encoding="utf-8")
        ]

        assert not offenders, offenders

    def test_no_crypt_context_survives_anywhere(self) -> None:
        """passlib's entry point. Its absence is what "no dead compatibility code" means."""
        offenders = [
            f"{path.relative_to(ROOT)}"
            for path in self.PRODUCTION
            if "CryptContext" in path.read_text(encoding="utf-8")
        ]

        assert not offenders, offenders

    def test_it_is_not_a_declared_dependency(self) -> None:
        """The declaration, not the word.

        pyproject.toml explains in a comment why bcrypt replaced passlib, and
        that explanation is worth keeping — so this looks for the dependency
        line rather than any mention.
        """
        declarations = [
            line
            for line in (ROOT / "pyproject.toml")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip().startswith("passlib")
        ]

        assert not declarations, declarations

    def test_it_is_not_in_the_lock_file(self) -> None:
        """A dependency removed from pyproject but left in the lock still installs."""
        assert 'name = "passlib"' not in (ROOT / "poetry.lock").read_text(
            encoding="utf-8"
        )


class TestHashingHasOneImplementation:
    """The policy is only centralised if the primitive is."""

    def test_bcrypt_is_a_declared_direct_dependency(self) -> None:
        """Not relied on transitively. Nothing else in the tree pulls it."""
        assert "bcrypt = " in (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_only_the_passwords_module_touches_bcrypt(self) -> None:
        """A second call site is a second set of decisions about cost and salt.

        Scoped to production code: `tests/unit/test_passwords.py` imports bcrypt
        deliberately, to pin the library's own truncation behaviour.
        """
        offenders = [
            f"{path.relative_to(ROOT)}"
            for path in TestPasslibIsGone.PRODUCTION
            if path.name != "passwords.py" and "bcrypt" in _statements(path).lower()
        ]

        assert not offenders, offenders


class TestIdentityReachesTheServiceAsAPrincipal:
    """principles.md §2: no service method accepts a caller-supplied `user_id`.

    M2/S2.5 moved `DocumentService` from `user_id: str` to `principal: Principal`.
    These keep it moved, and keep the `Principal` that arrives there honest.
    """

    def test_no_document_service_method_takes_a_user_id(self) -> None:
        """A bare string identity is exactly what a route could fill from a request."""
        import inspect

        from backend.services.document_service import DocumentService

        methods = [
            (name, member)
            for name, member in inspect.getmembers(DocumentService, inspect.isfunction)
            if not name.startswith("_")
        ]
        offenders = [
            name
            for name, member in methods
            if "user_id" in inspect.signature(member).parameters
        ]

        assert len(methods) == 4, [name for name, _ in methods]
        assert not offenders, offenders

    def test_every_document_service_method_requires_a_principal(self) -> None:
        import inspect
        import typing

        from backend.services.document_service import DocumentService
        from shared.models.principal import Principal

        for name, member in inspect.getmembers(DocumentService, inspect.isfunction):
            if name.startswith("_"):
                continue
            hints = typing.get_type_hints(member)
            assert (
                hints.get("principal") is Principal
            ), f"{name} does not take a Principal"

    def test_production_code_constructs_a_principal_in_exactly_one_place(self) -> None:
        """The resolver is the trusted construction point; nothing else may mint one.

        Read from the AST, so the class definition and prose mentioning the
        name are not mistaken for construction. A `Principal(...)` call anywhere
        else in production code would be a second identity source — one that
        need not have verified a token.
        """
        import ast

        production = [
            path
            for directory in ("backend", "shared", "mcp_server", "agents")
            for path in sorted((ROOT / directory).rglob("*.py"))
            if "__pycache__" not in path.parts
        ]
        constructors = [
            f"{path.relative_to(ROOT)}:{node.lineno}"
            for path in production
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Principal"
        ]

        assert len(constructors) == 1, constructors
        assert constructors[0].startswith("backend/security/authentication.py:")


class TestAuthorisationIsDecidedInOnePlace:
    """Brief §6: no `if principal.role == ...` spread across handlers."""

    ROUTERS = sorted((ROOT / "backend" / "api" / "routers").glob("*.py"))

    @pytest.mark.parametrize("path", ROUTERS, ids=lambda p: p.name)
    def test_no_router_inspects_a_role(self, path: pathlib.Path) -> None:
        """A role comparison in a handler is a second, unreviewed copy of the policy."""
        code = _statements(path)

        assert "UserRole" not in code
        assert ".role" not in code

    @pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
    def test_no_service_inspects_a_role(self, path: pathlib.Path) -> None:
        """Services scope by ownership; which roles may call them is the route's guard."""
        assert ".role ==" not in _statements(path)
        assert "has_permission" not in _statements(path)

    def test_the_policy_imports_no_web_framework(self) -> None:
        """So the MCP adapter can ask the same question (ADR-0002 §6)."""
        code = _statements(ROOT / "backend" / "security" / "rbac.py")

        assert "fastapi" not in code
        assert "starlette" not in code


class TestStorageIsReachedOnlyThroughItsContract:
    """ADR-0008: the service talks to `Storage`; only a backend touches a disk."""

    @pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
    def test_no_service_touches_the_filesystem(self, path: pathlib.Path) -> None:
        """A service that opened a path could be handed one built from a filename.

        Everything a service stores goes through `Storage.put`, whose keys come
        only from `document_storage_key`.
        """
        code = _statements(path)

        for forbidden in (
            "open(",
            "Path(",
            "os.path",
            "shutil",
            "tempfile",
            "os.remove",
        ):
            assert forbidden not in code, f"{path.name} uses {forbidden}"

    def test_no_storage_key_is_built_from_a_filename(self) -> None:
        """Every `document_storage_key(...)` in production code, read from the AST.

        None of its arguments may mention a filename or a display name. The
        behavioural traversal tests would catch the consequence; this catches
        the line.
        """
        import ast

        production = [
            path
            for directory in ("backend", "shared", "document_processing", "mcp_server")
            for path in sorted((ROOT / directory).rglob("*.py"))
            if "__pycache__" not in path.parts
        ]
        calls = [
            (path, node)
            for path in production
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            == "document_storage_key"
        ]
        offenders = [
            f"{path.relative_to(ROOT)}:{node.lineno}"
            for path, node in calls
            if any(
                word in ast.unparse(argument).lower()
                for argument in node.args + [k.value for k in node.keywords]
                for word in ("filename", "name", "file.")
            )
        ]

        assert calls, "document_storage_key is never called — the check is vacuous"
        assert not offenders, offenders
