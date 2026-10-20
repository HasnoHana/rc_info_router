# 第 4 阶段：契约冻结（stage4-contract-freeze-20261020）

## 目的 / 结论 / 依据

- **目的**：起草并冻结实现期只读契约：contracts/openapi.yaml + contracts/schema.sql。
- **结论**：契约已起草、验证、人类批准冻结（2026-10-20）。
- **依据**：stage3 定稿方案。

## 契约要点

- schema.sql：vendors（含 variables_schema 变量清单）/ tasks（渲染快照 + CHECK 状态机 + 幂等唯一索引 + last_error 冗余列）/ task_attempts（outcome 枚举约束）。
- openapi.yaml：8 端点——POST /notifications（202 受理，幂等命中返原 task_id）、GET/PUT /vendors、POST /vendors/{id}/rerender、GET /tasks/{id}、GET /tasks（status/vendor_id/limit 过滤，默认 50 上限 200）、GET /tasks/{id}/attempts。
- 起草中自洽性修正：Task.last_error 同步补进 schema.sql（注释声明为冗余摘要列）。
- 克制版校验：variables_schema 采用变量名清单而非 JSON Schema（免依赖，够用）——人类批准。

## 验证记录（已实际执行）

- schema 在内存 SQLite 中 executescript 通过，3 表建立、列齐全；冒烟测试确认非法 status 被 CHECK 拦截（IntegrityError）。
- 临时验证脚本 scripts/_check_schema.py 验证后删除。

## 冻结状态

两份契约自 2026-10-20 起冻结。修改需：停下 → 说明原因和影响 → 人类批准 → docs/adr/ 补记录。

## 遗留

- 业务方接入语境（显式调用 vs 事件总线）仍开放，契约按显式调用起草。

## 下一步

进入第 5 阶段（小步实现），第 1 步：store.py + schema 初始化 + 测试。
