"""Bounded factor expression language: parse AST, interpret vectors, never eval code."""
import ast
import math
from functools import lru_cache
from statistics import mean, pstdev

GRAMMAR = {
    'fields': ['quality', 'value'],
    'functions': ['momentum(window, skip)', 'volatility(window, skip)', 'rank(expression)', 'zscore(expression)'],
    'operators': ['+', '-', '*', '/'],
    'limits': {'characters': 500, 'nodes': 80, 'depth': 12, 'window': [2, 24], 'skip': [0, 6]},
    'notes': '窗口单位为月；volatility 为正波动率，低波动用负号；只访问当前及过去。除零和非有限结果报错。',
}


@lru_cache(maxsize=256)
def _parse(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 500:
        raise ValueError('公式必须为 1 至 500 字符')
    try:
        root = ast.parse(expression.strip(), mode='eval').body
    except (SyntaxError, RecursionError):
        raise ValueError('公式语法无效') from None
    if sum(1 for _ in ast.walk(root)) > 80:
        raise ValueError('公式节点过多')
    fields, history = set(), 0

    def visit(node, depth=0):
        nonlocal history
        if depth > 12:
            raise ValueError('公式嵌套过深')
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if abs(node.value) > 1e6 or not math.isfinite(node.value):
                raise ValueError('公式常数超出范围')
        elif isinstance(node, ast.Name) and node.id in ('quality', 'value'):
            fields.add(node.id)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            visit(node.operand, depth + 1)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            visit(node.left, depth + 1)
            visit(node.right, depth + 1)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name in ('momentum', 'volatility'):
                if len(node.args) != 2 or any(not isinstance(arg, ast.Constant) or type(arg.value) is not int for arg in node.args):
                    raise ValueError('动量/波动窗口及跳过月数必须为两个整数常量')
                window, skip = (arg.value for arg in node.args)
                if not 2 <= window <= 24 or not 0 <= skip <= 6:
                    raise ValueError('窗口需为 2–24 月，跳过需为 0–6 月，禁止未来引用')
                history = max(history, window + skip)
            elif name in ('rank', 'zscore') and len(node.args) == 1:
                visit(node.args[0], depth + 1)
            else:
                raise ValueError('公式包含未支持的函数或参数数量')
        else:
            raise ValueError('仅允许数值、已知字段、四则运算和白名单函数')

    visit(root)
    return root, tuple(sorted(fields)), history, ast.dump(root, include_attributes=False)


def validate_expression(expression):
    if not isinstance(expression, str):
        raise ValueError('公式必须是文本')
    _, fields, history, normalized = _parse(expression)
    return {'required_fields': list(fields), 'history_months': history, 'normalized': normalized}


def evaluate_expression(expression, panel, index):
    root, _, history, _ = _parse(expression)
    if index < history:
        raise ValueError('公式历史窗口不足')
    symbols = list(panel[index])
    memo = {}

    def walk(node):
        if id(node) in memo:
            return memo[id(node)]
        if isinstance(node, ast.Constant):
            values = dict.fromkeys(symbols, float(node.value))
        elif isinstance(node, ast.Name):
            values = {symbol: panel[index][symbol][node.id] for symbol in symbols}
            if any(value is None for value in values.values()):
                raise ValueError(f'{node.id} 公式字段缺少输入')
        elif isinstance(node, ast.UnaryOp):
            sign = -1 if isinstance(node.op, ast.USub) else 1
            values = {symbol: sign * value for symbol, value in walk(node.operand).items()}
        elif isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            values = {}
            for symbol in symbols:
                a, b = left[symbol], right[symbol]
                if isinstance(node.op, ast.Add):
                    value = a + b
                elif isinstance(node.op, ast.Sub):
                    value = a - b
                elif isinstance(node.op, ast.Mult):
                    value = a * b
                else:
                    if abs(b) < 1e-12:
                        raise ValueError('公式除数为零或接近零')
                    value = a / b
                values[symbol] = value
        else:
            name = node.func.id
            if name in ('momentum', 'volatility'):
                window, skip = (arg.value for arg in node.args)
                end = index - skip
                values = {}
                for symbol in symbols:
                    prices = [panel[i][symbol]['close'] if 'close' in panel[i][symbol] else panel[i][symbol]['total_return_index']
                              for i in range(end - window, end + 1)]
                    values[symbol] = (prices[-1] / prices[0] - 1 if name == 'momentum' else
                                      pstdev([math.log(b / a) for a, b in zip(prices, prices[1:])]))
            else:
                raw = walk(node.args[0])
                if name == 'rank':
                    values = {symbol: (sum(v < value for v in raw.values()) +
                                       (sum(v == value for v in raw.values()) - 1) / 2) / (len(raw) - 1)
                              for symbol, value in raw.items()}
                else:
                    center, scale = mean(raw.values()), pstdev(raw.values())
                    values = {symbol: (value - center) / scale if scale > 1e-12 else 0.
                              for symbol, value in raw.items()}
        if any(not math.isfinite(value) or abs(value) > 1e12 for value in values.values()):
            raise ValueError('公式结果不是有限数值或超出范围')
        memo[id(node)] = values
        return values

    return walk(root)
