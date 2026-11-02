# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py 的 Path 操作，及同目录 support/assessment.py:linked（22–25 行）。
# 功能：将相对路径绑定到显式工作区，拒绝越界和符号链接/Windows 重解析点，不使用 os.chdir。
# 后续：处理并发路径替换等 TOCTOU 风险；任意子进程仍能越界，本实现不是操作系统沙箱。
import stat
from pathlib import Path


def linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class Workspace:
    def __init__(self, root):
        source = Path(root).absolute()
        if linked(source):
            raise ValueError("Workspace must not be a symbolic link or reparse point")
        self.root = source.resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(str(self.root))

    def resolve(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError("Path must be a non-empty string")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Path is outside the workspace") from error
        current = self.root
        for part in relative.parts:
            if part == "..":
                raise ValueError("Parent path traversal is not allowed")
            current /= part
            if current.exists() or current.is_symlink():
                if linked(current):
                    raise ValueError("Links and reparse points are not allowed in workspace paths")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path is outside the workspace")
        return resolved