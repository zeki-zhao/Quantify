# Quantify 关键操作流程

本文件与[概要设计](README.md)、[模块架构](MODULES.md)配套。图中接口省略主机前缀；所有网页 API 都由本机 `Handler` 接收。图中的 SQLite 参与者包含对应 Store，进程通过函数和数据库交互，不存在消息中间件。

## 1. CSV 导入与月度快照

入口：工作台导入表单 → `POST /api/workspace/import`。浏览器读取 UTF-8 文件，将 CSV 文本与来源、口径等元数据作为 JSON 提交。

```plantuml
@startuml flow_csv_import
skinparam defaultFontName Noto Sans CJK SC
autonumber
title CSV 导入、增量与月度转换
actor "研究者" as user
boundary "工作台页面" as ui
control "Handler / Workbench" as wb
control "datasets" as data
database "ResearchStore / SQLite" as db
user -> ui : 选择 CSV、来源、口径、性质与可选父快照
ui -> wb : POST /api/workspace/import
wb -> data : import_snapshot(契约参数)
data -> data : 校验 schema、有限数值、时间、重复及单位
opt 指定增量父快照
  data -> db : 读取父版本
  data -> data : 校验兼容性，合并更新并保留修订来源
end
alt 校验失败
  data --> wb : ValueError
  wb --> ui : 400 + 原因，不保存半份快照
else 内容与既有快照一致
  data -> db : 查找/复用内容版本
  db --> ui : 经服务返回既有快照摘要
else 新数据或修订
  data -> db : 保存不可变 snapshot
  db --> ui : 经服务返回新 ID、范围与质量提示
end
opt 日线已具备研究所需输入
  user -> ui : 选择收益口径、财务与股票池快照
  ui -> wb : POST /api/workspace/monthly
  wb -> data : monthly_from_daily(...)
  data -> db : 读取指定输入版本
  data -> data : 共同月末观察日；舍弃最后日历月
  data -> data : 按 available_at 连接财务；检查固定股票池
  alt 数据不足、池变化或口径不满足
    data --> ui : 经服务返回明确拒绝
  else 通过
    data -> db : 保存派生 monthly / monthly_price 快照
    data --> ui : 经服务返回派生快照与血缘
  end
end
@enduml
```

不对缺失总回报指数、财务字段或退市数据进行编造。时点查询 `POST /api/workspace/as-of` 只按声明的可用时间返回数据，不等于核验真实公告。

## 2. 报价查询、历史下载和每日调度

报价刷新和历史下载是不同通路：前者唤醒主进程行情线程，后者进入持久化工作队列。

```plantuml
@startuml flow_market_data
skinparam defaultFontName Noto Sans CJK SC
title 在线数据操作分流（当前实现，真实联网受阻）
start
:研究者保存股票、日期与开关;
if (操作为报价刷新？) then (是)
  :POST /api/market/refresh
或开启报价轮询;
  :行情线程按手动请求或
近似交易时段/间隔发起新浪 HTTPS 请求;
  if (取得合法且时间未倒退的报价？) then (是)
    :配置 revision 仍匹配时
保存最近有效报价及行情/接收时间;
  else (否)
    :保留旧好报价，记录错误；
失败退避，显示过期/失败状态;
  endif
  :前端 GET /api/market/state 轮询展示;
else (历史下载)
  if (自动每日调度？) then (是)
    :应用持续运行且开关开启；
中国时间工作日16:30之后;
    if (今日已尝试或有未结束历史任务？) then (是)
      :本轮不提交;
      stop
    endif
    :先保存 last_auto_day 调度意图;
  else (手动)
    :POST /api/market/history;
  endif
  :校验股票、范围、已收盘日期与父快照;
  :创建 history 任务，立即返回 job_id;
  :队列启动 worker；增量复核最近7天
或按完整范围复核;
  :逐股请求东财日线并校验代码/日期/OHLC/单位;
  if (整批成功且仍有执行所有权？) then (是)
    :合并父数据，保存一个不可变 daily 快照;
    :再次检查所有权/停止请求后
更新最新快照指针，结束任务;
  else (否)
    :失败/取消/中断留档；
不把整批下载报告为成功;
  endif
  :前端轮询任务与行情状态;
endif
stop
@enduml
```

历史下载失败或停止前可能已生成不可变产物，任务状态与产物是否存在分别处理；指针更新需再次通过执行所有权和停止检查。调度持久化意图可能使故障当日不再自动尝试，显式新建重试保留历史。当前只有工作日/时段近似规则，尚无交易所节假日日历验收。日线下载之后不会自动转月度、生成信号或触发 AI 研究。

## 3. 开发实验与滚动样本外计算

页面通过 `POST /api/lab/experiments` 入队；旧同步 `/api/workspace/experiments` 保留兼容，两者最终调用同一个 `Workbench.run`。

```plantuml
@startuml flow_walk_forward
skinparam defaultFontName Noto Sans CJK SC
title 开发实验与滚动验证
start
:固定 snapshot_id、factor_ids、持有期、
选股数、费用、封存月数与 objective;
:提交 experiment 任务，队列 worker 校验引擎;
:Workbench 校验固定面板、因子字段与历史长度;
if (输入有效？) then (否)
  :保存失败 experiment，任务失败;
  stop
endif
:按末尾至少12月确定 holdout_start;
:仅 development CSV 传入 evaluate；
保存封存边界但不计算最终期收益;
:共同预热30月，构造滚动训练/测试块;
while (仍有测试块？) is (是)
  :训练窗仅保留退出日期
严格早于测试信号的标签;
  :计算各候选训练统计；
以列表首项为训练基线;
  :assess 检查主目标改善、均值、尾部、样本量;
  if (有合格替代者？) then (是)
    :按主目标、再按均值选择因子;
  else (否)
    :保留首个训练基线因子;
  endif
  :固定因子进入下一12月测试块;
  :信号 i，入场 i+1，退出 i+1+horizon；
按持有期步进，测试区间不重叠;
  :同日期全池参照，同一往返费用；
保存逐窗指标及训练判定;
endwhile (否)
:汇总上涨比例、均值、最差单笔与样本量;
:保存 completed experiment、引擎和协议；
任务记录 experiment_id;
stop
@enduml
```

训练标签允许重叠，横截面股票收益存在相关性；测试“非重叠”不意味着所有单笔样本相互独立。比较入口固定相同快照、目标、成本、持有期、封存与测试窗口；复现要求完全相同的引擎摘要及 Python 版本。

## 4. ChatGPT / DeepSeek 手动因子迭代

本节为保留的手动流程：程序执行校验/实验/反馈，研究者在外部模型客户端获取提案。API自动模式见第10节；两种模式均不自动进入最终验证或启用。

```plantuml
@startuml flow_ai_iteration
skinparam defaultFontName Noto Sans CJK SC
autonumber
title 手动 AI 提案与受限研究循环
actor "研究者" as user
participant "外部模型客户端\nChatGPT / DeepSeek" as model
control "Lab / 页面 API" as lab
database "JobStore / SQLite" as jobs
control "QueueManager" as queue
participant "research worker" as worker
control "ResearchTools / Workbench" as tools
user -> lab : POST /api/lab/sessions（成功基线、目标和预算）
lab -> tools : 校验当前引擎，读取开发摘要
lab -> jobs : 创建 waiting_input 会话，固定 objective/协议
user -> lab : GET /api/lab/jobs/{id}/package
lab -> tools : 获取会话范围内上下文
lab --> user : 开发摘要、公式/提案规范、预算及反馈
user -> model : 手动复制协作包
model --> user : JSON 提案或 stop
user -> lab : POST /api/lab/proposals
lab -> lab : 严格校验 action/name/expression/rationale
lab -> jobs : 追加提案；waiting_input 变 queued
queue -> jobs : 领取任务，写 owner
queue -> worker : 启动子进程
loop 每轮直到预算、patience 或 stop
  worker -> jobs : 检查执行所有权/停止请求；保存 pending
  worker -> tools : 校验公式 AST、字段、历史和任务内重复
  alt 提案无效
    tools --> worker : 拒绝原因
    worker -> jobs : 记录 rejected，消耗轮次
  else 合法候选
    worker -> tools : 创建因子；parent = 当前最佳首因子
    worker -> jobs : 保存候选 ID 与确定轮次实验编号
    worker -> tools : 在原快照/目标/成本/窗口运行或复用实验
    tools --> worker : 开发结果和相对当前最佳的差值
    worker -> worker : 同时对当前最佳与原基线执行 assess；\n检查额外约束、共同样本数
    worker -> jobs : 保存 improved / not_improved / failed 和原因
  end
end
alt 尚有预算但没有后续提案
  worker -> jobs : waiting_input，保留进度
  user -> lab : 重新导出反馈并交给外部模型
else 达到停止条件
  worker -> tools : 有改进则保存 research_suggestion_only
  worker -> jobs : succeeded；activated = false
end
@enduml
```

非法公式和重复公式也会计入轮次与无改进次数；候选数在合法因子建立后计数。任务预算不等于跨会话过拟合控制。协作包不含原始行情或最终期收益，工具没有任意快照读取、最终验收、启用或交易入口。

## 5. 冻结与最终验收

入口：`POST /api/strategies/freeze` → 人工决定验收 → `POST /api/strategies/evaluate`。最终期使用冻结的单因子，不再次训练或挑选。

```plantuml
@startuml flow_final_validation
skinparam defaultFontName Noto Sans CJK SC
autonumber
title 冻结、资格预留与最终验收
actor "研究者" as user
control "Lifecycle" as lifecycle
database "SQLite / 产物与 final_exposures" as db
control "固定因子计算 / assess" as compute
user -> lifecycle : freeze（开发实验、固定因子、可选基线）
lifecycle -> db : 读取当前引擎成功实验及固定定义
lifecycle -> lifecycle : 固定快照/因子/参照/费用/目标/封存边界
lifecycle -> db : 保存不可变 freeze
lifecycle --> user : freeze_id
user -> lifecycle : evaluate(freeze_id)
lifecycle -> db : 查询该 freeze 的 final_evaluation
alt 已有结果
  db --> lifecycle : 原始结果
  lifecycle --> user : 缓存结果，不重新计算
else 尚无结果
  lifecycle -> lifecycle : 检查冻结引擎，加载绑定快照/定义
  group BEGIN IMMEDIATE：最终收益计算前预留
    lifecycle -> db : 查找已有预留及相交股票/日期区间
    alt 本 freeze 已预留但结果未提交
      db --> lifecycle : 不允许该记录重跑
    else 新预留
      lifecycle -> db : INSERT final_exposures；记录 overlaps
    end
  end
  alt 本 freeze 已预留但没有结果
    lifecycle --> user : 明确拒绝；中断记录不清除资格占用
  else 预留成功
    lifecycle -> compute : 仅以固定因子计算留出期信号与成熟收益
    alt 少于两个成熟窗口或计算校验失败
      compute --> lifecycle : 失败原因
      lifecycle -> db : 保存 failed / accepted=false
    else 计算完成
      compute --> lifecycle : 筛选/参照/全池统计与 policy checks
      lifecycle -> lifecycle : independent = 无 overlaps
      lifecycle -> lifecycle : accepted = independent 且政策通过
      lifecycle -> db : 保存唯一 final_evaluation
    end
    lifecycle --> user : 结果、独立性、拒绝理由；不自动启用
  end
end
note over lifecycle, db
  预留后若进程中断，可能没有 final_evaluation，但暴露台账保留。
  新来源、快照修订、目标或候选不能重置相交区间的独立性。
  同一数据目录之外的人工窥视无法识别。
end note
@enduml
```

至少两个成熟非重叠窗口：3/6/12月持有期分别需要至少8/14/26月留出数据，且工作台封存下限仍为12月。最少窗口和样本门槛用于软件准入，不代表统计显著。看过最终结果再调参，需要新的未暴露数据；重测可保存探索性结果，但不能据此启用。

## 6. 人工启用、替换、退役与回滚

入口：`POST /api/strategies/change`。`activate` 在已有活跃版本时就是替换；没有单独的自动更替服务。

```plantuml
@startuml flow_strategy_change
skinparam defaultFontName Noto Sans CJK SC
autonumber
title 人工研究版本更替与并发保护
actor "研究者" as user
boundary "验证与版本页面" as ui
control "Lifecycle.change" as lifecycle
database "SQLite / strategy / lifecycle_events" as db
user -> ui : 选择 activate / rollback / retire，并填写理由
ui -> lifecycle : action、freeze_id、expected_active、reason
alt activate 或 rollback
  lifecycle -> db : 读取 freeze 和 final_evaluation
  lifecycle -> lifecycle : 要求当前引擎、completed、independent、accepted
else retire
  lifecycle -> lifecycle : 校验操作及理由
end
group BEGIN IMMEDIATE
  lifecycle -> db : 读取最后生效事件得到当前 active_id
  alt active_id 不等于 expected_active 或目标已是当前状态
    lifecycle --> ui : 拒绝，要求刷新；不写生效事件
  else 当前状态匹配
    opt rollback
      lifecycle -> db : 要求目标版本曾经启用
    end
    lifecycle -> db : 按需写不可变 strategy 引用
    lifecycle -> db : 追加事件：前版本、目标、理由、实际生效时间
  end
end
lifecycle --> ui : 返回最新状态和完整事件历史
note over ui, db
  retire 的目标为空。
  rollback 新增事件，不回写旧实验、信号或标签。
  操作只改变之后的研究引用，不产生订单。
end note
@enduml
```

只有通过独立验收的版本可启用；合成版本保留合成标识，仅用于流程演示。退役版本的既有信号仍可更新成熟标签。

## 7. 新快照、前向信号与成熟标签

入口：`POST /api/strategies/signal`、`POST /api/strategies/observe`。两项均为研究者手动触发的同步操作。

```plantuml
@startuml flow_forward_observation
skinparam defaultFontName Noto Sans CJK SC
title 前向信号与成熟标签（操作分流）
start
:研究者选择追加的月度快照;
if (本次生成新信号？) then (是)
  :要求有活跃版本且引擎一致;
  :校验新快照保留冻结历史，
数据类型/口径/性质/固定池一致;
  :仅处理冻结后新增的最新截面，
不在未来且距当前中国日期不超过45天;
  if (校验通过？) then (否)
    :返回数据/版本原因，不生成信号;
    stop
  endif
  :计算固定因子，选择股票；
绑定快照、成本和实际 generated_at;
  :事务内再次核对活跃版本；
按 strategy_id + signal_date 幂等保存;
else (更新成熟标签)
  :选择已启用过的研究版本
及后续追加快照;
  :先校验冻结历史和全部信号所见历史；
修订/缺失/口径变化则拒绝;
  while (仍有未保存标签的信号？) is (是)
    :定位信号的下一月观察点为 entry；
exit = entry 后 horizon 月;
    if (退出观察点存在且日期已到？) then (否)
      :继续等待，暂不产生标签;
    else (是)
      if (entry 日期严格晚于实际生成日期？) then (否)
        :拒绝事后前向成绩；不将其解释为退化;
        stop
      else (是)
        :按固定股票和费用计算筛选/全池收益；
以 signal_id 幂等保存 forward_label;
      endif
    endif
  endwhile (否)
endif
:保留全部信号/标签，按入场排序
汇总不重叠窗口;
if (至少两个成熟窗口且达到样本门槛？) then (否)
  :显示成熟样本不足，不判断退化;
else (是)
  :与冻结最终验收成绩比较主目标/均值/尾部；
显示描述性退化提示，不自动替换;
endif
stop
@enduml
```

生成信号时未来观察点尚未出现，最终入场时间检查在成熟更新时完成。界面不可把已知历史收益补录成前向成绩。观察窗口不同于最终期，下降提示不等于统计显著的因子失效；数据异常需要核对或新版本验证，不能当作策略退化依据。

## 8. 暂停、取消、超时与重启恢复

任务控制入口为 `POST /api/lab/control`，新建重试为 `POST /api/lab/retry`。下图以运行中的 research 暂停为例；取消和超时使用相同的进程停止基础。

```plantuml
@startuml flow_job_recovery
skinparam defaultFontName Noto Sans CJK SC
autonumber
title 任务停止与检查点恢复
actor "研究者" as user
control "Lab / JobStore" as lab
control "QueueManager" as queue
participant "worker 子进程" as worker
database "SQLite / jobs / events / artifacts" as db
user -> lab : control(job_id, pause)
lab -> db : 事务写 stop_requested=paused 和控制日志
alt worker 先到检查点或结束写入
  worker -> db : 检查 owner / stop_requested
  worker -> db : finish 尊重已接受的停止状态
else 仍在计算
  queue -> db : 轮询停止请求或运行时限
  queue -> worker : terminate，必要时 kill
  queue -> db : 若仍 running，按请求结束；累计运行时长
end
user -> lab : resume(job_id)
lab -> db : 校验 paused 且有剩余预算
alt API会话，或有 pending / 未处理提案
  lab -> db : queued
  queue -> worker : 重新启动工作进程
  worker -> db : 恢复 pending 的候选和确定实验编号
  alt 实验产物已经提交
    db --> worker : 复用原轮次实验
  else 尚未提交
    worker -> worker : 重新计算该候选
  end
else 没有待执行内容
  lab -> db : waiting_input
end
== 应用停止后再次启动 ==
queue -> db : queued / running 改为 interrupted
note over lab, db
  waiting_input / paused 保留。
  failed / cancelled / timed_out / interrupted 可显式重试：
  新 job_id、新预算，retry_of 指向旧任务；旧记录不覆盖。
end note
@enduml
```

计算产物已经提交而任务未完成的情形，通过确定实验编号和检查点复用处理，不删除已存在结果。最终验收暴露台账不是任务预算；新建任务、重试或重启不能为已暴露区间重新取得独立资格。

## 9. 个股、估值与风险模拟

该流程保留最初 demo 能力，与历史因子研究的统计和数据来源分开。

```plantuml
@startuml flow_stock_analysis
skinparam defaultFontName Noto Sans CJK SC
autonumber
title 合成个股分析、估值情景与风险模拟
actor "研究者" as user
boundary "个股页面" as ui
control "Handler / service.analyze" as service
control "DemoDataProvider" as provider
control "core 纯计算" as core
user -> ui : 选择公司，编辑情景/起点价/GBM 参数
ui -> service : POST /api/analyze
service -> provider : 获取带来源/时间/单位的合成快照
provider --> service : 合成公司数据与风险说明
service -> core : 基本面比率、利润×PE 或 EV×P/EV
service -> core : 同种子/同路径的3/6/12月 GBM 模拟
core --> service : 价格/总回报分布、分位和每日收盘触及统计
service --> ui : JSON 报告与完整 CSV，保留未校准/合成标识
user -> ui : 本地下载报告
note over ui, core
  财务、情景假设、GBM 概率与真实因子上涨比例分别解释。
  不保存到研究任务队列，也不连接券商。
end note
@enduml
```

估值情景不赋予概率；GBM 触及事件相对初始投入，不是组合最大回撤。使用者修改假设后重新计算，报告保留输入及参数来源，不能据此承诺真实收益。


## 10. 模型API自动提案与调用预算

入口：研究室保存非密模型/单价配置，再以 `provider=openai|deepseek` 创建会话。API只执行受限提案请求，不是模型直接工具调用。

```plantuml
@startuml flow_model_api
skinparam defaultFontName Noto Sans CJK SC
autonumber
title 双提供方API：预算预留、反馈与不确定恢复
actor "研究者" as user
control "Lab / ModelSettings" as lab
participant "research worker / APIProposalProvider" as worker
database "SQLite / jobs / model_calls" as db
participant "OpenAI Responses / DeepSeek Chat" as api
control "ResearchTools / 本地实验引擎" as research
user -> lab : 配置提供方、模型、声明单价；创建带预算的会话
lab -> lab : 检查环境Key存在与配置完整
lab -> db : 固定模型/单价/预算，创建queued研究任务
loop 在原轮次/候选/patience/API预算内
  worker -> research : 每轮读取开发摘要、因子定义与当前反馈
  worker -> worker : 构造白名单上下文和固定协议请求
  worker -> db : 查同job/round已记录调用
  alt 研究输入/配置一致（仅当前计数可变）且已有成功提案
    db --> worker : 复用缓存JSON，不发HTTP
  else 该轮reserved/unknown或不可重试失败
    worker -> db : 安全停止；不静默重复请求
  else 可首次请求或明确可重试HTTP错误
    worker -> worker : 同轮重试沿用原存储完整请求与计数快照
    group BEGIN IMMEDIATE：网络I/O前
      worker -> db : 检查lease/stop与调用、token、估算费用预算
      worker -> db : 持久化reserved尝试与请求摘要
    end
    alt 预算不足
      worker -> db : api_budget_exhausted受控结束
    else 已预留
      worker -> api : 固定HTTPS请求；Key仅在Authorization头
      alt 有效usage与单条JSON提案
        api --> worker : 受限JSON + usage
        worker -> db : 结算用量，缓存提案，标记succeeded
      else 拒答、截断、无效提案或HTTP错误
        api --> worker : 失败/异常
        worker -> db : 记录安全分类；有usage则结算，否则保留预留
      else 超时、断线或进程被停止
        worker -> db : 保留预留，停止/恢复时标记unknown
      end
    end
  end
  opt 已有可用提案且任务仍允许执行
    worker -> research : 校验公式、创建版本、实验与原目标政策比较
    research --> worker : 实际开发结果与拒绝原因
    worker -> db : 保存轮次反馈，供下一轮同一提供方使用
  end
end
note over worker, api
  当前diagnostics.trial_counts变化不阻断同轮缓存；其余研究输入/配置仍绑定。
  每次重试要取得独立预留；仅明确429/5xx在设置的次数内可重试。
  未知调用不自动重发；用户新建重试会重置本地预算并可能再次付费。
  费用为用户单价估算，不是供应商账户硬限额；真实联测待凭据/网络。
end note
@enduml
```

图中的停止路径会退出研究循环；只有成功提案才进入本地实验。模型输出合法JSON但公式非法时，沿用原拒绝轮次/预算规则并把反馈提供给下一轮。原始行情、最终收益、环境密钥和启用权限不进入上下文。


## 11. 组合版本与开发期回放（008）

```plantuml
@startuml portfolio_replay
skinparam defaultFontName Noto Sans CJK SC
title 固定因子组合：开发窗资金回放
start
:选择当前引擎成功开发实验、固定因子\n绑定月度价格、未复权日线、显式执行状态;
:保存不可变 portfolio_spec\n含父版本、资金/调仓/费用、引擎、开发截止日;
:提交 portfolio 后台任务，检查所有权与停止请求;
:排除最终留出\n检查月度收盘匹配、日线/状态完整、限价和可用时间;
if (输入有效？) then (是)
  :建立筛选组合与等权全池两个独立账户;
  while (还有开发窗日线？) is (是)
    :检查取消/时限/所有权;
    :按上一信号固定股数，下一观察日开盘先卖后买\n整手/T+1/停牌/限价/现金/费用检查;
    :记录成交或拒单，剩余量取消\nFIFO 成本、现金与已实现盈亏;
    :收盘估值，核对权益 = 初始 + 已实现 + 未实现\n保存净值、现金、持仓市值、回撤;
    if (月度调仓信号日？) then (是)
      :仅用截至信号日的月度因子分值\n用当日权益/收盘价固定目标整手股数;
    endif
  endwhile (否)
  :最后无下一日线订单记为未执行\n保留期末持仓，不强平;
  :保存双账本 portfolio_run，任务成功;
else (否)
  :保存失败 portfolio_run 与原因，任务失败;
endif
:查看/导出或按同引擎版本新建复现\n不是独立最终验收，不触发启用或更替;
stop
@enduml
```

停止和所有权异常沿用工作进程退出路径，不保存不完整的成功账本。重启将未完成任务标为中断；显式重试创建新任务。合成示例、真实来源声明和未调整分红/公司行动的限制保留在报告与导出中。


## 12. 开发期因子诊断与AI反馈（009）

```plantuml
@startuml flow_factor_diagnostics
skinparam defaultFontName Noto Sans CJK SC
title 诊断、审计与模型反馈边界
start
if (调用来源？) then (页面)
  :选择成功开发实验、因子、分组数;
  :提交diagnostic任务，检查引擎/停止/所有权;
else (AI工具)
  :验证会话内实验与因子范围\n固定快照、持有期、费用、目标与引擎;
endif
if (相同请求与引擎已有报告？) then (是)
  :返回不可变缓存，不增加收益实验数;
else (否)
  :从源快照截除最终留出\n只保留源实验的非重叠成熟测试信号窗;
  while (还有测试信号窗？) is (是)
    :用截至信号日的输入算各因子分值\n下一月入场、到期退出、扣固定往返费用;
    :缺失/公式错误整截面拒算并记原因\n成功截面记录覆盖、平均秩IC与分组净收益;
    :同分不拆组，常数/空组为null;
  endwhile (否)
  :按年/测试块汇总，逐对在共同有效窗算相关\n保留有效窗数，不作显著性或组合增量结论;
  :保存成功报告或计算失败记录;
endif
:单个只读事务统计实验与已消费候选\n合并事件/pending/outcomes，按会话+轮次去重;
if (输出给模型？) then (是)
  :只取同协议尝试计数与会话内诊断\n基线/当前最佳和每轮成功候选均可附摘要;
  :API字段白名单裁剪，无原始行情/逐股收益/最终留出\n请求前仍预留原调用与上下文预算;
  :提出后续假设或停止\n不改变原准入，不自动启用;
else (页面)
  :展示/导出报告与全目录或同协议试验记录\n保留失败、重放、重复和未完成，提示选择偏差;
endif
stop
@enduml
```


## 13. 页面异步响应与当前任务（010）

```plantuml
@startuml flow_async_views
skinparam defaultFontName Noto Sans CJK SC
title 诊断 / 组合页：迟到响应不能覆盖当前任务
actor 研究者 as user
participant "页面" as page
participant "TaskWatcher" as watcher
participant "本地HTTP服务" as api
user -> page : 提交A
page -> watcher : start，观察版本递增
watcher -> api : 提交并取得job A
watcher -> api : GET A，记录轮询序号
user -> page : 提交B / 新建重试
page -> watcher : start，观察版本递增，禁用旧控制
watcher -> api : 提交并取得job B
watcher -> api : GET B，记录新序号
api --> watcher : 迟到的A响应
watcher -> watcher : 观察版本不符，丢弃
api --> watcher : 当前B状态
watcher -> page : 仅渲染B及其控制按钮
watcher -> api : GET B，旧轮询尚未返回
user -> page : 取消当前任务
page -> watcher : cancel，固定捕获job B
watcher -> api : POST control(B)，再查询新状态
api --> watcher : B cancelled，最新轮询序号
watcher -> page : 显示取消，禁用控制
api --> watcher : 迟到的B旧状态
watcher -> watcher : 序号已过期，丢弃
note over page, watcher
  完成后刷新列表/报告也校验当前观察版本和序号。
  研究室详情单独采用请求序号+所选ID防护。
  试验范围加载有独立序号与选择核对，加载期禁导出旧数据。
end note
@enduml
```


## 14. 多因子添加、移除与AI增量（011）

```plantuml
@startuml factor_increment_flow
skinparam defaultFontName Noto Sans CJK SC
title 固定多因子与配对增量评估
actor 研究者 as User
participant "网页 / AI worker" as Caller
participant "IncrementService" as Service
participant "factors" as Factor
participant "development_data / increments" as Calc
database SQLite as DB
User -> Caller : 选择2至10个版本及正权重
Caller -> Factor : create_blend(components, parent_id)
Factor -> DB : 保存叶子快照/ID/精确权重；不可覆盖
User -> Caller : 运行仅绑定此版本的基线实验
Caller -> DB : 保存固定源实验
User -> Caller : 选新增候选及占比，或创建augment会话
note right of Caller
  augment每轮提出一个公式
  原组合和占比固定，不逐轮累加
end note
Caller -> Service : request / submit / run
Service -> DB : 核对源实验/引擎/引用/候选
alt 同请求与引擎已有报告
  DB --> Service : 返回不可变缓存
else 首次评估
  Service -> Factor : 按精确比例加入候选并保存新组合
  Service -> Calc : 仅开发数据与源成熟信号窗
  loop 每个源测试信号窗
    Calc -> Factor : 原组合/候选/加入后/逐项移除
    Factor --> Calc : 仅当期及过去输入的截面分值
    Calc -> Calc : 同池Top K、延迟入场、同退出与成本
    alt 任一变体缺失/错误
      Calc -> Calc : 整配对窗拒算；保留原因
    else 完整
      Calc -> Calc : 保存配对指标及差值
    end
  end
  Calc --> Service : 汇总/时期/移除贡献/覆盖与决策
  Service -> DB : 不可变factor_increment；失败也留档
end
Service --> Caller : 本地完整证据 / AI聚合白名单摘要
Caller -> Caller : augment核对候选实验指标与配对覆盖
Caller -> User : 当前最佳/原基线比较；不自动启用
note over Caller, Calc
  不读取最终留出；覆盖不完整不得判改善
  单笔差值不是资金净值归因，仍须独立最终验收
end note
@enduml
```


## 15. 组合冻结、独立最终回放与人工更替（012）

```plantuml
@startuml portfolio_validation_flow
skinparam defaultFontName Noto Sans CJK SC
title 组合独立验收与人工版本生命周期
actor 研究者 as User
participant "页面 / PortfolioLifecycle" as Service
participant "队列工作进程" as Worker
participant "共享exposures.reserve" as Exposure
participant "固定区间输入 / 组合账本" as Ledger
participant "组合政策" as Policy
database SQLite as DB
User -> Service : 已成功开发回放 / 参照 / 准入政策
Service -> DB : 冻结完整规则、数据、原调仓相位和端点
User -> Service : 提交最终验收
Service -> Worker : portfolio_validation(freeze_id, engine)
Worker -> DB : 查询不可变最终报告
alt 已有完整结果
  DB --> Worker : 成功或失败缓存；不再计算
else 首次验收
  Worker -> Exposure : 预留股票/完整最终区间
  Exposure -> DB : BEGIN IMMEDIATE
  Exposure -> DB : 核对单笔/组合历史暴露及已知开发暴露
  Exposure -> DB : 插入portfolio:freeze_id，COMMIT
  Worker -> Ledger : 按冻结边界从现金开始回放
  Ledger -> Ledger : 原调仓相位 / 下一观察点开盘 / 固定成本
  Ledger --> Policy : 候选与冻结参照净值、完整共同周期
  Policy --> Worker : 严格目标 + 收益/回撤/尾部/样本约束
  Worker -> DB : 完整不可变报告；accepted还需独立性
end
Worker -> DB : 任务成功交付报告（不等于准入通过）
Worker --> Service : 最终证据；不进入AI反馈
User -> Service : 人工启用 / 替换 / 回滚 / 退役，带理由及expected_active
Service -> DB : 核对独立通过、引擎、交付、当前版本和回滚历史
Service -> DB : 追加实际生效时间与前后版本
Service --> User : 当前组合研究引用及事件
note over Worker, DB
  失败/取消/中断保留暴露；无结果不得同冻结重算
  已提交报告但任务取消时，须成功交付缓存后才可启用
  不迁移历史持仓，不自动生成前向账本或交易
end note
@enduml
```
