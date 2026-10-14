"""Pure financial calculations; all rates use decimals, all money inputs use 亿元."""
import math
import random
from statistics import mean

MODEL = "GBM / daily exact log increments / v1"
HORIZONS = {63: 3, 126: 6, 252: 12}
THRESHOLDS = (.10, .20, .30, .40)


def number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + "必须是有限数值")
    if not low <= value <= high:
        raise ValueError(f"{label}必须在 {low} 至 {high} 之间")
    return value


def integer(value, label, low, high):
    number(value, label, low, high)
    if int(value) != value:
        raise ValueError(label + "必须是整数")
    return int(value)


def valuation(price, shares, scenarios, basis="pe"):
    number(price, "起点价格", .01, 1e6)
    number(shares, "总股本", .0001, 1e6)
    if basis not in ("pe", "pev"):
        raise ValueError("估值方法必须为 pe 或 pev")
    if not isinstance(scenarios, list) or len(scenarios) != 3:
        raise ValueError("必须提供悲观、中性、乐观三个情景")
    results = []
    for label, scenario in zip(("悲观", "中性", "乐观"), scenarios):
        if not isinstance(scenario, dict):
            raise ValueError("情景格式错误")
        amount = number(scenario.get("amount"), "利润/EV", -1e6, 1e6)
        multiple = number(scenario.get("multiple"), "估值倍数", 0, 1000)
        note = scenario.get("note")
        if not isinstance(note, str) or not note.strip() or len(note) > 1000:
            raise ValueError("每个情景必须填写假设说明（1 至 1000 字）")
        valid = amount > 0
        target = amount * multiple / shares if valid else None
        results.append({"scenario": label, "amount": amount, "multiple": multiple, "note": note,
                        "target_price": target, "price_return": target / price - 1 if valid else None,
                        "warning": None if valid else "利润/EV 非正，此估值法不适用；不输出负股价"})
    return {"basis": basis, "formula": "目标价 = 利润(亿元) × PE / 股本(亿股)" if basis == "pe" else "目标价 = EV(亿元) × P/EV / 股本(亿股)",
            "horizon": "用户假设的未来12个月估值情景；不赋予情景概率", "results": results}


def fundamentals(stock):
    latest = {key: item["observations"][-1]["value"] for key, item in stock["metrics"].items()}
    previous = {key: item["observations"][-2]["value"] for key, item in stock["metrics"].items()}
    market_cap = stock["price"]["value"] * stock["shares"]["value"]
    def ratio(n, d):
        return n / d if d > 0 else None
    output = {"PE": ratio(market_cap, latest["net_profit"]), "PB": ratio(market_cap, latest["equity"]),
              "归母净利润同比": ratio(latest["net_profit"] - previous["net_profit"], previous["net_profit"])}
    if stock["kind"] == "insurance":
        output.update({"P/EV": ratio(market_cap, latest["embedded_value"]),
                       "营运利润同比": ratio(latest["operating_profit"] - previous["operating_profit"], previous["operating_profit"]),
                       "NBV同比": ratio(latest["nbv"] - previous["nbv"], previous["nbv"])})
    else:
        output.update({"经营现金流/归母净利润": ratio(latest["operating_cash"], latest["net_profit"]),
                       "扣非利润/归母净利润": ratio(latest["recurring_profit"], latest["net_profit"]),
                       "简化自由现金流（亿元）": latest["operating_cash"] - latest["capex"],
                       "存货同比": ratio(latest["inventory"] - previous["inventory"], previous["inventory"]),
                       "资产负债率": ratio(latest["liabilities"], latest["assets"]),
                       "净有息负债（亿元）": latest["debt"] - latest["cash"]})
    return output


def quantile(sorted_values, p):
    position = (len(sorted_values) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def summarize(values, minima):
    ordered = sorted(values)
    n = len(values)
    low, high = ordered[0], ordered[-1]
    if high - low < 1e-12:
        low, high = low - .005, high + .005
    width = (high - low) / 32
    counts = [0] * 32
    for value in values:
        counts[min(31, max(0, int((value - low) / width)))] += 1
    up = sum(v > 1e-12 for v in values) / n
    down = sum(v < -1e-12 for v in values) / n
    return {"mean": mean(values), "quantiles": {str(p): quantile(ordered, p) for p in (.05, .25, .5, .75, .95)},
            "p_up": up, "p_down": down, "p_flat": max(0, 1 - up - down),
            "p_touch": {str(int(t * 100)): sum(v <= -t + 1e-12 for v in minima) / n for t in THRESHOLDS},
            "histogram": {"edges": [low + i * width for i in range(33)], "counts": counts},
            "max_mc_standard_error": .5 / math.sqrt(n)}


def simulate(mu=.06, sigma=.35, dividend_yield=.02, paths=3000, seed=42):
    mu = number(mu, "年化价格漂移率", -1, 1)
    sigma = number(sigma, "年化波动率", 0, 2)
    dividend_yield = number(dividend_yield, "年化股息率", 0, .3)
    paths = integer(paths, "路径数", 100, 10000)
    seed = integer(seed, "随机种子", 0, 2**32 - 1)
    rng = random.Random(seed)
    drift = (mu - .5 * sigma**2) / 252
    diffusion = sigma / math.sqrt(252)
    buckets = {month: {kind: [[], []] for kind in ("price", "total")} for month in HORIZONS.values()}
    for _ in range(paths):
        log_price = 0.
        min_price = min_total = 0.
        for day in range(1, 253):
            log_price += drift + diffusion * rng.gauss(0, 1)
            log_total = log_price + dividend_yield * day / 252
            min_price = min(min_price, log_price)
            min_total = min(min_total, log_total)
            if day in HORIZONS:
                bucket = buckets[HORIZONS[day]]
                for kind, value, minimum in (("price", log_price, min_price), ("total", log_total, min_total)):
                    bucket[kind][0].append(math.expm1(value))
                    bucket[kind][1].append(math.expm1(minimum))
    return {"model": MODEL, "calibration": "未经校准", "parameter_source": "用户可编辑假设；默认参数为演示常数，未从历史数据拟合",
            "parameters": {"mu": mu, "sigma": sigma, "dividend_yield": dividend_yield, "paths": paths, "seed": seed, "trading_days_per_year": 252},
            "formula": "S(t+dt)=S(t)*exp((mu-sigma²/2)*dt+sigma*sqrt(dt)*Z); TR(t)=S(t)/S(0)*exp(q*t)-1",
            "dividend_convention": "q 为恒定连续股息收益率，立即再投资；mu 是价格漂移，不再次减去 q；未计税费及离散除息",
            "barrier_convention": "相对初始投入，包含起点，每交易日收盘采样；非最大回撤；未统计日内或连续触及，可能低估连续触及概率",
            "limitations": "恒定参数、独立正态对数增量；无跳跃、厚尾、波动聚集、涨跌停、停牌或流动性约束；模拟概率不是经验证的真实胜率",
            "sampling_note": "最大蒙特卡洛标准误差 0.5/sqrt(N) 仅描述抽样噪声，不包括模型误差；0%/100% 不代表不可能/必然",
            "horizons": {str(month): {kind: summarize(*values) for kind, values in bucket.items()} for month, bucket in buckets.items()}}
