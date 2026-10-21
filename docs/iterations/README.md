# 迭代目录

本目录记录软件开发过程；运行时的 AI 因子实验另存应用实验数据库。执行约定见 [文档入口](../README.md)，范围与验收见 [构建计划](../BUILD_PLAN.md)。

## 索引

| 编号 | 日期（Asia/Shanghai） | 名称 | 计划项 | 状态 | 记录 |
| --- | --- | --- | --- | --- | --- |
| 000 | 2026-10-14 | 构建计划与迭代机制 | P0 | 完成 | [000-planning](000-planning/README.md) |
| 001 | 2026-10-14 | 第一阶段 CSV 研究基础设施 | P1.1–P1.4 | 完成（本轮范围） | [001-research-foundation](001-research-foundation/README.md) |
| 002 | 2026-10-14 | 任务队列与 AI 因子研究 | P2.1–P2.2 | 完成（手动协作范围） | [002-ai-research](002-ai-research/README.md) |
| 003 | 2026-10-15 | 在线行情与价格收益研究 | P1 在线补充 / P3.3 前置 | 阻塞（真实联网验收） | [003-online-data](003-online-data/README.md) |
| 004 | 2026-10-15 | 方案执行与自我迭代审计 | P1–P3 审计 / A1–A6 | 完成（审计；发现项待修复） | [004-plan-audit](004-plan-audit/README.md) |
| 005 | 2026-10-15 | 目标统一、最终验证与研究版本生命周期 | A1/A2、A3/A5/A6 基础 | 完成（研究范围） | [005-validation-lifecycle](005-validation-lifecycle/README.md) |
| 006 | 2026-10-15 | PlantUML 概要设计与架构文档 | P0 文档 / 当前实现架构固化 | 完成 | [006-outline-design](006-outline-design/README.md) |
| 007 | 2026-10-16 | OpenAI / DeepSeek API 因子迭代 | P2.3 模型API | 完成（软件/离线；真实API待验收） | [007-model-api](007-model-api/README.md) |
| 008 | 2026-10-16 | 组合账本与开发期回放 | P3.1 / A5 基础 | 完成（基础/离线范围） | [008-portfolio-ledger](008-portfolio-ledger/README.md) |
| 009 | 2026-10-16 | 因子诊断与跨会话试验统计 | A4 / 因子迭代诊断 | 完成（基础/离线范围） | [009-factor-diagnostics](009-factor-diagnostics/README.md) |
| 010 | 2026-10-16 | 诊断与模型反馈自我审查 | A4 / P2恢复与页面纠偏 | 完成（修复/离线回归） | [010-diagnostics-review](010-diagnostics-review/README.md) |
| 011 | 2026-10-17 | 云端开发进度核对与本地接续 | P0 交接与验证 | 完成（已提交进度/本地测试） | [011-local-sync](011-local-sync/README.md) |
| 011 | 2026-10-20 | 多因子引用与增量评估 | A3 / 因子自我迭代 | 完成（基础/离线范围） | [011-factor-increments](011-factor-increments/README.md) |
| 012 | 2026-10-20 | 组合独立验收与人工版本更替 | P3.2 / A5 | 完成（组合最终/人工/离线范围） | [012-portfolio-validation](012-portfolio-validation/README.md) |
| 013 | 2026-10-21 | 自动流水线与组合前向账本 | A6 / P3.2 | 完成（基础/离线范围） | [013-forward-pipeline](013-forward-pipeline/README.md) |

最近功能迭代013补来源追加→月度→单笔信号/成熟标签、组合实际决策前向账本、显式调度和恢复；A6/P3.2基础与离线验收完成，真实持续观察仍待积累。012组合独立验收与人工更替、011多因子增量及既有研究功能保留。概要设计当前27幅图。003真实行情与007真实模型联测等待本地条件；下一步优先真实来源/模型验收与前向积累，再扩展交易日历、公司行动/动态池及行业/统计校正。下一个可用编号：`014`。

并行历史说明：`011-local-sync` 与 `011-factor-increments` 来自不同开发环境，保留原始编号和目录；引用时使用完整目录名。构建计划中两条v12历史记录按日期区分。

## 记录规则

- 每次迭代建立 `NNN-short-name/README.md`，从 [模板](TEMPLATE.md) 复制；编号递增且不复用。
- 状态统一使用：待开始、进行中、阻塞、完成、取消。
- 开始登记索引，执行期间更新记录，结束同步计划进度及根目录交接文件。
- 记录实际日期、改动、验收证据与后续事项；旧测试结果必须标记为历史，不能当作本次执行。
- 需要附图或小型说明时放在对应迭代子目录。大体积数据、密钥、数据库和运行产物不放入本目录。
- 已结束迭代保留历史结论；后续发现问题通过新迭代说明，不悄悄重写原先结果。
