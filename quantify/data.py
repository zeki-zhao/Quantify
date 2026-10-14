"""Entirely invented fixtures; company names are labels, not financial facts."""
from copy import deepcopy

SOURCE = "人工合成演示数据 v1；未读取行情、公告或财报"
DATES = ["2023-12-31", "2024-12-31", "2025-12-31"]


def metric(label, unit, values, note=""):
    return {"label": label, "unit": unit, "source": SOURCE, "note": note,
            "observations": [{"period_end": date, "available_at": date[:4] + "-12-31",
                              "value": value} for date, value in zip(DATES, values)]}


def industrial(symbol, name, price, shares, profit, sigma):
    # Deliberately schematic, round values: NEVER present these as actual accounts.
    metrics = {
        "revenue": metric("营业收入", "亿元人民币", [200, 180, 210]),
        "net_profit": metric("归母净利润", "亿元人民币", profit),
        "recurring_profit": metric("扣非归母净利润", "亿元人民币", [round(p * .85, 2) for p in profit]),
        "operating_cash": metric("经营现金流净额", "亿元人民币", [profit[0] * .7, profit[1] * 1.1, profit[2] * .9]),
        "capex": metric("资本开支", "亿元人民币", [10, 12, 8], "简化为购建长期资产支付现金"),
        "inventory": metric("存货", "亿元人民币", [30, 38, 33]),
        "debt": metric("有息负债", "亿元人民币", [50, 60, 54]),
        "cash": metric("货币资金", "亿元人民币", [25, 22, 28]),
        "assets": metric("总资产", "亿元人民币", [180, 190, 200]),
        "liabilities": metric("总负债", "亿元人民币", [90, 100, 98]),
        "equity": metric("归母净资产", "亿元人民币", [75, 80, 88]),
    }
    return stock(symbol, name, "industrial", price, shares, metrics, sigma)


def stock(symbol, name, kind, price, shares, metrics, sigma):
    return {"symbol": symbol, "name": name, "kind": kind, "is_demo": True,
            "source": SOURCE, "as_of": "2026-10-12", "currency": "CNY",
            "price": {"value": price, "unit": "元/股", "as_of": "2026-10-12",
                      "source": SOURCE, "adjustment": "不复权合成起点价；非真实行情"},
            "shares": {"value": shares, "unit": "亿股", "as_of": "2026-10-12", "source": SOURCE,
                       "note": "假设总股本固定；未处理 A/H 股差异、增发、回购"},
            "metrics": metrics,
            "history_percentile": {"value": None, "window": None, "reason": "未提供历史估值序列，不计算历史分位"},
            "defaults": {"mu": .06, "sigma": sigma, "dividend_yield": .02, "paths": 3000, "seed": 42},
            "availability_note": "available_at 是合成占位时间，不是真实公告时间，禁止用于真实回测"}


_STOCKS = [
    industrial("301358.SZ", "湖南裕能", 36, 7.6, [16, 6, 10], .42),
    industrial("688005.SH", "容百科技", 28, 4.8, [8, 2, 5], .45),
    industrial("603501.SH", "豪威集团", 110, 12, [20, 30, 40], .38),
    industrial("600699.SH", "均胜电子", 18, 14, [8, 10, 12], .35),
    stock("601318.SH", "中国平安", "insurance", 52, 180, {
        "net_profit": metric("归母净利润", "亿元人民币", [800, 1000, 1100]),
        "operating_profit": metric("归母营运利润", "亿元人民币", [1100, 1150, 1200], "用于观察保险经营表现；非工业企业扣非利润"),
        "nbv": metric("新业务价值 NBV", "亿元人民币", [250, 300, 360]),
        "embedded_value": metric("集团内含价值 EV", "亿元人民币", [13000, 14000, 15000], "须核对集团/寿险边界及对应股本；此处全部合成"),
        "core_solvency": metric("核心偿付能力充足率", "%", [130, 135, 140], "假设某寿险主体口径；非集团汇总"),
        "total_solvency": metric("综合偿付能力充足率", "%", [210, 215, 220], "假设同一寿险主体口径；未判断监管达标"),
        "equity": metric("归母净资产", "亿元人民币", [8000, 8500, 9000]),
    }, .25),
]


class DemoDataProvider:
    def list_stocks(self):
        return deepcopy(_STOCKS)

    def get_stock(self, symbol):
        for item in _STOCKS:
            if item["symbol"] == symbol:
                return deepcopy(item)
        raise ValueError("未知股票代码")
