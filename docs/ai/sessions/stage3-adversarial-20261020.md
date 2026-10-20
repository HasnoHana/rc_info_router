# 第 3 阶段：方案对抗（stage3-adversarial-20261020）

## 目的 / 结论 / 依据

- **目的**：给出完整 MVP 方案，主动交代砍掉的设计，供人类攻击；记录全部驳回与修正。
- **结论**：单进程双角色（FastAPI + 同进程 worker 线程）、3 张表（vendors / tasks / task_attempts）、受理时渲染快照 + 显式 rerender、6 个模块 + 2 个脚本 + 约 20 个测试用例。人类驳回 AI 原案 2 项，AI 经对抗维持原判 5 项。
- **依据**：stage1 技术栈决策与失效边界、stage2 八项定点；本阶段多轮对抗问答。

## 一、架构（定稿）

```
┌────────────────────── 单进程 (uvicorn) ─────────────────────┐
│  FastAPI (api/)              SQLite (store/)     worker 线程 │
│  POST /notifications  ──→  vendors 契约表            │      │
│  PUT/GET /vendors            tasks 任务表  ←──────── 轮询循环 │
│  POST /vendors/{id}/rerender task_attempts 历史表     ▼      │
│  GET /tasks/{id} (+/attempts, ?status=dead)   HTTP → 供应商  │
└─────────────────────────────────────────────────────────────┘
```

- worker 与 API 同进程，一条命令起全套。放弃独立进程的代价：负载共享 CPU，当前量级无影响；worker 本就是独立模块，未来可平移拆分。
- store.py 为唯一碰 SQL 的模块（存储隔离层，服务于 stage1 演进路径 SQLite→PG→MQ）。

## 二、数据模型（3 张表，待 stage4 冻结）

| 表 | 关键字段 | 职责与来历 |
|---|---|---|
| vendors | vendor_id, url_template, method, headers_template, body_template（占位符） | 供应商契约归口。业务方提交 vendor_id + variables。【人类驳回 AI 原案「无契约库、业务方自拼最终形态」后新增】 |
| tasks | id, vendor_id, 渲染后最终请求快照（url/method/headers/body）, status, attempts, next_attempt_at, idempotency_key(unique), created_at/updated_at | 受理时渲染快照，投递行为可追溯 |
| task_attempts | task_id, attempt_no, started_at, finished_at, outcome, status_code, error_detail, duration_ms | 每次投递尝试全记录。【人类驳回 AI 原案「last_error 覆盖写」】——运维需要时间维度观测（每分钟超时 vs 间歇 500 处置完全不同），一条 INSERT 的成本远低于其观测价值。配套 GET /tasks/{id}/attempts |

- 模板能力边界：仅变量占位符替换，不做条件逻辑/函数/映射（模板引擎深水区，超出即拒绝）。变量缺失受理时校验 → 400，不进队列。
- 放弃独立重试历史表的反悔记录：原判断「日志已够」错误，观测需求是真实工程经验。

## 三、契约变更语义（人类确认）

1. 每条任务在受理时刻用当时契约定格（快照），此后契约变更与它无关。
2. 改契约后的新提交自动用新契约（渲染逐条受理时发生，与批次无关，1 条也生效）。
3. 排队中的老任务切换新契约靠显式 `POST /vendors/{id}/rerender`（刷新 pending/retrying）；done 不动（既成事实）；dead 不自动复活（需人工确认后重提，自动复活会掩盖「为什么死」）。

- 对抗记录：人类质疑「改 URL 后 pending 任务投旧地址会出错」→ AI 量化窗口（≈队列消化时间，分钟级，非全部历史任务）→ 指出投递时渲染（方案 B）的镜像问题（同一任务生命周期内多版本契约投递、不可追溯、无法对证）→ 以显式 rerender 补热更新能力，**自动静默热更新被明确放弃**。

## 四、投递语义（stage2 定点，对抗未变）

2xx=送达；3xx 不跟随；响应体不读取。4xx→dead；5xx/超时/连接错→重试。指数退避 base=30s、factor=2、7 次尝试（约 1 小时窗口），参数配置化；jitter 暂不做（单 worker 无惊群），并发化后必选。HTTP 超时连接 5s+读 10s。状态机 `pending → delivering → done / retrying → dead`。崩溃恢复：启动时一条 UPDATE 回收僵尸 delivering。幂等 key 24h 去重。

## 五、优雅停机（细化版）

SIGTERM → worker 停止领新任务（标志位）→ 在途投递等自然结束（宽限 15s > 读超时 10s）→ 正常落库退出。超时强退残留 delivering 由崩溃恢复兜底（重复投递为至少一次既有代价）。**完整 drain（清空队列再退）明确不做**——95% 复杂度在保护不重要的 pending 任务，drain 唯一保护对象是 delivering 中的单个任务，宽限+恢复已覆盖。

## 六、对抗结果总表

| 议题 | AI 原案 | 对抗结果 |
|---|---|---|
| 供应商契约管理 | 不做契约库，业务方拼最终形态 | **人类驳回** → vendors 契约表 + 受理渲染 |
| 重试历史 | last_error 覆盖写 | **人类驳回** → task_attempts 全记录表 |
| 渲染时机 | 受理快照（被质疑） | 维持快照 + 新增显式 rerender |
| Webhook 事件订阅 | 砍掉（业务方显式调用为前提） | 维持砍掉，接入语境待人类最终确认 |
| Celery/Redis/MQ | 不采纳（stage1 论证） | 维持（人类此前已认可） |
| 独立 worker 进程 / 完整模板引擎 / 回调 HMAC 签名 / alembic / async 全链路 / 完整 drain | 均砍 | 维持 |

## 七、模块与工具

```
app/    api.py / models.py / store.py / worker.py / config.py / main.py
scripts/submit_notification.py   CLI：拉契约 → 生成表单填写 → 校验 → 提交
        mock_vendor.py           本地模拟供应商（可配失败率）
tests/  约 20 用例（契约渲染、rerender、attempts 历史、受理校验、投递全路径、崩溃恢复）
```

## 八、遗留

1. **接入语境待确认**：业务方显式调用 API（当前方案前提）vs 公司事件总线订阅（若为后者，事件订阅翻案）。
2. 会话导出方式（make sessions 数据来源）。
3. 契约起草 → 第 4 阶段。

## 下一步

人类已确认归档，进入第 4 阶段（契约冻结：起草 contracts/openapi.yaml + contracts/schema.sql 供人类审批）。
