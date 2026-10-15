"""Read-only online diagnostic: python -m quantify.market_probe --symbol 600519.SH."""
import argparse
import json
from datetime import timedelta

from .market import closed_day
from .market_providers import SinaProvider, EastmoneyProvider, local_now, normalize_symbol, ProviderError


def main():
    parser = argparse.ArgumentParser(description='只读检验新浪报价与东财日线，不写入数据库')
    parser.add_argument('--symbol', default='600519.SH')
    parser.add_argument('--start', default=(local_now().date()-timedelta(days=14)).isoformat())
    parser.add_argument('--end', default=closed_day(local_now()))
    args = parser.parse_args()
    symbol = normalize_symbol(args.symbol)
    results = {}
    for name, call in (
            ('sina', lambda: SinaProvider().quotes([symbol])),
            ('eastmoney', lambda: EastmoneyProvider().history(symbol, args.start, args.end))):
        try:
            response = call()
            if name == 'sina':
                if not response['quotes']:
                    raise ProviderError('response', response['errors'].get(symbol, '无有效报价'))
                quote = response['quotes'][symbol]
                results[name] = {'ok': True, 'symbol': symbol, 'quote_at': quote['quote_at'],
                                 'received_at': quote['received_at'], 'price': quote['price']}
            else:
                results[name] = {'ok': True, 'symbol': symbol, 'rows': len(response['rows']),
                                 'date_range': [response['rows'][0]['date'], response['rows'][-1]['date']],
                                 'price_basis': 'unadjusted'}
        except (ValueError, OSError) as exc:
            results[name] = {'ok': False, 'category': getattr(exc, 'category', type(exc).__name__), 'error': str(exc)}
    print(json.dumps({'checked_at': local_now().isoformat(), 'results': results}, ensure_ascii=False, indent=2))
    return 0 if all(result['ok'] for result in results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
