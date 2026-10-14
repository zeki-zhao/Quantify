import csv
import io
from datetime import datetime, timezone

from .core import fundamentals, simulate, valuation, number
from .data import DemoDataProvider


def analyze(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须为 JSON 对象")
    stock = DemoDataProvider().get_stock(payload.get("symbol"))
    price = number(payload.get("price", stock["price"]["value"]), "起点价格", .01, 1e6)
    shares = number(payload.get("shares", stock["shares"]["value"]), "股本", .0001, 1e6)
    basis = payload.get("basis", "pe")
    if basis == "pev" and stock["kind"] != "insurance":
        raise ValueError("P/EV 仅用于此 demo 的保险企业")
    scenario_results = valuation(price, shares, payload.get("scenarios"), basis)
    params = payload.get("simulation", {})
    if not isinstance(params, dict) or set(params) - {"mu", "sigma", "dividend_yield", "paths", "seed"}:
        raise ValueError("模拟参数字段无效")
    return {"schema_version": "1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
            "warning": "合成演示数据；未经校准；不代表真实投资胜率；不自动交易",
            "stock": stock, "fundamentals": fundamentals(stock),
            "user_inputs": {"price": price, "shares": shares, "source": "用户可编辑估值与模拟起点；基本面表估值仍使用演示快照价格/股本"},
            "valuation": scenario_results, "simulation": simulate(**params)}


def export_csv(report):
    """Long-form CSV preserves ALL nested values/metadata, with spreadsheet injection protection."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["field", "value"])

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + "." + str(key) if path else str(key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        else:
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            writer.writerow([path, value])
    walk(report, "")
    return buf.getvalue()
