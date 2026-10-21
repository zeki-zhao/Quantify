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

- AI 研究室见 `/lab` 和 `docs/AI_RESEARCH.md`；手动提案保留；007新增OpenAI/DeepSeek API软件，真实账户联测待Key/网络。任务与日志保存在同一 SQLite，每个数据目录仅一个队列服务。

- 迭代 003 新增 `/market`：新浪快照、东财未复权日线、每日增量和独立价格收益模式。说明见 `docs/MARKET_DATA.md`。当前代理阻断真实联网验收；不得把合成接口测试描述为实际行情已接通。

- 迭代 005 新增 `/strategies`：结构化目标、固定单因子最终验证与暴露台账、人工启用/更替/回滚、手动月度信号和成熟标签。规则见 `docs/VALIDATION_LIFECYCLE.md`；008补组合基础账本；组合级最终验收/启用、行业诊断、自动串联与真实数据验收仍未完成。

- 架构与操作流程见 `docs/design/README.md`、`MODULES.md`、`FLOWS.md`；修改模块边界或关键流程时同步图源，文档渲染工具不属于应用运行依赖。

- 迭代007模型API见 `docs/MODEL_API.md`；密钥仅取环境，非密设置/模型调用台账在SQLite。请求前预留、未知结果不静默重发，估算单价不是供应商费用硬限额；真实行情验收按用户决定等待本地环境。

- 迭代008新增 `/portfolio` 与 `docs/PORTFOLIO.md`：Decimal资金/FIFO持仓、100股/T+1、显式执行状态与限价、双账本净值/回撤和后台任务；只读开发窗。组合版本绑定源实验与三类快照，不继承单笔最终验收资格；无公司行动/完整日历/真实盘口，勿将其描述为真实组合效果验收。

- 迭代009新增 `/diagnostics`、`docs/FACTOR_DIAGNOSTICS.md` 与第五种 `diagnostic` 任务：同开发测试窗覆盖/Rank IC/分组/年度与测试块稳定性/成对相关，跨会话尝试/重复/未完成计数；手动和双模型API每轮读取受限摘要。常数和空组为null，不能把诊断当作独立最终验证、组合增量或统计显著性；原准入政策不变。

- 010自查修复：模型同轮恢复仅忽略当前diagnostics.trial_counts变化，研究输入/配置仍严格绑定，已记录重试用原始请求；unknown不重发。诊断/组合共用TaskWatcher并按观察版本/请求序号防旧响应覆盖，研究室详情加请求序号；统计范围和因子勾选保持当前选择。见010审查记录。

- 011新增 `/increments` 与 `docs/FACTOR_INCREMENTS.md`：不可变多因子/精确权重、同窗添加/逐项移除及版本引用；第六种increment任务。AI可选augment模式固定原组合和候选占比，每轮一个新增公式，不逐轮累加、不自动启用。覆盖不全拒判改善；单笔增量不是净值归因/独立最终验收。下一步P3.2组合独立验收与更替，之后A6自动串联。

- 012新增 `/portfolio-validation`、`docs/PORTFOLIO_VALIDATION.md`：组合冻结/独立留出现金回放/专门准入与人工启用更替；第七种portfolio_validation任务，共用单笔暴露台账并检查已知开发暴露。最终结果不进入AI，启用仅改变研究引用；不继承持仓，组合前向账本和A6自动串联继续待做。

- 011本地接续（2026-10-17）：本地与GitHub的`work`同为`aa7dc3b`，云端已提交代码无需再拉取；Windows 126项测试及CLI启动/HTTP检查通过，浏览器未复跑。云端聊天、未提交内容和运行数据库尚未同步。下一功能优先A3多因子引用/增量评估，下个迭代012；详细证据见`docs/iterations/011-local-sync/README.md`。
