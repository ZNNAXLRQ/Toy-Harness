"""契约测试的共享 fixture —— 实现方的「接入点」。

C 交付 SQLite 实现时**只需要改这个文件里的一处**：把 ``_sqlite_store`` 从 skip 换成
构造你的实现，其余全部用例一行都不用改。用例红转绿 = 验收通过。

为什么现在是 skip 而不是 xfail：
- skip 会让 CI 常绿，是为了不让「实现未交付」卡住其他人的 PR；
- 但也意味着**交付那天必须手动改这里**，否则 CI 永远是绿的假象。

交付检查清单：
1. 把 ``_sqlite_store`` 改成 ``return SqliteStateStore(db_path)``；
2. 跑 ``uv run pytest tests/contracts -q``，47 条（memory + sqlite 各一遍）全绿；
3. 若有用例红：先看是契约读不懂，还是实现少了事务/幂等语义——**不要改测试来迁就实现**，
   改契约要先通知 A 并同步更新这份用例。
"""

import pytest

from tests.fakes.memory_state_store import MemoryStateStore


def _sqlite_store():
    """TODO(C)：交付后替换成真实实现构造。"""
    pytest.skip(
        "SQLite StateStore 尚未交付（C）；交付后在 tests/contracts/conftest.py "
        "的 _sqlite_store() 里构造实现，本套用例即可复用"
    )


@pytest.fixture(params=["memory", "sqlite"])
async def store(request):
    if request.param == "memory":
        return MemoryStateStore()
    return _sqlite_store()
