-- ============================================================
-- contracts/schema.sql —— 数据库契约（stage4 冻结）
-- 冻结后实现期只读；修改需人类批准并补 docs/adr/ 变更记录。
-- 时间字段统一 ISO8601 UTC 文本；ID 统一 uuid4 字符串。
-- ============================================================

-- 供应商契约表：外部 HTTP 通知目标的请求规格归口
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id        TEXT PRIMARY KEY,              -- 业务标识，如 'ads-a'、'crm-main'
    name             TEXT NOT NULL,                 -- 人类可读名称
    url_template     TEXT NOT NULL,                 -- 可含 {{var}} 占位符
    method           TEXT NOT NULL DEFAULT 'POST',
    headers_template TEXT NOT NULL DEFAULT '{}',    -- JSON：{"Header-Name": "{{var}}或字面量"}
    body_template    TEXT NOT NULL DEFAULT '',      -- 字符串模板，可含 {{var}}
    variables_schema TEXT NOT NULL DEFAULT '[]',    -- JSON 数组：契约要求的变量名清单，受理时校验
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 通知任务表：受理时渲染快照，此后不受契约变更影响
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,              -- uuid4
    vendor_id        TEXT NOT NULL REFERENCES vendors(vendor_id),
    url              TEXT NOT NULL,                 -- 渲染快照（最终形态）
    method           TEXT NOT NULL,
    headers_json     TEXT NOT NULL,                 -- 渲染快照，JSON
    body             TEXT NOT NULL,                 -- 渲染快照
    variables_json   TEXT NOT NULL DEFAULT '{}',    -- 受理时的原始变量（rerender 原料，ADR-002）
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','delivering','retrying','done','dead')),
    attempts         INTEGER NOT NULL DEFAULT 0,    -- 已执行投递次数
    next_attempt_at  TEXT NOT NULL,                 -- 轮询依据
    idempotency_key  TEXT UNIQUE,                   -- 可空；全局唯一永久去重（ADR-001）
    last_error       TEXT,                          -- 最近失败摘要（冗余自 task_attempts，便于单查）
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due
    ON tasks(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_tasks_vendor_status
    ON tasks(vendor_id, status);

-- 投递尝试历史表：每次投递一条记录，运维观测依据
CREATE TABLE IF NOT EXISTS task_attempts (
    id           TEXT PRIMARY KEY,                  -- uuid4
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    attempt_no   INTEGER NOT NULL,                  -- 第几次尝试（1 起）
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    outcome      TEXT CHECK (outcome IN
                 ('success','http_4xx','http_5xx','timeout','conn_error','unknown')),
    status_code  INTEGER,                           -- HTTP 状态码（有则填）
    error_detail TEXT,                              -- 超时类型/异常摘要（有则填）
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attempts_task
    ON task_attempts(task_id, attempt_no);
