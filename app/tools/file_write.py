import asyncio
import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, Mapping

from app.common.api.tool import ToolResult, ToolSpec
from app.common.security import redact
from app.tools.workspace import Workspace


@dataclass(frozen=True)
class _FileVersion:
    data: bytes
    mode: int
    modified_ns: int

    def fingerprint(self) -> dict[str, str | int]:
        return {
            "hash": hashlib.sha256(self.data).hexdigest(),
            "mode": self.mode,
            "modified_ns": self.modified_ns,
        }


def _snapshot(path: Path) -> _FileVersion | None:
    try:
        if not path.is_file():
            if path.exists():
                raise ValueError("Target must be a regular file")
            return None
        with path.open("rb") as file:
            data = file.read()
            info = os.fstat(file.fileno())
        return _FileVersion(data, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    except FileNotFoundError:
        return None


def _replace_file(path: Path, data: bytes, previous: _FileVersion | None, *, restore=False) -> _FileVersion:
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.close()
            if previous is not None:
                staged.chmod(previous.mode & 0o777)
                if restore:
                    os.utime(staged, ns=(previous.modified_ns, previous.modified_ns))
            info = staged.stat()
            os.replace(staged, path)
            return _FileVersion(data, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
        finally:
            temporary.close()
            if os.name == "nt" and staged.exists():
                staged.chmod(stat.S_IWRITE)
            staged.unlink(missing_ok=True)


class FileWrite:
    def __init__(self, workspace: Workspace, spec: ToolSpec):
        self.workspace = workspace
        self.spec = spec
        self._lock = Lock()

    async def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        return await asyncio.to_thread(self._write, **arguments)

    def _write(self, file_path: str, content: str) -> ToolResult:
        try:
            data = content.encode("utf-8")
            with self._lock:
                path = self.workspace.resolve(file_path)
                before = _snapshot(path)
                if before is not None and before.mode > 0o777:
                    raise ValueError("Cannot replace a file with special permission bits")
                relative = path.relative_to(self.workspace.root).as_posix()
                path.parent.mkdir(parents=True, exist_ok=True)
                written = _replace_file(path, data, before)
                saved = None if before is None else {
                    "content": base64.b64encode(before.data).decode("ascii"),
                    "mode": before.mode,
                    "modified_ns": before.modified_ns,
                }
                receipt = json.dumps({
                    "path": relative,
                    "before": saved,
                    "written": written.fingerprint(),
                })
            return ToolResult("success", {"file_path": relative, "characters_written": len(content)}, receipt)
        except (OSError, UnicodeError, ValueError) as error:
            return ToolResult("error", redact(f"Error writing file: {error}"))

    @staticmethod
    def rollback_target(receipt: str) -> str:
        return os.path.normcase(json.loads(receipt)["path"])

    async def rollback(self, receipt: str, *, retry: bool = False) -> ToolResult:
        return await asyncio.to_thread(self._restore, receipt, retry)

    def _restore(self, receipt: str, retry: bool) -> ToolResult:
        try:
            saved = json.loads(receipt)
            original = saved["before"]
            before = None if original is None else _FileVersion(
                base64.b64decode(original["content"], validate=True),
                original["mode"],
                original["modified_ns"],
            )
            if before is not None and not 0 <= before.mode <= 0o777:
                raise ValueError("Invalid saved file mode")
            with self._lock:
                path = self.workspace.resolve(saved["path"])
                current = _snapshot(path)
                if retry and current == before:
                    return ToolResult("success")
                if current is None or current.fingerprint() != saved["written"]:
                    raise ValueError("File changed after the tool wrote it")
                if before is None:
                    path.unlink()
                else:
                    _replace_file(path, before.data, before, restore=True)
            return ToolResult("success")
        except (KeyError, TypeError, ValueError, OSError) as error:
            return ToolResult("error", redact(f"Error rolling back file: {error}"))
