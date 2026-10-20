# 内部通知投递服务（rc_nickname）

AI Coding 作业：API 通知系统设计与实现。

> **目的**：为企业内多个业务系统提供「把外部 HTTP 通知可靠送达供应商 API」的内部服务。
> **结论**：MVP = FastAPI 受理 + SQLite 队列表 + 单线程 worker 指数退避投递，至少一次语义，零消息队列。
> **依据**：作业需求描述 + 本仓库 `docs/ai/sessions/` 完整六阶段决策记录与 `docs/adr/` 变更记录。

---

## 1. 问题理解

多个业务系统在关键事件（广告注册回调、CRM 状态变更、库存变更）后需要调用外部供应商 HTTP API。需求的关键读法：

- **业务方不关心外部 API 的返回值**——这句话的主语是业务方。读取返回值、判断送达、失败重试的职责**转移到本服务**，这正是本系统存在的理由。
- 业务方只要求一件事：**稳定、可靠地送达**。
- 不同供应商 URL/Header/Body 格式不同——格式知识归口到本服务的**契约表**，由业务方提交变量、服务端渲染。

## 2. 架构与核心设计

```
┌────────────────────── 单进程 (uvicorn) ─────────────────────┐
│  FastAPI (api/)              SQLite (store/)     worker 线程 │
│  POST /notifications  ──→  vendors 契约表            │      │
│  PUT/GET /vendors            tasks 任务表  ←──────── 轮询循环 │
│  POST /vendors/{id}/rerender task_attempts 历史表     ▼      │
│  GET /tasks/{id} (+/attempts, ?status=dead)   HTTP → 供应商  │
└─────────────────────────────────────────────────────────────┘
```

核心设计（对应作业三问中的设计要求）：

### 2.1 投递语义：至少一次（at-least-once）

- 2xx = 送达；4xx 判死不重试；5xx/超时/连接错误进入指数退避重试（30s 起、×2、最多 7 次尝试、约 1 小时窗口），超限标记 `dead`（死信）。
- 超时被归入「结果未知」——重试可能导致供应商收到两次，**因此下游必须幂等**。这是文档里明确声明的边界，不在系统内解决。
- `delivering` 中间状态 + 启动时崩溃恢复（一条 UPDATE 回收僵尸任务），保证 worker 崩溃不丢任务。

### 2.2 契约管理与渲染

- `vendors` 表是供应商契约的唯一归口：URL/Header/Body 模板 + 变量清单。业务方提交 `vendor_id + variables`，服务端受理时渲染。
- **受理时渲染快照**：tasks 同时落库「渲染成品 + 原始变量」——成品保证投递行为可追溯，变量保证契约变更时 rerender 有原料（ADR-002）。
- 契约变更语义（三句话）：任务在受理时刻定稿；新提交自动用新契约；在途任务切换靠显式 `POST /vendors/{id}/rerender`。done 不动、dead 不自动复活。
- 模板能力刻意限定为**变量占位符替换**，不做条件逻辑/函数/映射——模板错误在契约管理和受理两个时刻全部拦下（400），不进投递队列。

### 2.3 可观测性

- 每次投递尝试落一条 `task_attempts` 记录（outcome/状态码/耗时/错误详情），`GET /tasks/{id}/attempts` 完整回放——运维能区分「每分钟超时一次」和「间歇 500」，这是两种完全不同的处置。
- CLI 工具（`scripts/submit_notification.py`）支持契约填空、提交、watch 观察到终态、历史回放。

## 3. 关键工程决策与取舍

每个决策的完整推理与对抗过程见 `docs/ai/sessions/`（stage1–stage5），此处摘录：

| 决策 | 选择 | 放弃了什么，为什么 |
|---|---|---|
| 队列载体 | SQLite 表 + worker 轮询 | 消息队列：当前规模下是未被论证的基础设施；Redis 作队列需自建 ack 协议（BLMOVE+processing 队列+超时回收），且 DB+MQ 双写会逼出 Transactional Outbox——绕一圈重新发明轮询。放弃的是投递延迟（亚秒→平均 0.5s）和「事件驱动」的优雅，换来零中间件与崩溃后状态天然一致 |
| 进程形态 | API 与 worker 同进程 | 独立 worker 进程：部署复杂度减半的收益大于负载隔离的收益（当前量级）；worker 本是独立模块，未来可平移拆分 |
| 供应商差异 | 服务端契约表 + 朴素模板 | 业务方各自拼最终形态：同供应商格式知识会散落多处（人类驳回 AI 原案后修正）；插件式适配器体系则明显过度 |
| 幂等键 | 全局唯一、永久有效 | 24h 窗口制（AI 最初提议）：与 schema UNIQUE 约束冲突并制造真缺陷，且与「宁重复不丢失」的至少一次语义自相矛盾（ADR-001，人类推翻） |
| 重试历史 | task_attempts 全记录 | last_error 覆盖写：省一张表但失去时间维度观测（人类驳回 AI 原案后修正） |
| HTTP 客户端 | 标准库 http.client | 异步客户端：个位数吞吐下收益为零，徒增调试成本 |
| 鉴权 | 不做 | 内部服务 + MVP；明确写入「不解决清单」而非假装它不存在 |

### 拒绝的过度设计（作业第 3 问的直接回答）

1. **Celery/Redis/消息队列**——见上表第一条，论证见 stage1「被驳回的提案记录」。
2. **完整模板引擎**（条件/函数/多级映射）——占位符替换是唯一被需要的能力。
3. **Web 表单控制台**——调用方是程序员，CLI 是正确量级。
4. **alembic 迁移工具**——一张表 + `CREATE TABLE IF NOT EXISTS`，工具的复杂度超过它省下的。
5. **优雅停机的完整 drain**——95% 的复杂度在保护不重要的 pending 任务；宽限期 + 崩溃恢复已覆盖唯一需要保护的对象（在途的那一个投递）。
6. **jitter 防惊群**——单 worker 串行投递物理上不会产生惊群；并发化后升级为必选（已写入演进条件）。

## 4. 明确不解决的问题

鉴权（内部服务）、多租户、优先级队列、响应体解析、回调签名（供应商契约不归我们定）、下游幂等性（至少一次语义的固有代价，由下游负责）、死信自动复活（掩盖失败归因，需人工确认后重提）。

## 5. 适用边界与演进路径（一等公民章节，非事后补充）

本选型**仅在小业务量下无问题**。量化边界与触发动作（stage1 人类确认）：

| 组件 | 失效边界 | 触发动作 |
|---|---|---|
| SQLite 轮询 | 存量百万级 / 活跃任务万级 / 写入每秒百次 | → PostgreSQL（分区表 + LISTEN/NOTIFY 原生推送，轮询消失） |
| 单 worker | 投递延迟 P99 > 10s 或积压持续增长 | → 加 worker 线程 + jitter（防惊群） |
| 单进程 | CPU 竞争影响 API 延迟 | → worker 拆独立进程 |
| 多业务线共享事件流 | 出现一份数据 N 个下游 | → 此时才引入消息队列（outbox 模式） |

量级推演：10 亿行 ≈ 500 GB 表，先崩的是 SQLite 单写锁下的写入吞吐而非轮询（带索引的轮询只扫活跃任务）。不预测流量，但每个组件的失效边界和触发动作预先写明。

## 6. 运行方式

依赖：Python 3.10+，pip 安装 `fastapi`、`uvicorn`（唯一两个第三方依赖，见 `requirements.txt`）。

```powershell
pip install fastapi uvicorn          # 或 pip install -r requirements.txt
make verify                          # 69 单元/集成测试 + 11 项端到端冒烟（Windows 无 make 时分开跑两条命令）
python scripts/mock_vendor.py 9901   # 终端 1：本地模拟供应商
python -m app.main                   # 终端 2：启动服务（API :8000 + worker）
python scripts/submit_notification.py --server http://127.0.0.1:8000 submit `
    --vendor ads-a --var user_id=u1 --var token=tk --idempotency-key evt-1 --watch
```

配置全部可环境变量覆盖（`NDS_RETRY_BASE_SECONDS` 等，见 [app/config.py](app/config.py)）。冻结契约：[contracts/openapi.yaml](contracts/openapi.yaml)、[contracts/schema.sql](contracts/schema.sql)。

## 7. AI 协作记录

六阶段工作流的完整会话归档在 `docs/ai/sessions/`（stage1 语境装配 → stage2 需求审讯 → stage3 方案对抗 → stage4 契约冻结 → stage5 小步实现），包含全部被驳回的提案与错误链记录；契约变更见 `docs/adr/`。`make sessions` 生成 `ai-sessions.zip`。
