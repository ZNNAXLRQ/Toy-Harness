"""AGENTS.md / Skills 加载端口。

只在**会话开始时**读一次，把内容和版本号写进会话（``sessions.agents_md_version``）
与稳定前缀（``sessions.prefix_hash``）。中途重新读文件会改变前缀、击穿 prefix
cache，因此运行期不重新加载；文件变了属于「新会话」或显式的「重建前缀」操作。

同步接口是刻意的：这是启动/建会话时的一次文件读取，不属于请求热路径。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True)
class AgentsDocument:
    """一次加载的结果。``version`` 由内容哈希得出，用于判断前缀是否需要重建。"""

    version: str
    content: str
    source: Path | None = None
    skills: Sequence[str] = field(default_factory=tuple)


class AgentsMdLoader(Protocol):
    def load(self, workspace: Path) -> AgentsDocument:
        """读取工作区根目录的 AGENTS.md；不存在时返回空文档而不是抛异常。"""
        ...
