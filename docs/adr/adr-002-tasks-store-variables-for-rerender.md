# ADR-002：tasks 表增加 variables_json 列（支撑契约 rerender）

日期：2026-10-20 ｜ 状态：已批准 ｜ 影响：contracts/schema.sql（加列，向后兼容）

## 背景

stage4 冻结的 tasks 表只存「渲染后快照」，未存业务方提交的原始 variables。
stage5 第 2 步实现 rerender 时发现信息依赖断裂：rerender = 当前契约模板 ×
原始变量，原料缺失导致 stage3 定稿的 rerender 语义无法成立。
（设计疏漏发生在 stage3 方案审查，未被冻结环节发现，责任在 AI。）

## 决策

tasks 表新增 `variables_json TEXT NOT NULL DEFAULT '{}'`，受理时同时落库
渲染快照与原始变量。契约变更时的 rerender 用新模板 × 存量变量重渲染在途任务。

## 语义澄清（人类确认）

供应商/契约变更分两类，分开管理：
1. 仅变量不同（同一模板 × 不同业务事件）→ 每次受理独立渲染，无需契约动作。
2. 模板本身变更 → PUT /vendors 更新契约，随后显式 rerender 刷新在途任务。

## 影响评估

- 纯加列，向后兼容；旧行默认 '{}'。
- openapi 接口结构零变化。
- 快照可追溯性不受影响（成品照存，多存一份原料）。
