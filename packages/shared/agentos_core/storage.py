"""Controlled file storage for artifacts and uploaded documents.

All paths are relative to a single root and are resolved and verified to stay
inside it, so neither tools nor API callers can reach the host filesystem.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from agentos_core.config import get_settings
from agentos_core.domain.errors import PolicyViolationError

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def validate_filename(name: str) -> str:
    if not SAFE_FILENAME.fullmatch(name) or ".." in name:
        raise PolicyViolationError(f"invalid filename {name!r}: use letters, digits, '.', '_' or '-'")
    return name


class Storage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or get_settings().artifact_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        if os.path.isabs(relative) or "\x00" in relative:
            raise PolicyViolationError("absolute paths are not allowed")
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise PolicyViolationError("path escapes the storage root")
        return target

    def write_atomic(self, relative: str, data: bytes) -> Path:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def read_bytes(self, relative: str, *, max_bytes: int | None = None) -> bytes:
        target = self.resolve(relative)
        if max_bytes is not None and target.stat().st_size > max_bytes:
            raise PolicyViolationError(f"file exceeds size limit of {max_bytes} bytes")
        return target.read_bytes()
