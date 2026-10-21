"""Small parameterized factor registry. Definitions are data, never executable strings."""
import math
from fractions import Fraction
from statistics import mean, pstdev
from .expressions import validate_expression, evaluate_expression
from .core import number

KINDS = ('momentum', 'quality', 'value', 'low_volatility', 'combined')
NAMES = {'momentum': '动量', 'quality': '盈利质量', 'value': '价值',
         'low_volatility': '低波动', 'combined': '四因子等权排名'}
BUILTINS = tuple({'id': kind, 'key': kind, 'kind': kind, 'lookback': 6, 'skip': 0,
                  'name': NAMES[kind], 'reason': '内置基线', 'parent_id': None}
                 for kind in KINDS)


def validate_spec(spec):
    if spec.get('kind') not in (*KINDS, 'expression', 'blend'):
        raise ValueError('不支持的因子类型')
    if spec['kind'] == 'blend':
        components = spec.get('components')
        if not isinstance(components, list) or not 2 <= len(components) <= 10:
            raise ValueError('多因子组合需要2至10个成分')
        ids = []
        for component in components:
            if not isinstance(component, dict) or set(component) != {'factor', 'weight', 'ratio'}:
                raise ValueError('多因子成分格式无效')
            child = component['factor']
            if not isinstance(child, dict) or child.get('kind') == 'blend':
                raise ValueError('成分必须为非嵌套因子版本')
            validate_spec(child)
            ids.append(child['id'])
            number(component['weight'], '成分权重', 0, 1000000)
            ratio = component['ratio']
            if not isinstance(ratio, list) or len(ratio) != 2 or any(type(v) is not int or v <= 0 for v in ratio):
                raise ValueError('成分精确权重无效')
            if component['weight'] <= 0 or float(Fraction(*ratio)) != component['weight']:
                raise ValueError('成分显示权重与精确权重不一致')
        if len(set(ids)) != len(ids):
            raise ValueError('不能重复引用同一因子版本')
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
    elif parent['kind'] == 'blend':
        if (lookback, skip) != (6, 0):
            raise ValueError('多因子组合窗口由成分版本决定；请通过多因子入口更改权重')
        spec['components'] = parent['components']
    validate_spec(spec)
    return store.put('factor', spec)


def create_blend(store, *, components, name, reason, parent_id=None):
    """Bind immutable leaf versions; canonical ordering makes ties input-order independent."""
    if not isinstance(components, list) or not 2 <= len(components) <= 10:
        raise ValueError('多因子组合需要2至10个成分')
    bound = []
    for item in components:
        if not isinstance(item, dict) or set(item) != {'factor_id', 'weight'}:
            raise ValueError('成分需要且只能包含factor_id、weight')
        child = store.get('factor', item['factor_id'])
        weight = number(item['weight'], '成分权重', .000001, 1000000)
        bound.append((child, Fraction(str(weight))))
    return _save_blend(store, bound, name, reason, parent_id)


def _save_blend(store, bound, name, reason, parent_id):
    if parent_id is not None:
        store.get('factor', parent_id)
    components = [dict(factor=child, weight=float(weight), ratio=[weight.numerator, weight.denominator]) for child, weight in bound]
    spec = dict(kind='blend', key='blend', name=name, reason=reason, parent_id=parent_id,
                lookback=6, skip=0, components=sorted(components, key=lambda c: c['factor']['id']))
    validate_spec(spec)
    return store.put('factor', spec)


def constituents(spec):
    """Return flattened version references with normalized positive weights."""
    if spec['kind'] != 'blend':
        return [dict(factor=spec, weight=1., ratio=[1, 1])]
    weights = [Fraction(*c['ratio']) for c in spec['components']]
    total = sum(weights)
    return [dict(factor=c['factor'], weight=float(w/total), ratio=[(w/total).numerator, (w/total).denominator])
            for c, w in zip(spec['components'], weights)]


def augment(store, base, candidate, weight):
    weight = number(weight, '候选权重', .01, .99)
    if candidate['kind'] == 'blend':
        raise ValueError('新增候选必须是非组合因子')
    share = Fraction(str(weight))
    bound = [(c['factor'], Fraction(*c['ratio'])*(1-share)) for c in constituents(base)]
    bound.append((candidate, share))
    return _save_blend(store, bound, name='增量 · '+candidate['name'][:80],
                       reason='固定原组合加入候选；仅开发期研究', parent_id=base['id'])


def required_fields(specs):
    needed = set()
    for spec in specs:
        if spec['kind'] == 'blend':
            needed.update(required_fields([c['factor'] for c in spec['components']]))
        elif spec['kind'] in ('quality', 'value'):
            needed.add(spec['kind'])
        elif spec['kind'] == 'expression':
            needed.update(validate_expression(spec['expression'])['required_fields'])
        elif spec['kind'] == 'combined':
            needed.update(('quality', 'value'))
    return needed


def history_length(specs):
    return max(history_length([c['factor'] for c in spec['components']]) if spec['kind'] == 'blend' else
               validate_expression(spec['expression'])['history_months'] if spec['kind'] == 'expression'
               else (spec['lookback'] + spec['skip'] if spec['kind'] not in ('quality', 'value') else 0)
               for spec in specs)


def series_level(row):
    """Use the explicitly parsed series without aliasing prices as a total-return index."""
    return row['close'] if 'close' in row else row['total_return_index']


def calculate(panel, index, specs):
    scores = {symbol: {} for symbol in panel[index]}
    for spec in specs:
        kind, factor_id = spec['kind'], spec['id']
        if kind == 'blend':
            children = calculate(panel, index, [c['factor'] for c in spec['components']])
            ranked = {}
            for component in constituents(spec):
                child_id = component['factor']['id']
                values = {s: children[s][child_id] for s in scores}
                if any(not math.isfinite(v) for v in values.values()):
                    raise ValueError('成分含非有限分值')
                ranked[child_id] = {s: Fraction(2*sum(v < values[s] for v in values.values()) +
                                               sum(v == values[s] for v in values.values()) - 1, 2*(len(values)-1))
                                    for s in values}
            for symbol in scores:
                scores[symbol][factor_id] = float(sum(Fraction(*c['ratio'])*ranked[c['factor']['id']][symbol] for c in constituents(spec)))
            continue
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
