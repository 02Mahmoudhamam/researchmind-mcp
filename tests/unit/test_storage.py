"""Local content-addressed storage (ADR-0008), against a real temporary directory.

No mocked filesystem. What is under test is how this code behaves on an actual
disk — atomic creation, containment, what happens when the OS says no — and a
fake filesystem would only test the fake.

Every test writes beneath pytest's `tmp_path`. Nothing touches the project's
`uploads/` directory.
"""

import asyncio
import hashlib
import inspect
import os
from pathlib import Path

import pytest

from backend.storage import LocalStorage
from shared.interfaces.storage import (
    STORAGE_KEY_PATTERN,
    Storage,
    StorageError,
    StorageObjectMissing,
    document_storage_key,
)

OWNER = "0b8f7c3e-6d3a-4f1e-9c2b-5a7d8e9f0a1b"
DATA = b"%PDF-1.7 stored bytes"
HASH = hashlib.sha256(DATA).hexdigest()
KEY = f"{OWNER}/{HASH}.pdf"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def storage(root: Path) -> LocalStorage:
    return LocalStorage(root)


def _everything_under(path: Path) -> set[str]:
    return (
        {str(p.relative_to(path)) for p in path.rglob("*")} if path.exists() else set()
    )


class TestTheKeyIsTheContract:
    def test_the_key_is_owner_slash_hash_dot_pdf(self) -> None:
        """ADR-0008 §2, exactly."""
        assert document_storage_key(OWNER, HASH) == KEY
        assert STORAGE_KEY_PATTERN.fullmatch(KEY)

    def test_the_owner_is_canonicalised(self) -> None:
        """One user, one directory — not one per spelling of their UUID."""
        assert document_storage_key(OWNER.upper(), HASH) == KEY

    def test_the_key_has_no_way_to_carry_a_filename(self) -> None:
        """ADR-0008 §3 as a signature: there is no parameter a filename could use."""
        assert list(inspect.signature(document_storage_key).parameters) == [
            "owner_id",
            "content_hash",
        ]

    @pytest.mark.parametrize(
        "owner",
        ["not-a-uuid", "", "../..", f"{OWNER}/..", "/etc"],
        ids=["garbage", "empty", "dots", "suffix", "absolute"],
    )
    def test_an_owner_that_is_not_a_uuid_is_refused(self, owner: str) -> None:
        with pytest.raises(ValueError):
            document_storage_key(owner, HASH)

    @pytest.mark.parametrize(
        "content_hash",
        [HASH.upper(), HASH[:-1], HASH + "0", "../" + HASH[3:], "g" * 64, ""],
        ids=["uppercase", "short", "long", "traversal", "non-hex", "empty"],
    )
    def test_a_hash_that_is_not_sha256_hex_is_refused(self, content_hash: str) -> None:
        with pytest.raises(ValueError):
            document_storage_key(OWNER, content_hash)

    def test_local_storage_satisfies_the_protocol(self, storage: LocalStorage) -> None:
        """Structural: the service depends on `Storage`, never on `LocalStorage`."""
        conforming: Storage = storage
        assert conforming is storage


class TestPut:
    async def test_it_writes_the_exact_bytes_at_the_adr_path(
        self, storage: LocalStorage, root: Path
    ) -> None:
        await storage.put(KEY, DATA)

        stored = root / OWNER / f"{HASH}.pdf"
        assert stored.read_bytes() == DATA
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == HASH

    async def test_it_reports_whether_this_call_created_the_object(
        self, storage: LocalStorage
    ) -> None:
        """The flag that lets an upload undo only its *own* write (ADR-0010)."""
        assert await storage.put(KEY, DATA) is True
        assert await storage.put(KEY, DATA) is False

    async def test_a_second_put_leaves_the_first_object_untouched(
        self, storage: LocalStorage, root: Path
    ) -> None:
        await storage.put(KEY, DATA)
        stored = root / OWNER / f"{HASH}.pdf"
        before = stored.stat().st_mtime_ns

        await storage.put(KEY, DATA)

        assert stored.stat().st_mtime_ns == before

    async def test_no_temporary_file_is_left_behind(
        self, storage: LocalStorage, root: Path
    ) -> None:
        await storage.put(KEY, DATA)
        await storage.put(KEY, DATA)

        assert _everything_under(root) == {OWNER, f"{OWNER}/{HASH}.pdf"}

    async def test_concurrent_puts_have_exactly_one_creator(
        self, storage: LocalStorage
    ) -> None:
        """Atomic create-if-absent, measured under a real race.

        `os.replace` would also produce a correct file, but every racer would
        believe it created it — and each might then delete it on a later
        failure. `os.link` refuses an existing target, so only one can.
        """
        results = await asyncio.gather(*[storage.put(KEY, DATA) for _ in range(25)])

        assert sum(results) == 1

    async def test_constructing_storage_touches_no_disk(self, root: Path) -> None:
        """Built per request by the API provider, so construction must be free."""
        LocalStorage(root)

        assert not root.exists()


class TestGetAndDelete:
    async def test_get_returns_what_put_stored(self, storage: LocalStorage) -> None:
        await storage.put(KEY, DATA)

        assert await storage.get(KEY) == DATA

    async def test_get_of_nothing_is_missing_not_a_generic_error(
        self, storage: LocalStorage
    ) -> None:
        with pytest.raises(StorageObjectMissing):
            await storage.get(KEY)

    async def test_delete_removes_the_object(
        self, storage: LocalStorage, root: Path
    ) -> None:
        await storage.put(KEY, DATA)

        await storage.delete(KEY)

        assert not (root / OWNER / f"{HASH}.pdf").exists()

    async def test_deleting_something_absent_is_not_an_error(
        self, storage: LocalStorage
    ) -> None:
        await storage.delete(KEY)
        await storage.delete(KEY)

    async def test_after_delete_the_next_put_creates_again(
        self, storage: LocalStorage
    ) -> None:
        await storage.put(KEY, DATA)
        await storage.delete(KEY)

        assert await storage.put(KEY, DATA) is True


class TestNothingEscapesTheRoot:
    """Traversal is unexpressible by key construction; these prove the backend agrees."""

    HOSTILE = [
        "../escape.pdf",
        f"../{OWNER}/{HASH}.pdf",
        f"{OWNER}/../../escape.pdf",
        f"/tmp/{OWNER}/{HASH}.pdf",
        f"/{OWNER}/{HASH}.pdf",
        f"{OWNER}\\{HASH}.pdf",
        f"{OWNER}/{HASH}.pdf\x00.txt",
        f"{OWNER.upper()}/{HASH}.pdf",
        f"{OWNER}/{HASH}.PDF",
        f"{OWNER}/{HASH}.exe",
        f"{OWNER}/sub/{HASH}.pdf",
        "My Paper.pdf",
        "",
    ]

    @pytest.mark.parametrize("key", HOSTILE, ids=range(len(HOSTILE)))
    @pytest.mark.parametrize("operation", ["put", "get", "delete"])
    async def test_a_key_not_built_by_the_contract_is_refused(
        self, storage: LocalStorage, tmp_path: Path, key: str, operation: str
    ) -> None:
        before = _everything_under(tmp_path)

        with pytest.raises(StorageError):
            if operation == "put":
                await storage.put(key, DATA)
            elif operation == "get":
                await storage.get(key)
            else:
                await storage.delete(key)

        assert _everything_under(tmp_path) == before, "something was written"

    async def test_a_symlinked_owner_directory_cannot_lead_outside(
        self, storage: LocalStorage, root: Path, tmp_path: Path
    ) -> None:
        """The second lock: the resolved path must still be inside the root.

        The key is well-formed, so the pattern check passes. The owner directory
        has been replaced with a link to somewhere else — which nothing in the
        application does, and which this refuses anyway.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        root.mkdir()
        (root / OWNER).symlink_to(outside, target_is_directory=True)

        with pytest.raises(StorageError):
            await storage.put(KEY, DATA)

        assert list(outside.iterdir()) == []


class TestStorageFailuresAreStorageErrors:
    async def test_a_root_that_is_a_file_fails_cleanly(self, tmp_path: Path) -> None:
        not_a_directory = tmp_path / "file"
        not_a_directory.write_text("occupied")
        storage = LocalStorage(not_a_directory / "store")

        with pytest.raises(StorageError) as raised:
            await storage.put(KEY, DATA)

        assert OWNER not in str(raised.value)
        assert HASH not in str(raised.value)
        assert str(tmp_path) not in str(raised.value)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root ignores directory permissions",
    )
    async def test_a_read_only_root_fails_cleanly(
        self, storage: LocalStorage, root: Path
    ) -> None:
        root.mkdir()
        root.chmod(0o500)
        try:
            with pytest.raises(StorageError):
                await storage.put(KEY, DATA)
        finally:
            root.chmod(0o700)

    async def test_the_session_storage_root_is_not_the_project_directory(self) -> None:
        """The net under every other test: conftest points STORAGE_ROOT elsewhere."""
        from backend.config.settings import get_settings

        project = Path(__file__).resolve().parents[2]
        configured = Path(get_settings().STORAGE_ROOT).resolve()

        assert not configured.is_relative_to(project)
