# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:read_file/write_file/list_files/_run_process/execute_command/run_python（50–127 行）。
# 功能：保留 UTF-8 文件读写、递归空目录、argv 执行、真实输出及超时；新增显式工作区和统一结果。
# 后续：大文件/输出限额、写前备份和补偿、进程树终止及沙箱；审批由 Registry 统一执行。
import os
import subprocess
import sys

from app.common.model.runtime import ToolResult
from app.tools.workspace import Workspace


class BuiltinTools:
    def __init__(self, workspace: Workspace, timeout=15.0):
        if not 0 < timeout < float("inf"):
            raise ValueError("Process timeout must be positive and finite")
        self.workspace, self.timeout = workspace, timeout

    def read_file(self, file_path: str):
        """Read a UTF-8 text file inside the workspace."""
        try:
            return ToolResult("success", self.workspace.resolve(file_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            return ToolResult("error", f"Error reading file: {error}")

    def write_file(self, file_path: str, content: str):
        """Create or replace a UTF-8 text file, creating parent directories if needed."""
        if not isinstance(content, str):
            return ToolResult("error", "Error writing file: content must be a string")
        try:
            path = self.workspace.resolve(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            return ToolResult("success", {"file_path": path.relative_to(self.workspace.root).as_posix(),
                                          "characters_written": path.write_text(content, encoding="utf-8")})
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            return ToolResult("error", f"Error writing file: {error}")

    def list_files(self, directory: str):
        """Recursively list relative paths, including empty directories; reject links."""
        try:
            root = self.workspace.resolve(directory)
            files, directories, pending = [], [], [root]
            while pending:
                for entry in pending.pop().iterdir():
                    path = self.workspace.resolve(str(entry))
                    relative = path.relative_to(root).as_posix()
                    if path.is_dir():
                        directories.append(relative)
                        pending.append(path)
                    elif path.is_file():
                        files.append(relative)
            return ToolResult("success", {"files": sorted(files), "directories": sorted(directories)})
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            return ToolResult("error", f"Error listing directory: {error}")

    def _run_process(self, command: list[str]):
        try:
            environment = {name: value for name, value in os.environ.items()
                           if name.upper() not in {"BASE_URL", "PYTHONPATH", "PYTHONHOME"}
                           and not any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
            environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(
                command, cwd=self.workspace.root, shell=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout,
                stdin=subprocess.DEVNULL, env=environment,
            )
            return ToolResult("success" if result.returncode == 0 else "error",
                              {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.stdout or "", error.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return ToolResult("error", {"returncode": None, "stdout": stdout, "stderr": stderr,
                                        "error": f"Process timed out after {error.timeout} seconds."})
        except (OSError, ValueError) as error:
            return ToolResult("error", {"returncode": None, "stdout": "", "stderr": str(error)})

    def execute_command(self, command: list[str]):
        """Execute an argv list; the registry must approve this sensitive operation first."""
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            return ToolResult("error", {"returncode": None, "stdout": "",
                                        "stderr": "command must be a non-empty list of strings"})
        return self._run_process(command)

    def run_python(self, file_path: str, args: list[str] | None = None):
        """Run a workspace Python file using the current interpreter and optional argv."""
        if args is not None and (not isinstance(args, list) or not all(isinstance(arg, str) for arg in args)):
            return ToolResult("error", {"returncode": None, "stdout": "", "stderr": "args must be a list of strings or null"})
        try:
            path = self.workspace.resolve(file_path)
            return self._run_process([sys.executable, str(path), *(args or [])])
        except (OSError, TypeError, ValueError) as error:
            return ToolResult("error", {"returncode": None, "stdout": "", "stderr": str(error)})