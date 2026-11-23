-- Toy Harness 数据库结构
--
-- 说明：
-- 1. 前 7 张表沿用 sample.md 第四章（与 Java 版一致）的结构，未改字段。
-- 2. **新增 approvals 表**（A 的偏离决策，2026-11）：
--    审批要跨重启恢复，就必须有地方存「谁在等审批、谁批的、什么时候批的」。
--    原 7 张表里没有任何一张能承载这个语义（audit_logs 是流水，不表达状态）。
--    新增表的代价最小：不动已有列、按 (session_id, status) 与 call_id 建索引即可。
--    C 实现时可以调整列名，但必须能表达：哪个 call、待审批/批准/拒绝、谁批的、
--    何时批的；以及「同一 call 只允许一条审批记录」。
-- 3. 事务要求：审批请求的写入必须与 tool_calls.status=awaiting_approval 同事务；
--    审批决策必须与 audit_logs 的 approval_decided 记录同事务。

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    agents_md_version TEXT,
    prefix_hash TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id TEXT,
    tool_name TEXT NOT NULL,
    arguments TEXT,
    status TEXT,
    reversible INTEGER,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    tool_call_id TEXT NOT NULL,
    content TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER,
    state_json TEXT,
    description TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    actor TEXT,
    action TEXT,
    detail TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS subagent_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_task_id TEXT,
    status TEXT,
    context_json TEXT,
    result_json TEXT,
    created_at INTEGER
);

-- 新增：审批记录（偏离 Java 版 schema 的唯一一处）
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT,
    description TEXT,
    status TEXT NOT NULL,
    -- status: pending / approved / denied
    decided_by TEXT,
    created_at INTEGER,
    decided_at INTEGER,
    UNIQUE (call_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id, status);
