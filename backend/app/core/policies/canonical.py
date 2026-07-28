"""F4 制度与索引配置的 canonical SHA-256 指纹。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence


def canonical_sha256(value: object) -> str:
    """对 JSON 可表达值计算稳定指纹。"""
    canonical = _canonicalize(value)
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonicalize(item) for item in value]
    return value
