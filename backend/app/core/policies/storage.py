"""租户隔离、内容寻址的私有制度源文件存储。"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ExpenseGuardError


class PolicyStorageError(ExpenseGuardError):
    """私有制度文件存储失败。"""

    status_code = 500


class StoredPolicyBlob(BaseModel):
    """可安全持久化到 PG 的相对存储元数据。"""

    model_config = ConfigDict(frozen=True)

    storage_key: str = Field(min_length=1, max_length=1024)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class PrivatePolicyStorage:
    """只允许在显式私有根目录下读写内容寻址 blob。"""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    def store(
        self,
        *,
        tenant_id: uuid.UUID,
        content: bytes,
        suffix: str,
        max_bytes: int,
    ) -> StoredPolicyBlob:
        if not content:
            raise PolicyStorageError(code="POLICY_TEXT_UNAVAILABLE", message="制度文件为空")
        if len(content) > max_bytes:
            raise PolicyStorageError(code="POLICY_FILE_TOO_LARGE", message="制度文件超过大小上限")

        normalized_suffix = suffix.lower()
        if normalized_suffix not in {".pdf", ".docx", ".txt"}:
            raise PolicyStorageError(
                code="POLICY_FILE_UNSUPPORTED", message="仅支持 PDF、DOCX 或 UTF-8 TXT"
            )
        digest = hashlib.sha256(content).hexdigest()
        storage_key = f"{tenant_id}/{digest[:2]}/{digest}{normalized_suffix}"
        target = self._resolve_key(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise PolicyStorageError(
                    code="POLICY_STORAGE_HASH_MISMATCH", message="私有存储内容校验失败"
                )
        else:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(content)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return StoredPolicyBlob(
            storage_key=storage_key,
            content_sha256=digest,
            size_bytes=len(content),
        )

    def read(self, storage_key: str) -> bytes:
        target = self._resolve_key(storage_key)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise PolicyStorageError(
                code="POLICY_STORAGE_UNAVAILABLE", message="制度源文件不可读取"
            ) from exc

    def _resolve_key(self, storage_key: str) -> Path:
        if not storage_key or Path(storage_key).is_absolute():
            raise PolicyStorageError(code="POLICY_STORAGE_KEY_INVALID", message="制度存储键无效")
        target = (self._root / Path(storage_key)).resolve()
        if not target.is_relative_to(self._root):
            raise PolicyStorageError(code="POLICY_STORAGE_KEY_INVALID", message="制度存储键无效")
        return target
