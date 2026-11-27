"""AGENTS.md 加载器替身：记录被读次数，用于验证「每轮只读一次」。"""

from pathlib import Path

from app.common.api.agents_md_loader import AgentsDocument


class FakeAgentsLoader:
    def __init__(self, content: str = "", version: str = "v1") -> None:
        self.content = content
        self.version = version
        self.calls = 0

    def load(self, workspace: Path) -> AgentsDocument:
        self.calls += 1
        return AgentsDocument(version=self.version, content=self.content, source=None)
