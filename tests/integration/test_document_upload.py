"""Document upload, end to end — M3/S3.1.

The real application over ASGI, real PostgreSQL, a real filesystem under
`tmp_path`, and real PDFs. The one seam replaced is the ingestion queue, with a
recorder, because asserting what the worker is handed is the point of the
boundary and there is no worker yet (ADR-0009, next sprint).

Every layer M1 and M2 built is exercised again here, through a route that now
writes: authentication (401), authorisation (403), identity from the Principal
only, ownership in the SQL (404). Upload must not have become a way around any
of them.
"""

import asyncio
import hashlib
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
from pydantic import ValidationError
from sqlalchemy import text, update
from structlog.testing import capture_logs

from backend.api.app import app
from backend.api.dependencies.services import get_ingestion_queue
from backend.config.settings import get_settings
from backend.db.models import UserORM
from backend.db.repositories import DocumentRepository, UserRepository
from backend.ingestion import DeferredIngestionQueue
from backend.security.jwt_handler import JWTHandler
from backend.storage import LocalStorage
from shared.models.ingestion import IngestionJob
from shared.models.user import User, UserRole
from tests.pdfs import make_encrypted_pdf, make_owner_restricted_pdf, make_pdf

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("engine_isolation", "migrated_schema"),
]

UPLOAD = "/api/v1/documents/upload"
V, R, A = UserRole.VIEWER, UserRole.RESEARCHER, UserRole.ADMIN


# -------------------------------------------------------------------- fixtures


class RecordingQueue:
    """Records jobs, and checks at enqueue time that the row is already committed.

    The check runs in a separate session, so an uncommitted row is invisible to
    it. That is how "enqueue happens after commit" is observed rather than
    assumed.
    """

    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []
        self.committed_at_enqueue: list[bool] = []

    async def enqueue(self, job: IngestionJob) -> None:
        from backend.db.session import get_sessionmaker

        async with get_sessionmaker()() as other:
            found = (
                await other.execute(
                    text("SELECT 1 FROM documents WHERE id = :id"),
                    {"id": uuid.UUID(job.document_id)},
                )
            ).scalar_one_or_none()
        self.committed_at_enqueue.append(found == 1)
        self.jobs.append(job)


@pytest.fixture(autouse=True)
async def _truncate_after_each_test() -> AsyncIterator[None]:
    yield
    from backend.db.engine import get_engine

    async with get_engine().begin() as connection:
        await connection.execute(
            text("TRUNCATE document_chunks, documents, users RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh storage root for this test, picked up by the real provider."""
    root = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(root))
    get_settings.cache_clear()
    return root


@pytest.fixture
def queue() -> Iterator[RecordingQueue]:
    recorder = RecordingQueue()
    app.dependency_overrides[get_ingestion_queue] = lambda: recorder
    try:
        yield recorder
    finally:
        app.dependency_overrides.pop(get_ingestion_queue, None)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    def set_limits(
        *, max_bytes: int | None = None, max_pages: int | None = None
    ) -> None:
        if max_bytes is not None:
            monkeypatch.setenv("MAX_UPLOAD_BYTES", str(max_bytes))
        if max_pages is not None:
            monkeypatch.setenv("MAX_PDF_PAGES", str(max_pages))
        get_settings.cache_clear()

    return set_limits


async def _account(session: Any, role: UserRole = R) -> tuple[User, dict[str, str]]:
    user = await UserRepository(session).create(
        email=f"{uuid.uuid4()}@example.com", full_name="Uploader", role=role
    )
    await session.commit()
    token = JWTHandler().create_access_token(user.id, user.email, user.role)
    return user, {"Authorization": f"Bearer {token}"}


async def _upload(
    client: Any,
    headers: dict[str, str],
    content: bytes,
    filename: str = "paper.pdf",
    content_type: str = "application/pdf",
    **kwargs: Any,
) -> Any:
    """`content` is the file; httpx's own `data=` (form fields) passes through."""
    return await client.post(
        UPLOAD,
        headers=headers,
        files={"file": (filename, content, content_type)},
        **kwargs,
    )


async def _rows(session: Any) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT id, user_id, filename, doc_type, status, storage_key, content_hash,"
            " size_bytes, mime_type, page_count, deleted_at FROM documents"
            " ORDER BY created_at"
        )
    )
    return [dict(row._mapping) for row in result]


def _files(root: Path) -> set[str]:
    return (
        {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        if root.exists()
        else set()
    )


# =============================================================================
# Authentication and authorisation
# =============================================================================


class TestOnlyAPermittedIdentityCanUpload:
    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer totally-fake-not-a-jwt"},
            {"Authorization": "Basic dXNlcjpwYXNz"},
        ],
        ids=["no-token", "forged", "wrong-scheme"],
    )
    async def test_unauthenticated_is_401_and_nothing_is_kept(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
        headers: dict[str, str],
    ) -> None:
        response = await _upload(api_client, headers, make_pdf())

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert await _rows(committing_session) == []
        assert _files(storage_root) == set()
        assert queue.jobs == []

    async def test_an_expired_token_is_401(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        user, _ = await _account(committing_session)
        expired = JWTHandler().create_access_token(
            user.id, user.email, user.role, expires_delta=timedelta(minutes=-1)
        )

        response = await _upload(
            api_client, {"Authorization": f"Bearer {expired}"}, make_pdf()
        )

        assert response.status_code == 401
        assert _files(storage_root) == set()

    async def test_a_viewer_is_403_and_nothing_is_kept(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        _, viewer = await _account(committing_session, V)

        response = await _upload(api_client, viewer, make_pdf())

        assert response.status_code == 403
        assert "www-authenticate" not in response.headers
        assert await _rows(committing_session) == []
        assert _files(storage_root) == set()
        assert queue.jobs == []

    @pytest.mark.parametrize("role", [R, A], ids=lambda r: r.value)
    async def test_researchers_and_administrators_upload(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
        role: UserRole,
    ) -> None:
        _, headers = await _account(committing_session, role)

        response = await _upload(api_client, headers, make_pdf())

        assert response.status_code == 202
        assert len(queue.jobs) == 1

    async def test_a_demoted_user_loses_upload_with_the_same_token(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        """S2.2's rule on the new write path: the database role, not the claim."""
        user, headers = await _account(committing_session, R)
        await committing_session.execute(
            update(UserORM).where(UserORM.id == uuid.UUID(user.id)).values(role=V)
        )
        await committing_session.commit()

        response = await _upload(api_client, headers, make_pdf())

        assert response.status_code == 403
        assert _files(storage_root) == set()

    async def test_a_deactivated_user_is_401(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        user, headers = await _account(committing_session, R)
        await committing_session.execute(
            update(UserORM)
            .where(UserORM.id == uuid.UUID(user.id))
            .values(is_active=False)
        )
        await committing_session.commit()

        assert (await _upload(api_client, headers, make_pdf())).status_code == 401


# =============================================================================
# The owner is the Principal, and only the Principal
# =============================================================================


class TestTheOwnerComesOnlyFromThePrincipal:
    async def test_no_request_field_can_name_another_owner(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        """Every place a client might try to put an owner, all at once.

        A form field, a query parameter and two headers, each naming a real,
        different user. The document, the file's directory and the ingestion job
        must all belong to the token's subject.
        """
        uploader, headers = await _account(committing_session)
        victim, _ = await _account(committing_session)

        response = await _upload(
            api_client,
            {**headers, "X-User-Id": victim.id, "X-Forwarded-User": victim.id},
            make_pdf(),
            data={"user_id": victim.id, "owner_id": victim.id},
            params={"user_id": victim.id, "owner_id": victim.id},
        )

        assert response.status_code == 202
        rows = await _rows(committing_session)
        assert [str(row["user_id"]) for row in rows] == [uploader.id]
        assert {path.split("/")[0] for path in _files(storage_root)} == {uploader.id}
        assert [job.owner_id for job in queue.jobs] == [uploader.id]

    async def test_the_victim_has_no_documents_afterwards(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        _, headers = await _account(committing_session)
        victim, victim_headers = await _account(committing_session)

        await _upload(api_client, headers, make_pdf(), data={"user_id": victim.id})

        listed = await api_client.get("/api/v1/documents/", headers=victim_headers)
        assert listed.json() == {"documents": [], "total": 0}


# =============================================================================
# Storage and the database record
# =============================================================================


class TestAnUploadIsStoredAndRecorded:
    async def test_the_file_is_at_owner_slash_sha256_with_the_exact_bytes(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        """ADR-0008 §2, read back from the disk."""
        user, headers = await _account(committing_session)
        data = make_pdf(pages=2)
        digest = hashlib.sha256(data).hexdigest()

        await _upload(api_client, headers, data)

        stored = storage_root / user.id / f"{digest}.pdf"
        assert _files(storage_root) == {f"{user.id}/{digest}.pdf"}
        assert stored.read_bytes() == data
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == digest

    async def test_the_row_describes_the_bytes_and_is_committed(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        """Read through a separate session, so a flush would not pass for a commit."""
        user, headers = await _account(committing_session)
        data = make_pdf(pages=3)
        digest = hashlib.sha256(data).hexdigest()

        response = await _upload(api_client, headers, data, filename="Thesis.pdf")

        rows = await _rows(committing_session)
        assert len(rows) == 1
        row = rows[0]
        assert str(row["id"]) == response.json()["id"]
        assert str(row["user_id"]) == user.id
        assert row["filename"] == "Thesis.pdf"
        assert row["doc_type"] == "pdf"
        assert row["status"] == "pending"
        assert row["storage_key"] == f"{user.id}/{digest}.pdf"
        assert row["content_hash"] == digest
        assert row["size_bytes"] == len(data)
        assert row["mime_type"] == "application/pdf"
        assert row["page_count"] == 3
        assert row["deleted_at"] is None

    async def test_the_response_is_the_document_contract_and_nothing_internal(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        """202, the existing DocumentResponse shape, and no storage location."""
        user, headers = await _account(committing_session)
        data = make_pdf()
        digest = hashlib.sha256(data).hexdigest()

        response = await _upload(api_client, headers, data, filename="paper.pdf")

        assert response.status_code == 202
        body = response.json()
        assert set(body) == {"id", "filename", "status", "metadata"}
        assert body["filename"] == "paper.pdf"
        assert body["status"] == "pending"
        for internal in (str(storage_root), user.id, digest, "storage", ".pdf/"):
            assert internal not in response.text, internal

    async def test_the_client_content_type_is_ignored_both_ways(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        """A real PDF labelled text/plain is a PDF; text labelled a PDF is not."""
        _, headers = await _account(committing_session)

        mislabelled_pdf = await _upload(
            api_client, headers, make_pdf(), content_type="text/plain"
        )
        disguised_text = await _upload(
            api_client, headers, b"just some text", content_type="application/pdf"
        )

        assert mislabelled_pdf.status_code == 202
        assert disguised_text.status_code == 415

    async def test_the_owner_can_read_it_back(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        _, headers = await _account(committing_session)
        document_id = (await _upload(api_client, headers, make_pdf())).json()["id"]

        fetched = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=headers
        )
        listed = await api_client.get("/api/v1/documents/", headers=headers)

        assert fetched.status_code == 200
        assert fetched.json()["status"] == "pending"
        assert [d["id"] for d in listed.json()["documents"]] == [document_id]


class TestTheFilenameIsMetadataOnly:
    @pytest.mark.parametrize(
        ("sent", "stored"),
        [
            ("../../../../etc/passwd.pdf", "passwd.pdf"),
            ("/etc/shadow.pdf", "shadow.pdf"),
            ("..\\..\\windows\\win.ini.pdf", "win.ini.pdf"),
            ("研究論文 (最終版).pdf", "研究論文 (最終版).pdf"),
            ("invoice‮fdp.exe", "invoicefdp.exe"),
            (f"{'a' * 40}/../../{'b' * 10}.pdf", f"{'b' * 10}.pdf"),
        ],
        ids=["traversal", "absolute", "windows", "unicode", "rtl-override", "mixed"],
    )
    async def test_no_filename_reaches_the_filesystem(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        tmp_path: Path,
        sent: str,
        stored: str,
    ) -> None:
        user, headers = await _account(committing_session)
        data = make_pdf()
        digest = hashlib.sha256(data).hexdigest()
        before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}

        response = await _upload(api_client, headers, data, filename=sent)

        assert response.status_code == 202
        assert response.json()["filename"] == stored
        assert _files(storage_root) == {f"{user.id}/{digest}.pdf"}
        after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
        created = after - before
        assert all(path.startswith("storage") for path in created), created

    async def test_a_filename_with_nothing_usable_is_refused(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        _, headers = await _account(committing_session)

        response = await _upload(api_client, headers, make_pdf(), filename="../")

        assert response.status_code == 422
        assert _files(storage_root) == set()


# =============================================================================
# Validation over HTTP: refused before anything is stored
# =============================================================================


class TestInvalidUploadsLeaveNothingBehind:
    @pytest.mark.parametrize(
        ("build", "status", "message"),
        [
            (lambda: b"", 422, "empty"),
            (lambda: b"a text file, not a document", 415, "Only PDF"),
            (lambda: b"%PDF-1.7\nnot really", 422, "could not be read"),
            (make_encrypted_pdf, 422, "Password-protected"),
            (lambda: b"GIF89a" + make_pdf(), 415, "Only PDF"),
        ],
        ids=["empty", "not-pdf", "fake-header", "encrypted", "polyglot"],
    )
    async def test_each_refusal_has_its_status_and_stores_nothing(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
        build: Any,
        status: int,
        message: str,
    ) -> None:
        _, headers = await _account(committing_session)

        response = await _upload(
            api_client, headers, build(), filename="secret-name.pdf"
        )

        assert response.status_code == status
        assert message in response.json()["detail"]
        assert "secret-name" not in response.text
        assert await _rows(committing_session) == []
        assert _files(storage_root) == set()
        assert queue.jobs == []

    async def test_over_the_size_limit_is_413(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        limits: Any,
    ) -> None:
        data = make_pdf(pages=5)
        limits(max_bytes=len(data) - 1)
        _, headers = await _account(committing_session)

        response = await _upload(api_client, headers, data)

        assert response.status_code == 413
        assert _files(storage_root) == set()
        assert await _rows(committing_session) == []

    async def test_exactly_the_size_limit_is_accepted(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        limits: Any,
    ) -> None:
        data = make_pdf()
        limits(max_bytes=len(data))
        _, headers = await _account(committing_session)

        assert (await _upload(api_client, headers, data)).status_code == 202

    async def test_over_the_page_limit_is_422(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        limits: Any,
    ) -> None:
        limits(max_pages=2)
        _, headers = await _account(committing_session)

        response = await _upload(api_client, headers, make_pdf(pages=3))

        assert response.status_code == 422
        assert "maximum of 2 pages" in response.json()["detail"]
        assert _files(storage_root) == set()

    async def test_an_owner_restricted_pdf_is_accepted(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        _, headers = await _account(committing_session)

        response = await _upload(api_client, headers, make_owner_restricted_pdf())

        assert response.status_code == 202

    async def test_a_request_with_no_file_is_422(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        _, headers = await _account(committing_session)

        response = await api_client.post(UPLOAD, headers=headers, data={"x": "y"})

        assert response.status_code == 422
        assert _files(storage_root) == set()


# =============================================================================
# Duplicate content (ADR-0010)
# =============================================================================


class TestDuplicateContentIsIdempotentPerOwner:
    async def test_the_same_owner_uploading_the_same_bytes_gets_the_same_document(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        _, headers = await _account(committing_session)
        data = make_pdf()

        first = await _upload(api_client, headers, data, filename="first.pdf")
        second = await _upload(api_client, headers, data, filename="renamed.pdf")

        assert (first.status_code, second.status_code) == (202, 200)
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["filename"] == "first.pdf", "the first name wins"
        assert len(await _rows(committing_session)) == 1
        assert len(_files(storage_root)) == 1
        assert len(queue.jobs) == 1, "a duplicate is not re-ingested"

    async def test_different_owners_get_independent_documents_and_files(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        """No cross-tenant deduplication, and so no cross-tenant signal."""
        alice, alice_headers = await _account(committing_session)
        bob, bob_headers = await _account(committing_session)
        data = make_pdf()
        digest = hashlib.sha256(data).hexdigest()

        a = await _upload(api_client, alice_headers, data)
        b = await _upload(api_client, bob_headers, data)

        assert (a.status_code, b.status_code) == (202, 202)
        assert a.json()["id"] != b.json()["id"]
        assert _files(storage_root) == {
            f"{alice.id}/{digest}.pdf",
            f"{bob.id}/{digest}.pdf",
        }
        assert sorted(job.owner_id for job in queue.jobs) == sorted([alice.id, bob.id])

    async def test_after_deleting_a_document_the_same_file_uploads_again(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        """A soft-deleted document does not count; the blob on disk is reused."""
        _, headers = await _account(committing_session)
        data = make_pdf()
        first = (await _upload(api_client, headers, data)).json()["id"]
        assert (
            await api_client.delete(f"/api/v1/documents/{first}", headers=headers)
        ).status_code == 204

        again = await _upload(api_client, headers, data)

        assert again.status_code == 202
        assert again.json()["id"] != first
        assert len(await _rows(committing_session)) == 2
        assert len(_files(storage_root)) == 1
        assert len(queue.jobs) == 2

    async def test_concurrent_identical_uploads_create_exactly_one_document(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        """The race the partial unique index exists for, run for real.

        Several requests can all miss the service's lookup; only one insert can
        succeed. Every other request must answer with that document, not a 500
        and not a second row.
        """
        _, headers = await _account(committing_session)
        data = make_pdf()

        responses = await asyncio.gather(
            *[_upload(api_client, headers, data) for _ in range(6)]
        )

        statuses = sorted(r.status_code for r in responses)
        assert statuses.count(202) == 1, statuses
        assert set(statuses) <= {200, 202}, statuses
        assert len({r.json()["id"] for r in responses}) == 1
        assert len(await _rows(committing_session)) == 1
        assert len(_files(storage_root)) == 1
        assert len(queue.jobs) == 1

    async def test_losing_the_insert_race_keeps_the_winners_file(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forced deterministically: the lookup misses, the insert collides.

        The file on disk belongs to the winning row, so the losing request must
        leave it — whoever wrote it.
        """
        user, headers = await _account(committing_session)
        data = make_pdf()
        winner = (await _upload(api_client, headers, data)).json()["id"]

        async def always_miss(self: Any, content_hash: str, user_id: str) -> None:
            return None

        original = DocumentRepository.find_live_by_content_hash_for_user
        calls: list[int] = []

        async def miss_once(self: Any, content_hash: str, user_id: str) -> Any:
            calls.append(1)
            if len(calls) == 1:
                return await always_miss(self, content_hash, user_id)
            return await original(self, content_hash, user_id)

        monkeypatch.setattr(
            DocumentRepository, "find_live_by_content_hash_for_user", miss_once
        )

        response = await _upload(api_client, headers, data)

        assert response.status_code == 200
        assert response.json()["id"] == winner
        assert len(await _rows(committing_session)) == 1
        assert (
            storage_root / user.id / f"{hashlib.sha256(data).hexdigest()}.pdf"
        ).exists()


# =============================================================================
# Failures: nothing half-done is left behind
# =============================================================================


class TestFailuresAreCompensated:
    async def test_a_storage_failure_is_503_and_records_nothing(
        self,
        api_client: Any,
        committing_session: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        queue: RecordingQueue,
    ) -> None:
        """A real OS failure, not a mock: the storage root sits under a file."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("occupied")
        monkeypatch.setenv("STORAGE_ROOT", str(blocker / "storage"))
        get_settings.cache_clear()
        _, headers = await _account(committing_session)

        response = await _upload(api_client, headers, make_pdf())

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Document storage is unavailable. Nothing was saved."
        }
        assert str(tmp_path) not in response.text
        assert await _rows(committing_session) == []
        assert queue.jobs == []

    async def test_a_database_failure_after_storing_removes_the_file(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        queue: RecordingQueue,
    ) -> None:
        """The compensation: this upload wrote the file, so this upload removes it."""
        _, headers = await _account(committing_session)

        async def failing_create(self: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection lost during insert")

        monkeypatch.setattr(DocumentRepository, "create", failing_create)

        response = await _upload(api_client, headers, make_pdf())

        assert response.status_code == 500
        assert response.json() == {
            "detail": "The document could not be saved. Nothing was kept."
        }
        assert "connection lost" not in response.text
        assert await _rows(committing_session) == []
        assert _files(storage_root) == set(), "the stored file was not cleaned up"
        assert queue.jobs == []

    async def test_a_database_failure_never_removes_a_file_it_did_not_create(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The file already belonged to this owner's soft-deleted document.

        Deleting it would break that document's deletion finalisation later —
        compensation is only for what *this* request wrote (ADR-0010).
        """
        user, headers = await _account(committing_session)
        data = make_pdf()
        digest = hashlib.sha256(data).hexdigest()
        first = (await _upload(api_client, headers, data)).json()["id"]
        await api_client.delete(f"/api/v1/documents/{first}", headers=headers)

        async def failing_create(self: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection lost during insert")

        monkeypatch.setattr(DocumentRepository, "create", failing_create)

        response = await _upload(api_client, headers, data)

        assert response.status_code == 500
        assert (storage_root / user.id / f"{digest}.pdf").exists()

    async def test_the_session_is_clean_after_a_failed_upload(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The next request on the application works; nothing is left pending."""
        _, headers = await _account(committing_session)

        async def failing_create(self: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(DocumentRepository, "create", failing_create)
        assert (await _upload(api_client, headers, make_pdf())).status_code == 500
        monkeypatch.undo()
        get_settings.cache_clear()

        assert (await _upload(api_client, headers, make_pdf())).status_code == 202


# =============================================================================
# The ingestion boundary
# =============================================================================


class TestTheIngestionHandOff:
    async def test_the_job_carries_everything_the_worker_needs_and_the_true_owner(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        user, headers = await _account(committing_session)
        data = make_pdf()
        digest = hashlib.sha256(data).hexdigest()

        response = await _upload(
            api_client, headers, data, filename="Private Title.pdf"
        )

        assert len(queue.jobs) == 1
        job = queue.jobs[0]
        assert job.document_id == response.json()["id"]
        assert job.owner_id == user.id
        assert job.storage_key == f"{user.id}/{digest}.pdf"
        assert job.content_hash == digest
        assert job.mime_type == "application/pdf"
        assert "Private Title" not in job.model_dump_json()

    async def test_the_job_is_enqueued_only_after_the_commit(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        _, headers = await _account(committing_session)

        await _upload(api_client, headers, make_pdf())

        assert queue.committed_at_enqueue == [True]

    async def test_the_worker_can_read_back_the_bytes_the_job_names(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        """Through the Storage protocol, verifying the hash — what S3.2 will do."""
        _, headers = await _account(committing_session)
        data = make_pdf()
        await _upload(api_client, headers, data)
        job = queue.jobs[0]

        stored = await LocalStorage(storage_root).get(job.storage_key)

        assert hashlib.sha256(stored).hexdigest() == job.content_hash
        assert stored == data

    def test_the_job_payload_is_closed_and_immutable(self) -> None:
        job = IngestionJob(
            document_id=str(uuid.uuid4()),
            owner_id=str(uuid.uuid4()),
            storage_key="k",
            content_hash="h",
            mime_type="application/pdf",
        )

        # Specific errors, not `Exception`: a typo in the test would raise too.
        with pytest.raises(ValidationError):
            job.owner_id = str(uuid.uuid4())
        with pytest.raises(ValidationError):
            IngestionJob(**{**job.model_dump(), "authorization": "Bearer x"})
        assert set(IngestionJob.model_fields) == {
            "document_id",
            "owner_id",
            "storage_key",
            "content_hash",
            "mime_type",
        }

    async def test_the_default_queue_defers_honestly(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        """With no override: the real provider, a logged deferral, status pending."""
        assert isinstance(get_ingestion_queue(), DeferredIngestionQueue)
        _, headers = await _account(committing_session)

        with capture_logs() as logs:
            response = await _upload(api_client, headers, make_pdf())

        assert response.json()["status"] == "pending"
        assert [e for e in logs if e["event"] == "ingestion.job.deferred"]


# =============================================================================
# Ownership of what was uploaded
# =============================================================================


class TestUploadedDocumentsStayTheirOwners:
    async def test_another_user_cannot_read_list_or_delete_it(
        self, api_client: Any, committing_session: Any, storage_root: Path
    ) -> None:
        _, owner = await _account(committing_session)
        _, stranger = await _account(committing_session)
        document_id = (await _upload(api_client, owner, make_pdf())).json()["id"]

        read = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=stranger
        )
        deleted = await api_client.delete(
            f"/api/v1/documents/{document_id}", headers=stranger
        )
        listed = await api_client.get("/api/v1/documents/", headers=stranger)

        assert (read.status_code, deleted.status_code) == (404, 404)
        assert listed.json()["total"] == 0
        still = await api_client.get(f"/api/v1/documents/{document_id}", headers=owner)
        assert still.status_code == 200

    async def test_the_content_lookup_is_scoped_to_the_owner_in_sql(
        self, committing_session: Any
    ) -> None:
        """TestOwnershipIsInTheQuery's rule, applied to the method S3.1 added."""
        from sqlalchemy import event

        from backend.db.engine import get_engine

        user, _ = await _account(committing_session)
        statements: list[str] = []

        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            await DocumentRepository(
                committing_session
            ).find_live_by_content_hash_for_user("0" * 64, user.id)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        selects = [s for s in statements if "FROM documents" in s]
        assert selects
        assert all("documents.user_id = " in s for s in selects), selects
        assert all("documents.deleted_at IS NULL" in s for s in selects), selects


# =============================================================================
# Logging
# =============================================================================


class TestLogsCarryIdentifiersNotContent:
    async def test_no_log_event_contains_the_filename_bytes_or_token(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        user, headers = await _account(committing_session)
        data = make_pdf(text="CONFIDENTIAL-MANUSCRIPT-TEXT")
        token = headers["Authorization"].split()[1]

        with capture_logs() as logs:
            response = await _upload(
                api_client, headers, data, filename="Secret Grant.pdf"
            )
            await _upload(
                api_client, headers, b"not a pdf", filename="Other Secret.pdf"
            )

        stored = [e for e in logs if e["event"] == "document.upload.stored"]
        assert len(stored) == 1
        assert stored[0]["document_id"] == response.json()["id"]
        assert stored[0]["owner_id"] == user.id
        assert stored[0]["content_hash"] == hashlib.sha256(data).hexdigest()
        assert [
            e["reason"] for e in logs if e["event"] == "document.upload.rejected"
        ] == ["unsupported_type"]

        everything = repr(logs)
        for secret in ("Secret Grant", "Other Secret", "CONFIDENTIAL", token, "%PDF"):
            assert secret not in everything, secret


# =============================================================================
# The headline journey
# =============================================================================


class TestTheWholeUploadJourney:
    async def test_register_login_upload_store_record_read_and_be_refused(
        self,
        api_client: Any,
        committing_session: Any,
        storage_root: Path,
        queue: RecordingQueue,
    ) -> None:
        """Authentication, authorisation, Principal, storage, persistence, ownership.

        One user, through real registration and login, uploads a real PDF; the
        row, the file, the hash and the job all name them. A second user, also
        registered and logged in for real, can see none of it.
        """
        password = "correct-horse-battery"
        registered = await api_client.post(
            "/api/v1/auth/register",
            json={
                "email": "author@example.com",
                "password": password,
                "full_name": "Author",
            },
        )
        assert registered.status_code == 201
        owner_id = registered.json()["id"]
        token = (
            await api_client.post(
                "/api/v1/auth/login",
                json={"email": "author@example.com", "password": password},
            )
        ).json()["access_token"]
        owner = {"Authorization": f"Bearer {token}"}

        data = make_pdf(pages=4, text="A real research paper")
        digest = hashlib.sha256(data).hexdigest()
        uploaded = await _upload(api_client, owner, data, filename="Research Paper.pdf")
        assert uploaded.status_code == 202
        document_id = uploaded.json()["id"]

        rows = await _rows(committing_session)
        assert len(rows) == 1
        assert str(rows[0]["id"]) == document_id
        assert str(rows[0]["user_id"]) == owner_id
        assert rows[0]["content_hash"] == digest
        assert rows[0]["page_count"] == 4

        stored = storage_root / owner_id / f"{digest}.pdf"
        assert stored.is_file()
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == digest
        assert [job.owner_id for job in queue.jobs] == [owner_id]

        fetched = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=owner
        )
        assert fetched.status_code == 200
        assert fetched.json()["filename"] == "Research Paper.pdf"

        await api_client.post(
            "/api/v1/auth/register",
            json={
                "email": "other@example.com",
                "password": password,
                "full_name": "Other",
            },
        )
        other_token = (
            await api_client.post(
                "/api/v1/auth/login",
                json={"email": "other@example.com", "password": password},
            )
        ).json()["access_token"]
        other = {"Authorization": f"Bearer {other_token}"}

        refused = await api_client.get(
            f"/api/v1/documents/{document_id}", headers=other
        )
        assert refused.status_code == 404
        assert refused.json() == {"detail": "Document not found"}
        assert (await api_client.get("/api/v1/documents/", headers=other)).json()[
            "total"
        ] == 0
