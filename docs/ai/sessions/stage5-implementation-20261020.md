# 第 5 阶段：小步实现（stage5-implementation-20261020）

## 目的 / 结论 / 依据

- **目的**：按冻结契约分 5 小步实现 MVP，每步可验证，全程测试护航。
- **结论**：MVP 完成——69 单元/集成测试 + 11 项端到端冒烟全绿；真实三进程环境完成「断供应商→退避重试→自动恢复」实景验证。
- **依据**：stage4 冻结契约 + stage1-3 全部决策。

## 五小步记录（每步均含改动清单/验证命令/通过标准）

1. **存储层**：app/store.py + app/config.py + tests/test_store.py（13→25 用例）。零新增依赖（unittest 代替 pytest，避免为收益有限的 fixture 引入依赖）。
2. **渲染 + 受理层**：app/render.py + app/api.py + app/models.py + tests（+22）。发现冻结契约信息缺口（rerender 需要原始变量而快照未存）→ 按 2.2 走审批 → ADR-002 加列。抓到跨线程 SQLite 崩溃（FastAPI 线程池），修复为 check_same_thread=False + 全方法锁串行化。
3. **worker 投递**：app/worker.py + app/main.py + scripts/mock_vendor.py + tests/test_worker.py（+13，全真实 HTTP）。五种 outcome 分流、退避、耗尽判死全历史、双超时、优雅停机、崩溃恢复端到端。
4. **回归 + 工具**：Makefile + scripts/submit_notification.py（CLI，标准库）+ scripts/e2e_smoke.py 转正。真实三进程栈验证重试恢复闭环。
5. **文档交付**：README.md（问题理解/架构/取舍/拒绝的过度设计/演进）+ requirements.txt + 本归档 + ai-sessions.zip。

## 人类回归要求与审查结果（stage5 中期）

人类要求「多写测试再查一遍」→ AI 自审发现 4 个真问题并全部修复：
1. header 模板残留占位符静默通过（残留检查未覆盖 header）
2. `GET /tasks?limit=-5` → SQLite `LIMIT -1` 无界全表（改为显式 400；AI 第一版钳位方案写测试时自我否决——无效输入应可见）
3. PUT /vendors 不校验模板合法性（错误推迟到投递时暴露，违背「模板错误拦在管理时刻」决策）
4. 渲染未知名变量抛裸 KeyError（统一 RenderError）

## 本阶段发现的设计缺陷链（作业「对 AI 输出的判断与修正」素材）

1. **幂等键 24h 窗口**（AI stage2 引入）→ 与 schema UNIQUE 冲突制造真缺陷 → 第一版修复（DELETE 旧行）将连带删除投递历史，写完即自我否决 → 过渡方案（:expired 后缀）→ 人类质疑窗口语义不合常理 → 根因废除，永久唯一（ADR-001，人类掀翻）。
2. **rerender 信息缺口**：快照决策与 rerender 决策的变量依赖在 stage3/4 均未被发现，stage5 实现时暴露 → 停下走 2.2 审批 → ADR-002 加列（人类批准，并补充「变量变更与模板变更分开管理」的语义澄清）。

## 最终验证记录（已实际执行）

- `python -m unittest discover -s tests` → Ran 69 tests, OK
- `python scripts/e2e_smoke.py` → E2E regression: 11/11 passed
- 真实环境冒烟：CLI 提交 → mock 供应商不可达（3 次 conn_error + 退避）→ 供应商就绪后第 4 次尝试 success → done；CLI watch/status 全程可观测。

## 交付物核对（作业要求）

- README.md（问题理解/架构/取舍）✓
- MVP 代码 ✓
- ai-sessions.zip（make sessions）✓
