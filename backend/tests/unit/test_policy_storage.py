import uuid
from pathlib import Path

import pytest

from app.core.policies.storage import PolicyStorageError, PrivatePolicyStorage


def test_storage_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    storage = PrivatePolicyStorage(tmp_path)
    tenant_id = uuid.uuid4()

    first = storage.store(tenant_id=tenant_id, content=b"policy", suffix=".txt", max_bytes=100)
    second = storage.store(tenant_id=tenant_id, content=b"policy", suffix=".txt", max_bytes=100)

    assert first == second
    assert not Path(first.storage_key).is_absolute()
    assert storage.read(first.storage_key) == b"policy"


def test_storage_rejects_traversal(tmp_path: Path) -> None:
    storage = PrivatePolicyStorage(tmp_path)
    with pytest.raises(PolicyStorageError) as caught:
        storage.read("../outside.txt")
    assert caught.value.code == "POLICY_STORAGE_KEY_INVALID"
