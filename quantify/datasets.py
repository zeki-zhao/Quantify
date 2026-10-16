"""Versioned CSV imports. Source statements are preserved, not independently certified."""
import csv
import hashlib
import io
import math
import re
from datetime import date

from .research import FIELDS as MONTHLY_FIELDS, PRICE_FIELDS, parse_panel

SCHEMAS = {
    'daily': ['date', 'symbol', 'open', 'high', 'low', 'close', 'volume', 'total_return_index', 'available_at'],
    'monthly': MONTHLY_FIELDS,
    'monthly_price': PRICE_FIELDS,
    'financials': ['period_end', 'symbol', 'quality', 'value', 'available_at'],
    'universe': ['date', 'symbol', 'member', 'available_at'],
    'execution': ['date', 'symbol', 'suspended', 'limit_up', 'limit_down', 'no_price_limit', 'available_at'],
}
BASES = ('unadjusted', 'forward_adjusted', 'backward_adjusted', 'total_return_index', 'not_applicable')
MAX_BYTES = 8_000_000


def text(value, label, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label}必须为非空文本，最多 {limit} 字')
    return value.strip()


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期必须为 YYYY-MM-DD')
    return date.fromisoformat(value)


def numeric(value, label, minimum=-1e12, optional=False):
    if value == '' and optional:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label}必须为有限数值') from None
    if not math.isfinite(result) or not minimum <= result <= 1e12:
        raise ValueError(f'{label}数值超出范围')
    return result


def csv_text(kind, rows):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=SCHEMAS[kind], lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def row_key(kind, row):
    if kind == 'financials':
        return row['period_end'], row['symbol'], row['available_at']
    if kind == 'universe':
        return row['date'], row['symbol'], row['available_at']
    return row['date'], row['symbol']


def _parse_rows(kind, raw):
    if kind not in SCHEMAS:
        raise ValueError('不支持的数据类型')
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('CSV 必须为 UTF-8 文本且不超过 8 MB')
    reader = csv.DictReader(io.StringIO(raw.lstrip('\ufeff')), strict=True)
    if reader.fieldnames != SCHEMAS[kind]:
        raise ValueError('CSV 表头及顺序必须为：' + ','.join(SCHEMAS[kind]))
    rows, seen, input_keys = [], set(), []
    for line, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'第 {line} 行列数不一致')
        row = {key: value.strip() for key, value in row.items()}
        symbol = text(row['symbol'], '股票代码', 40)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', symbol):
            raise ValueError(f'第 {line} 行股票代码无效')
        event = day(row.get('date', row.get('period_end')))
        available = day(row['available_at'])
        if kind in ('daily', 'financials') and available < event:
            raise ValueError('可用日期不能早于行情日期或财务报告期')
        if kind in ('monthly', 'monthly_price') and available > event:
            raise ValueError('因子可用日期晚于观察日期（未来数据）')
        if kind == 'daily':
            for field in ('open', 'high', 'low', 'close'):
                row[field] = numeric(row[field], field, .000001)
            row['volume'] = numeric(row['volume'], '成交量（股）', 0)
            row['total_return_index'] = numeric(row['total_return_index'], '含分红指数', .000001, True)
            if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
                raise ValueError('OHLC 高低价格不一致')
        elif kind in ('monthly', 'monthly_price', 'financials'):
            for field in ('quality', 'value'):
                row[field] = numeric(row[field], field, optional=kind in ('monthly', 'monthly_price'))
            if kind == 'monthly':
                row['total_return_index'] = numeric(row['total_return_index'], '含分红指数', .000001)
            elif kind == 'monthly_price':
                row['close'] = numeric(row['close'], '未复权价格', .000001)
        elif kind == 'execution':
            if available > event:
                raise ValueError('执行状态须声明为交易日开盘前已知，available_at不能晚于date')
            for field in ('suspended', 'no_price_limit'):
                if row[field] not in ('0', '1'):
                    raise ValueError(field + '必须为0或1')
                row[field] = int(row[field])
            for field in ('limit_up', 'limit_down'):
                row[field] = numeric(row[field], field, .000001, optional=True)
            if row['no_price_limit']:
                if row['limit_up'] is not None or row['limit_down'] is not None:
                    raise ValueError('明确无价格限制日应留空上下限')
            elif row['limit_up'] is None or row['limit_down'] is None or row['limit_up'] < row['limit_down']:
                raise ValueError('限价日必须提供有效上下限；缺失不能当作无限价')
        else:
            if row['member'] not in ('0', '1'):
                raise ValueError('member 必须为 0（退出）或 1（加入）')
            row['member'] = int(row['member'])
        key = row_key(kind, row)
        if key in seen:
            raise ValueError(f'第 {line} 行包含重复键')
        seen.add(key)
        input_keys.append(key)
        rows.append(row)
        if len(rows) > 100_000:
            raise ValueError('单次导入最多 100000 行')
    if not rows:
        raise ValueError('CSV 不能为空')
    warnings = []
    if input_keys != sorted(input_keys):
        warnings.append('输入未按日期和代码排序，已规范排序；原始 CSV 保留。')
    return sorted(rows, key=lambda row: row_key(kind, row)), warnings


def parse_rows(kind, raw):
    try:
        return _parse_rows(kind, raw)
    except csv.Error:
        raise ValueError("CSV 格式无效或单个字段过长") from None


class CSVDataAdapter:
    """Reference adapter: external services should return the same validated CSV contract."""
    def read(self, kind, raw):
        return parse_rows(kind, raw)


def snapshot_summary(snapshot):
    return {key: value for key, value in snapshot.items() if key not in ('rows', 'raw_import')}


def _prepare_snapshot(store, *, kind, raw_csv, source, price_basis, is_demo, parent_id=None):
    source = text(source, '数据来源与口径说明')
    if type(is_demo) is not bool or price_basis not in BASES:
        raise ValueError('必须声明 is_demo 布尔值和有效价格口径')
    if kind == 'monthly' and price_basis != 'total_return_index':
        raise ValueError('月度研究必须使用含分红再投资指数')
    if kind == 'monthly_price' and price_basis != 'unadjusted':
        raise ValueError('价格研究当前仅支持明确未复权的价格，不能混用复权与总回报')
    if kind in ('financials', 'universe', 'execution') and price_basis != 'not_applicable':
        raise ValueError('财务、股票池和执行状态的价格口径应为 not_applicable')
    if kind == 'daily' and price_basis == 'not_applicable':
        raise ValueError('日线必须声明 OHLC 价格口径')
    rows, warnings = CSVDataAdapter().read(kind, raw_csv)
    metadata = {'kind': kind, 'source': source, 'price_basis': price_basis, 'is_demo': is_demo}
    replaced = 0
    if parent_id:
        parent = store.get('snapshot', parent_id)
        if any(parent[key] != value for key, value in metadata.items()):
            raise ValueError('增量更新必须保持数据类型、来源、价格口径和演示标识一致')
        merged = {row_key(kind, row): row for row in parent['rows']}
        changed = False
        for row in rows:
            key = row_key(kind, row)
            if key in merged and merged[key] != row:
                if kind in ('financials', 'universe'):
                    raise ValueError('同一可用时间的记录冲突；修订应提供新的 available_at')
                replaced += 1
            if merged.get(key) != row:
                changed = True
            merged[key] = row
        if not changed:
            return parent
        rows = [merged[key] for key in sorted(merged)]
    if len(rows) > 100_000:
        raise ValueError('单个快照最多 100000 行，请缩小数据集')
    dates = sorted({row.get('date', row.get('period_end')) for row in rows})
    symbols = sorted({row['symbol'] for row in rows})
    if kind == 'daily':
        warnings += ['未对接交易日历，缺失交易日、停牌和退市覆盖尚未核验。',
                     '含分红指数与复权口径由导入者声明，未核验公司行动。']
        if any(row['total_return_index'] is None for row in rows):
            warnings.append('缺少含分红指数：可保存行情，但不能由原始价格推定总回报。')
    if kind in ('monthly', 'monthly_price'):
        warnings.append('平衡月度面板可能存在幸存者偏差；研究前仍需完整窗口校验。')
    if kind == 'monthly_price':
        warnings.append('未复权价格收益不含现金分红，拆股/送转/配股等跳变未调整；不代表投资总回报。')
    if kind in ('financials', 'universe'):
        warnings.append('available_at 为用户声明的历史可用时间，未向公告源核验。')
    if kind == 'execution':
        warnings.append('停牌和价格上下限为用户声明的开盘前状态；没有核验交易日历、板块或公司行动。')
    if replaced:
        warnings.append(f'本次修订 {replaced} 条历史记录，原版本仍可读取。')
    body = {**metadata, 'schema_version': 1, 'parent_id': parent_id,
            'raw_import': raw_csv, 'raw_sha256': hashlib.sha256(raw_csv.encode()).hexdigest(),
            'rows': rows, 'row_count': len(rows), 'symbols': symbols, 'date_range': [dates[0], dates[-1]],
            'warnings': warnings, 'units': {'prices': 'CNY/share or declared index', 'volume': 'shares',
                                           'total_return_index': 'dimensionless; dividends reinvested'}}
    if kind == 'monthly_price':
        body['units'].pop('total_return_index')
        body['units']['close'] = 'CNY/share; unadjusted; cash dividends excluded'
    return body


def import_snapshot(store, **kwargs):
    body = _prepare_snapshot(store, **kwargs)
    return body if 'id' in body else store.put('snapshot', body)


def as_of_rows(snapshot, cutoff):
    """Latest known revision of each period/event; never uses later announcements."""
    day(cutoff)
    latest = {}
    kind = snapshot['kind']
    for row in snapshot['rows']:
        event = row.get('date', row.get('period_end'))
        if event > cutoff or row['available_at'] > cutoff:
            continue
        if kind == 'financials' and row['available_at'] == cutoff:
            continue  # Date-only announcements are conservatively usable after publication day.
        key = (event, row['symbol'])
        if key not in latest or row['available_at'] > latest[key]['available_at']:
            latest[key] = row
    return [latest[key] for key in sorted(latest)]


def monthly_from_daily(store, *, daily_id, financials_id=None, universe_id=None, return_basis='total_return'):
    if return_basis not in ('total_return', 'price'):
        raise ValueError('收益口径无效')
    kind = 'monthly_price' if return_basis == 'price' else 'monthly'
    level_key = 'close' if return_basis == 'price' else 'total_return_index'
    daily = store.get('snapshot', daily_id)
    if daily['kind'] != 'daily':
        raise ValueError('需要日线快照')
    if return_basis == 'price' and daily['price_basis'] != 'unadjusted':
        raise ValueError('价格收益研究必须选择未复权日线')
    financials = store.get('snapshot', financials_id) if financials_id else None
    universe = store.get('snapshot', universe_id) if universe_id else None
    for obj, expected_kind in ((financials, 'financials'), (universe, 'universe')):
        if obj and (obj['kind'] != expected_kind or obj['is_demo'] != daily['is_demo']):
            raise ValueError('关联快照类型或演示标识不一致')
    # Drop the final calendar month: no exchange calendar is available to certify its completion.
    by_month = {}
    for row in daily['rows']:
        by_month.setdefault(row['date'][:7], []).append(row)
    months = sorted(by_month)[:-1]
    output = []
    previous_members = None
    for month in months:
        rows = by_month[month]
        cutoff = max(row['date'] for row in rows)
        observed = {row['symbol']: row for row in rows if row['date'] == cutoff}
        members = set(daily['symbols'])
        if universe:
            state = {}
            for row in as_of_rows(universe, cutoff):
                state[row['symbol']] = row['member']
            members = {symbol for symbol, member in state.items() if member}
            if previous_members is not None and members != previous_members:
                raise ValueError('历史股票池发生变化：当前研究引擎尚不支持动态池，不会静默删股')
        if not members or not members <= set(observed):
            raise ValueError(f'{month} 月末观测不齐全，不会使用过期价格或静默删股')
        previous_members = members
        known = {}
        if financials:
            for row in as_of_rows(financials, cutoff):
                known[row['symbol']] = row
        for symbol in sorted(members):
            row = observed[symbol]
            if row['available_at'] > cutoff or row[level_key] is None:
                raise ValueError('月末行情尚不可用或缺少含分红指数')
            finance = known.get(symbol)
            if financials and finance is None:
                raise ValueError(f'{symbol} 在 {cutoff} 无已知财务因子')
            output.append({'date': cutoff, 'symbol': symbol, level_key: row[level_key],
                           'quality': finance['quality'] if finance else '',
                           'value': finance['value'] if finance else '',
                           'available_at': finance['available_at'] if finance else cutoff})
    raw = csv_text(kind, output)
    parse_panel(raw, required_fields=(), return_basis=return_basis)
    result = _prepare_snapshot(store, kind=kind, raw_csv=raw,
                             source='日线转月度；取各月最后观测日；舍弃最后日历月；来源：' + daily['source'][:200],
                             price_basis='unadjusted' if return_basis == 'price' else 'total_return_index', is_demo=daily['is_demo'])
    # Derived lineage is an additional immutable object; do not mutate the imported snapshot.
    body = {key: value for key, value in result.items() if key not in ('id', 'created_at')}
    body['derived_from'] = {'daily': daily_id, 'financials': financials_id, 'universe': universe_id}
    body['warnings'] = list(dict.fromkeys(body['warnings'] + daily['warnings'] +
                                         (financials['warnings'] if financials else [])))
    return store.put('snapshot', body)
