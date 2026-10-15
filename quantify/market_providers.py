"""Read-only public-web adapters. No credentials, arbitrary endpoints or executable responses."""
import hashlib
import json
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Protocol

from .datasets import day, numeric, parse_rows, csv_text

CHINA = timezone(timedelta(hours=8))
SINA_URL = 'https://hq.sinajs.cn/'
EASTMONEY_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
HISTORY_SOURCE = '东方财富公开日线网页接口；未复权元/股，成交量转换为股；不含现金分红；公司行动未调整'


def local_now():
    return datetime.now(CHINA)


def normalize_symbol(value):
    if not isinstance(value, str):
        raise ValueError('股票代码必须为文本')
    value = value.strip().upper()
    match = re.fullmatch(r'(\d{6})\.(SH|SZ|BJ)', value)
    if match:
        code, exchange = match.groups()
    elif re.fullmatch(r'(SH|SZ|BJ)\d{6}', value):
        exchange, code = value[:2], value[2:]
    elif re.fullmatch(r'\d{6}', value):
        code = value
        exchange = 'SH' if code.startswith('6') else 'SZ' if code.startswith(('0', '3')) else 'BJ'
    else:
        raise ValueError('请填写 A 股代码，例如 600519.SH 或 sz000001')
    prefixes = {'SH': ('6',), 'SZ': ('0', '3'), 'BJ': ('4', '8', '9')}
    if not code.startswith(prefixes[exchange]):
        raise ValueError('代码与交易所不匹配，当前仅支持沪深北 A 股')
    return code + '.' + exchange


def symbols_list(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 30:
        raise ValueError('请选择 1 至 30 只沪深北 A 股')
    symbols = [normalize_symbol(value) for value in values]
    if len(set(symbols)) != len(symbols):
        raise ValueError('股票代码不能重复')
    return sorted(symbols)


class ProviderError(ValueError):
    def __init__(self, category, message):
        self.category = category
        super().__init__(message)


class QuoteProvider(Protocol):
    def quotes(self, symbols: list) -> dict: ...


class HistoryProvider(Protocol):
    def history(self, symbol: str, start_date: str, end_date: str) -> dict: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicHTTP:
    """Fixed HTTPS destinations, verified TLS, bounded response/time/retry; respects proxy settings."""
    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def get(self, url, headers):
        if url.split('?', 1)[0] not in (SINA_URL, EASTMONEY_URL):
            raise ProviderError('endpoint', '不支持的行情地址')
        for attempt in range(2):
            try:
                request = Request(url, headers={'User-Agent': 'Mozilla/5.0', **headers})
                with self.opener.open(request, timeout=6) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ProviderError('response', '行情响应超过 2 MB，已拒绝')
                return raw
            except HTTPError as exc:
                if attempt == 0 and (exc.code == 429 or 500 <= exc.code < 600):
                    time.sleep(.5); continue
                raise ProviderError('http', f'行情服务器返回 HTTP {exc.code}；未更新数据') from None
            except (TimeoutError, socket.timeout):
                if attempt == 0:
                    time.sleep(.2); continue
                raise ProviderError('timeout', '行情请求超时；未更新数据') from None
            except URLError as exc:
                reason = str(exc.reason)
                if 'Tunnel connection failed' in reason:
                    raise ProviderError('network_policy', '网络代理拒绝建立连接；尚未连接到行情服务器，这不是额度判断') from None
                if attempt == 0:
                    time.sleep(.2); continue
                raise ProviderError('network', '无法连接行情服务器，请检查网络、DNS 或代理') from None
            except OSError:
                if attempt == 0:
                    time.sleep(.2); continue
                raise ProviderError('network', '行情连接中断；未更新数据') from None
        raise ProviderError('network', '行情请求失败')


def parse_sina(raw, requested, received_at):
    requested = symbols_list(requested)
    try:
        text = raw.decode('gbk')
    except UnicodeError:
        raise ProviderError('response', '新浪响应不是有效 GBK 文本') from None
    pattern = re.compile(r'var\s+hq_str_((?:sh|sz|bj)\d{6})="([^"\r\n]*)"\s*;')
    entries, position = {}, 0
    for match in pattern.finditer(text):
        if text[position:match.start()].strip():
            raise ProviderError('response', '新浪响应包含未支持的内容；不执行返回脚本')
        position = match.end()
        symbol = normalize_symbol(match[1])
        if symbol in entries or symbol not in requested:
            raise ProviderError('response', '新浪响应包含重复或未请求的代码')
        entries[symbol] = match[2]
    if text[position:].strip() or not entries:
        raise ProviderError('response', '新浪响应格式无效；不执行返回脚本')
    quotes, errors = {}, {}
    for symbol in requested:
        try:
            values = entries.get(symbol, '').split(',')
            if len(values) < 32 or not values[0].strip():
                raise ValueError('未返回完整行情，可能暂无报价或代码不可用')
            quote_at = datetime.fromisoformat(values[30] + 'T' + values[31]).replace(tzinfo=CHINA)
            if quote_at > received_at + timedelta(seconds=5):
                raise ValueError('行情时间晚于接收时间，请检查时钟或响应')
            quote = dict(symbol=symbol, name=values[0], source='sina', price_basis='unadjusted',
                         quote_at=quote_at.isoformat(), received_at=received_at.isoformat(),
                         is_demo=False, unit='CNY/share', volume_unit='shares')
            for name, index, lower in (('price', 3, .000001), ('open', 1, 0), ('previous_close', 2, .000001),
                                       ('high', 4, 0), ('low', 5, 0), ('volume', 8, 0), ('amount', 9, 0)):
                quote[name] = numeric(values[index], name, lower)
            if not quote['low'] <= quote['price'] <= quote['high'] or quote['open'] <= 0:
                raise ValueError('暂无有效成交价格，可能停牌；不将零填为有效行情')
            quote['change_percent'] = (quote['price'] / quote['previous_close'] - 1) * 100
            quotes[symbol] = quote
        except (ValueError, TypeError) as exc:
            errors[symbol] = str(exc)
    return {'quotes': quotes, 'errors': errors, 'received_at': received_at.isoformat(),
            'raw_sha256': hashlib.sha256(raw).hexdigest()}


def parse_eastmoney(raw, symbol, start_date, end_date):
    symbol = normalize_symbol(symbol)
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise ProviderError('response', '东财响应不是有效 JSON') from None
    if not isinstance(data, dict) or data.get('rc') != 0:
        raise ProviderError('response', '东财返回错误状态')
    body = data.get('data')
    if not isinstance(body, dict) or str(body.get('code')) != symbol[:6]:
        raise ProviderError('response', '东财未返回指定代码，拒绝错配或空响应')
    expected_market = 1 if symbol.endswith('.SH') else 0
    if body.get('market') != expected_market:
        raise ProviderError('response', '东财返回的交易所与请求不一致')
    records = body.get('klines')
    if not isinstance(records, list) or not records:
        raise ProviderError('no_data', f'{symbol} 在所选区间无历史数据；不会删除该代码继续导入')
    rows = []
    seen = set()
    for record in records:
        if not isinstance(record, str):
            raise ProviderError('response', '东财 K 线字段格式无效')
        values = record.split(',')
        if len(values) < 7:
            raise ProviderError('response', '东财 K 线字段不足')
        stamp = values[0]
        day(stamp)
        if stamp in seen or not start_date <= stamp <= end_date:
            raise ProviderError('response', '东财返回重复或区间外日期，拒绝静默截断')
        seen.add(stamp)
        rows.append(dict(date=stamp, symbol=symbol, open=values[1], close=values[2], high=values[3],
                         low=values[4], volume=numeric(values[5], '成交量（手）', 0) * 100,
                         total_return_index='', available_at=stamp))
    rows, warnings = parse_rows('daily', csv_text('daily', rows))
    return {'rows': rows, 'warnings': warnings, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'source': HISTORY_SOURCE, 'symbol': symbol}


class SinaProvider:
    def __init__(self, transport=None, clock=local_now):
        self.transport, self.clock = transport or PublicHTTP(), clock

    def quotes(self, symbols):
        symbols = symbols_list(symbols)
        codes = [symbol[-2:].lower() + symbol[:6] for symbol in symbols]
        raw = self.transport.get(SINA_URL + '?' + urlencode({'list': ','.join(codes)}),
                                 {'Referer': 'http://finance.sina.com.cn/'})
        return parse_sina(raw, symbols, self.clock())


class EastmoneyProvider:
    def __init__(self, transport=None):
        self.transport = transport or PublicHTTP()

    def history(self, symbol, start_date, end_date):
        symbol = normalize_symbol(symbol)
        params = dict(ut='7eea3edcaed734bea9cbfc24409ed989', secid=('1.' if symbol.endswith('.SH') else '0.') + symbol[:6], klt=101, fqt=0,
                      beg=day(start_date).strftime('%Y%m%d'), end=day(end_date).strftime('%Y%m%d'), lmt=10000,
                      fields1='f1,f2,f3,f4,f5,f6', fields2='f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61')
        raw = self.transport.get(EASTMONEY_URL + '?' + urlencode(params), {'Referer': 'https://quote.eastmoney.com/'})
        return parse_eastmoney(raw, symbol, start_date, end_date)
