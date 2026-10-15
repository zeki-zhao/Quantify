# 用户指定的交接入口：agnet.md

本文件名按用户要求保留，标准代理指令另见 `AGENTS.md`。

- 目标：个人使用的轻量股票量化研究，重点评估因子在滚动样本外能否改善上涨比例，同时观察收益与风险；无账户、无自动交易。
- 架构：Python 标准库 HTTP 服务；`quantify/` 纯计算与数据；`static/` 个股与工作台界面；SQLite 持久化；`tests/` 核心、数据、工作台及 HTTP 测试。
- 后续架构与实施顺序以 `docs/BUILD_PLAN.md` 为准：真实历史数据与每日更新 → 因子版本与实验对比 → ChatGPT 迭代 → 组合、实时行情与前向观察。允许按需接入数据/模型 API 和本地 SQLite，继续保持个人本地使用。
- 每次开发前阅读构建计划与 `docs/iterations/README.md`；在 `docs/iterations/NNN-short-name/README.md` 记录本次范围、变更、验证和下一步，并同步索引、计划进度与 `HANDOFF.md`。详细约定见 `docs/README.md`，保持与 `AGENTS.md` 一致。
- 启动：在 `D:\Engineering\Quantify` 执行 `python app.py`，访问 `http://127.0.0.1:8000`。
- 测试：`python -m unittest discover -s tests -v`。
- 已完成：CSV 快照与增量、财务/股票池时点查询、因子参数/公式版本、留出实验持久化与对比、后台队列、手动 ChatGPT 提案/实验/反馈与预算停止；五股合成基本面、保险指标、可编辑估值、3/6/12月 GBM 风险、收益图和风险表、JSON/CSV导出、可导入月度面板的简化因子实验。
- 限制：合成数据不是财报或行情，模拟未经校准；因子研究不是组合回测，真实面板的可用日期与退市偏差尚需人工核验。
- 下一步：接入带时点信息的数据、处理动态股票池与退市、独立留出验证与概率校准。完整继续开发说明见 `HANDOFF.md`；使用说明见 `README.md`。

- CSV 字段与时间规则见 `docs/DATA_CONTRACTS.md`；`runtime/` 数据库和原始数据不提交 Git。版本不可覆盖，实验绑定具体版本。来源声明不等于真实数据核验。

- AI 研究室见 `/lab` 和 `docs/AI_RESEARCH.md`；本轮使用手动提案，没有远程模型 API 调用。任务与日志保存在同一 SQLite，每个数据目录仅一个队列服务。

- 迭代 003 新增 `/market`：新浪快照、东财未复权日线、每日增量和独立价格收益模式。说明见 `docs/MARKET_DATA.md`。当前代理阻断真实联网验收；不得把合成接口测试描述为实际行情已接通。
