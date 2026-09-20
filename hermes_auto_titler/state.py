"""Durable lifecycle state for hermes-auto-titler.

The store deliberately contains scheduling metadata only.  It never writes a
candidate into SessionDB; the normal review and provenance gates remain the
only path to a user-visible title.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


STATE_VERSION = 1


class StateStore:
    """Versioned JSON state with same-directory atomic replacement."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": STATE_VERSION, "sessions": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise ValueError("unsupported auto-titler state format")
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            raise ValueError("auto-titler state sessions must be an object")
        return {"version": STATE_VERSION, "sessions": sessions}

    def save(self, sessions: Mapping[str, Mapping[str, Any]]) -> None:
        """fsync a temporary file, atomically replace, then fsync its directory."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": STATE_VERSION, "sessions": sessions},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        tmp_path: Path | None = None
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            tmp_path = Path(raw_path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
            self._fsync_parent()
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass

    def _fsync_parent(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(self.path.parent, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
