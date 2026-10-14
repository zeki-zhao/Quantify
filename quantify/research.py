"""Small point-in-time monthly factor experiment, independent of the MC model."""
import csv
import hashlib
import io
import math
import random
from datetime import date
from statistics import mean, pstdev

from .core import integer, number

FIELDS = ["date", "symbol", "total_return_index", "quality", "value", "available_at"]
CANDIDATES = ("momentum", "quality", "value", "low_volatility", "combined")


def demo_csv():
    rng = random.Random(20261013)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(FIELDS)
    levels = [100.] * 5
    for month in range(108):
        stamp = f"{2017 + month // 12}-{month % 12 + 1:02d}-01"
        common = rng.gauss(.004, .04)
        for i in range(5):
            levels[i] *= math.exp(common + rng.gauss(0, .06))
            # Factors deliberately have no designed predictive relationship with future shocks.
            writer.writerow([stamp, f"DEMO{i+1}", round(levels[i], 6), round(rng.random(), 6),
                             round(rng.random(), 6), stamp])
    return buf.getvalue()


def parse_panel(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 1_500_000:
        raise ValueError("CSV 必须为 UTF-8 文本，且不超过 1.5 MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames != FIELDS:
        raise ValueError("CSV 表头及顺序必须为：" + ",".join(FIELDS))
    grouped = {}
    for row in reader:
        if len(grouped) > 600:
            raise ValueError("最多支持 600 个月")
        try:
            stamp = date.fromisoformat(row["date"])
            available = date.fromisoformat(row["available_at"])
            symbol = row["symbol"].strip()
            values = {key: float(row[key]) for key in ("total_return_index", "quality", "value")}
        except (ValueError, TypeError, AttributeError):
            raise ValueError("CSV 日期或数值无效") from None
        if not symbol or len(symbol) > 40 or available > stamp:
            raise ValueError("代码无效或因子可用日期晚于观察日期（未来数据）")
        number(values["total_return_index"], "含分红指数", .000001, 1e12)
        for key in ("quality", "value"):
            number(values[key], key, -1e9, 1e9)
        bucket = grouped.setdefault(stamp, {})
        if symbol in bucket:
            raise ValueError("CSV 包含重复日期/代码")
        bucket[symbol] = values
    dates = sorted(grouped)
    if not 60 <= len(dates) <= 600:
        raise ValueError("研究需要 60 至 600 个连续月度截面")
    universe = set(grouped[dates[0]])
    if not 3 <= len(universe) <= 100:
        raise ValueError("每月需要 3 至 100 只股票")
    for i, stamp in enumerate(dates):
        if set(grouped[stamp]) != universe:
            raise ValueError("首版仅支持平衡面板：每月代码必须相同；真实数据须处理退市，不能静默删除")
        if i and (stamp.year * 12 + stamp.month) - (dates[i-1].year * 12 + dates[i-1].month) != 1:
            raise ValueError("日期必须每月一个截面且连续")
    return dates, [grouped[d] for d in dates]


def signals(panel, index):
    scores = {}
    for symbol, row in panel[index].items():
        prices = [panel[i][symbol]["total_return_index"] for i in range(index - 6, index + 1)]
        log_returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
        scores[symbol] = {"momentum": prices[-1] / prices[0] - 1,
                          "quality": row["quality"], "value": row["value"],
                          "low_volatility": -pstdev(log_returns)}
    ranks = {symbol: [] for symbol in scores}
    for factor in CANDIDATES[:-1]:
        for symbol in scores:
            value = scores[symbol][factor]
            lower = sum(scores[other][factor] < value for other in scores)
            equal = sum(scores[other][factor] == value for other in scores)
            ranks[symbol].append((lower + (equal - 1) / 2) / (len(scores) - 1))
    for symbol in scores:
        scores[symbol]["combined"] = mean(ranks[symbol])
    return scores


def statistics(returns):
    n = len(returns)
    p = sum(r > 0 for r in returns) / n
    return {"observations": n, "positive_rate": p, "mean_return": mean(returns),
            "worst_return": min(returns), "note": "样本为股票×入场月；横截面相关，不是独立试验，不提供伪精确置信区间"}


def evaluate(csv_text=None, horizon=3, top_k=2, cost_bps=20, source="人工合成研究面板 v1"):
    horizon = integer(horizon, "研究持有月数", 3, 12)
    if horizon not in (3, 6, 12):
        raise ValueError("持有月数必须为 3、6 或 12")
    top_k = integer(top_k, "筛选数量", 1, 20)
    cost_bps = number(cost_bps, "单次往返费用基点", 0, 1000)
    is_demo = csv_text is None
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("请填写数据来源和复权/分红口径（最多 500 字）")
    input_csv = demo_csv() if is_demo else csv_text
    dates, panel = parse_panel(input_csv)
    if top_k >= len(panel[0]):
        raise ValueError("筛选数量必须小于股票池数量")
    factor_scores = {i: signals(panel, i) for i in range(6, len(panel))}
    cost = cost_bps / 10000

    def event(index, factor=None):
        # Signal at month i close; enter next observation to avoid same-close execution/lookahead.
        symbols = sorted(panel[index])
        if factor:
            symbols.sort(key=lambda symbol: (-factor_scores[index][symbol][factor], symbol))
            symbols = symbols[:top_k]
        return [panel[index + 1 + horizon][s]["total_return_index"] /
                panel[index + 1][s]["total_return_index"] - 1 - cost for s in symbols]

    folds, selected_returns, baseline_returns = [], [], []
    # 36-month rolling fit, 12-month forward test; training labels mature BEFORE test signal.
    for start in range(42 + horizon, len(panel) - horizon - 1, 12):
        train = list(range(start - horizon - 36, start - horizon - 1))
        train = [i for i in train if i >= 6 and i + 1 + horizon < start]
        fitness = {factor: statistics([r for i in train for r in event(i, factor)])["positive_rate"]
                   for factor in CANDIDATES}
        chosen = max(CANDIDATES, key=lambda factor: fitness[factor])
        # Non-overlapping holding windows within/across 12-month test blocks.
        test = list(range(start, min(start + 12, len(panel) - horizon - 1), horizon))
        chosen_results = [r for i in test for r in event(i, chosen)]
        baseline = [r for i in test for r in event(i)]
        if not test:
            continue
        selected_returns.extend(chosen_results)
        baseline_returns.extend(baseline)
        folds.append({"train_signal_start": dates[train[0]].isoformat(), "training_signal_count": len(train),
                      "train_last_label": dates[train[-1] + 1 + horizon].isoformat(),
                      "test_signal_start": dates[test[0]].isoformat(),
                      "test_last_exit": dates[test[-1] + 1 + horizon].isoformat(),
                      "chosen_factor": chosen, "training_rates": fitness,
                      "selected": statistics(chosen_results), "baseline": statistics(baseline)})
    if not folds:
        raise ValueError("数据不足以形成完整的训练及样本外持有窗口")
    selected, baseline = statistics(selected_returns), statistics(baseline_returns)
    return {"is_demo": is_demo, "source": "人工合成研究面板 v1（与公司财报演示数据独立）" if is_demo else source,
            "status": "流程验证，不能证明真实胜率提升" if is_demo else "探索性滚动样本外统计；未经独立验证或概率校准",
            "parameters": {"horizon_months": horizon, "top_k": top_k, "round_trip_cost_bps": cost_bps,
                           "training_window_months": 36, "test_block_months": 12},
            "data_window": [dates[0].isoformat(), dates[-1].isoformat()],
            "dataset": {"sha256": hashlib.sha256(input_csv.encode("utf-8")).hexdigest(),
                        "raw_csv": input_csv, "frequency": "monthly",
                        "price_basis": "含分红再投资指数；拆股调整与真实口径由数据提供者声明，未经外部核验",
                        "units": {"total_return_index": "无量纲，一致基期的含分红指数", "quality": "无量纲", "value": "无量纲"},
                        "availability": "raw_csv 保留每个截面的 date 与财务因子 available_at；价格在观察时点可用"},
            "factor_definitions": {"momentum": "过去6个月含分红指数涨幅", "quality": "用户提供的时点可用盈利质量因子，越大越好",
                                   "value": "用户提供的时点可用估值因子（如盈利收益率），越大越好",
                                   "low_volatility": "过去6个月对数月收益标准差的相反数", "combined": "四因子截面百分位等权平均；不是概率"},
            "method": "滚动训练窗内选择上涨比例最高的候选因子，剔除标签尚未成熟的训练样本；固定因子进入下一测试块。月末信号、下一观察点入场；测试持有区间不重叠。基准为相同入场时间的全股票池。收益为含分红指数收益减一次往返费用。",
            "limitations": "训练期持有区间有重叠；测试股票间相关；仅5个候选规则，仍有选择偏差。平衡股票池可能引入幸存者偏差；CSV可用日期由用户声明，未验证公告；未模拟真实成交、停牌、涨跌停、组合资金与税费。不是组合净值回测，不承诺提升。需独立留出集及更长多市场数据。",
            "selected": selected, "baseline": baseline,
            "positive_rate_difference": selected["positive_rate"] - baseline["positive_rate"], "folds": folds}
