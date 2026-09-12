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

    # One known violation, inherited from the original scaffold and recorded
    # rather than hidden: DocumentService.upload_and_process is typed
    # `file: UploadFile`. It is an M3 method and still a stub, so changing the
    # signature belongs to the sprint that implements it. This exemption is
    # deliberately exact — any *other* FastAPI import in a service fails.
    KNOWN_DEBT = {("document_service.py", "from fastapi import UploadFile")}

    @pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
    def test_no_unrecorded_fastapi_import_in_a_service(
        self, path: pathlib.Path
    ) -> None:
        offenders = {
            line.strip()
            for line in _code(path).splitlines()
            if "fastapi" in line and line.strip().startswith(("import ", "from "))
        }
        allowed = {text for name, text in self.KNOWN_DEBT if name == path.name}

        assert offenders <= allowed, f"{path.name}: {offenders - allowed}"

    def test_the_known_debt_still_exists(self) -> None:
        """Fails once M3 removes it, so the exemption cannot outlive the debt."""
        source = (ROOT / "backend" / "services" / "document_service.py").read_text(
            encoding="utf-8"
        )

        assert (
            "from fastapi import UploadFile" in source
        ), "the UploadFile debt is gone — delete KNOWN_DEBT and this test"
