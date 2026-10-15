"""Small parameterized factor registry. Definitions are data, never executable strings."""
import math
from statistics import mean, pstdev
from .expressions import validate_expression, evaluate_expression

KINDS = ('momentum', 'quality', 'value', 'low_volatility', 'combined')
NAMES = {'momentum': '动量', 'quality': '盈利质量', 'value': '价值',
         'low_volatility': '低波动', 'combined': '四因子等权排名'}
BUILTINS = tuple({'id': kind, 'key': kind, 'kind': kind, 'lookback': 6, 'skip': 0,
                  'name': NAMES[kind], 'reason': '内置基线', 'parent_id': None}
                 for kind in KINDS)


def validate_spec(spec):
    if spec.get('kind') not in (*KINDS, 'expression'):
        raise ValueError('不支持的因子类型')
    if spec['kind'] == 'expression':
        validate_expression(spec.get('expression'))
    for name, low, high in (('lookback', 2, 24), ('skip', 0, 6)):
        value = spec.get(name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name} 必须为 {low} 至 {high} 的整数')
    if spec['kind'] in ('quality', 'value') and (spec['lookback'], spec['skip']) != (6, 0):
        raise ValueError('质量与价值因子直接读取财务字段，不支持窗口参数')
    for name in ('name', 'reason', 'key'):
        value = spec.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError(f'{name} 必须为非空文本且不超过 500 字')


def initialize_factors(store):
    for spec in BUILTINS:
        store.put('factor', {key: value for key, value in spec.items() if key != 'id'})


def create_version(store, *, parent_id, name, reason, lookback=6, skip=0, expression=None):
    parent = store.get('factor', parent_id)
    spec = {'key': parent['key'], 'kind': parent['kind'], 'name': name, 'reason': reason,
            'lookback': lookback, 'skip': skip, 'parent_id': parent_id}
    if expression is not None:
        spec.update(kind='expression', expression=expression, lookback=6, skip=0)
    elif parent['kind'] == 'expression':
        spec['expression'] = parent['expression']
    validate_spec(spec)
    return store.put('factor', spec)


def required_fields(specs):
    needed = set()
    for spec in specs:
        if spec['kind'] in ('quality', 'value'):
            needed.add(spec['kind'])
        elif spec['kind'] == 'expression':
            needed.update(validate_expression(spec['expression'])['required_fields'])
        elif spec['kind'] == 'combined':
            needed.update(('quality', 'value'))
    return needed


def history_length(specs):
    return max(validate_expression(spec['expression'])['history_months'] if spec['kind'] == 'expression'
               else (spec['lookback'] + spec['skip'] if spec['kind'] not in ('quality', 'value') else 0)
               for spec in specs)


def series_level(row):
    """Use the explicitly parsed series without aliasing prices as a total-return index."""
    return row['close'] if 'close' in row else row['total_return_index']


def calculate(panel, index, specs):
    scores = {symbol: {} for symbol in panel[index]}
    for spec in specs:
        kind, factor_id = spec['kind'], spec['id']
        if kind == 'expression':
            for symbol, value in evaluate_expression(spec['expression'], panel, index).items():
                scores[symbol][factor_id] = value
            continue
        raw = {}
        for symbol, row in panel[index].items():
            if kind in ('quality', 'value'):
                if row[kind] is None:
                    raise ValueError(f'{kind} 因子缺少财务输入')
                raw[symbol] = row[kind]
                continue
            end = index - spec['skip']
            start = end - spec['lookback']
            if start < 0:
                raise ValueError('因子历史窗口不足')
            prices = [series_level(panel[i][symbol]) for i in range(start, end + 1)]
            momentum = prices[-1] / prices[0] - 1
            volatility = -pstdev([math.log(b / a) for a, b in zip(prices, prices[1:])])
            if kind == 'momentum':
                raw[symbol] = momentum
            elif kind == 'low_volatility':
                raw[symbol] = volatility
            else:
                if row['quality'] is None or row['value'] is None:
                    raise ValueError('组合因子缺少财务输入')
                raw[symbol] = (momentum, row['quality'], row['value'], volatility)
        for symbol in scores:
            if kind == 'combined':
                ranks = []
                for column, value in enumerate(raw[symbol]):
                    lower = sum(other[column] < value for other in raw.values())
                    equal = sum(other[column] == value for other in raw.values())
                    ranks.append((lower + (equal - 1) / 2) / (len(raw) - 1))
                scores[symbol][factor_id] = mean(ranks)
            else:
                scores[symbol][factor_id] = raw[symbol]
    return scores
