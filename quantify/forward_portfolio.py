"""Replay only recorded forward decisions: immutable quantities and real creation dates."""
from .portfolio import Account, dec, price
from .portfolio_policy import summarize


def orders_for(account, chosen, marks, config, signal_date):
    capital = account.equity(marks)*(1-dec(config['cash_buffer']))
    targets = {s:int(capital/len(chosen)/price(marks[s])/100)*100 for s in chosen}
    orders=[]
    for symbol in sorted(set(account.lots)|set(targets)):
        difference=targets.get(symbol,0)-account.quantity(symbol)
        if difference:
            orders.append(dict(signal_date=signal_date,symbol=symbol,side='buy' if difference>0 else 'sell',
                               quantity=abs(difference),target_quantity=targets.get(symbol,0),
                               sizing_price=float(price(marks[symbol])),signal_equity=float(account.equity(marks))))
    return orders


def replay(*, signals, daily, states, config, reference_config, checkpoint=None):
    accounts = dict(selected=Account(config),reference=Account(reference_config))
    if not signals:
        return accounts,None
    first=min(s['valuation_date'] for s in signals)
    days=sorted(d for d in daily if d>=first)
    executed=set()
    for step,stamp in enumerate(days):
        if checkpoint:checkpoint(dict(stage='forward_ledger',processed_days=step,total_days=len(days)))
        eligible=[s for s in signals if s['id'] not in executed and s['generation_date']<stamp]
        for signal in sorted(eligible,key=lambda s:(s['generated_at'],s['id'])):
            for key,account in accounts.items():
                for order in sorted(signal['orders'][key],key=lambda o:(o['side']!='sell',o['symbol'])):
                    account.fill(order,stamp,daily[stamp][order['symbol']],states[stamp][order['symbol']])
            executed.add(signal['id'])
        marks={s:r['close'] for s,r in daily[stamp].items()}
        for account in accounts.values():account.mark(stamp,marks)
    if not days:raise ValueError('前向信号的估值日无完整日线')
    report={key:account.report(marks) for key,account in accounts.items()}
    pending=[dict(signal_id=s['id'],generation_date=s['generation_date'],orders=s['orders']) for s in signals if s['id'] not in executed]
    boundaries=[s['valuation_date'] for s in sorted(signals,key=lambda s:(s['generated_at'],s['id'])) if s['selected_due']]
    if boundaries!=sorted(set(boundaries)):
        raise ValueError('前向调仓端点不递增，拒绝回填或重算历史')
    selected,periods=summarize(report['selected'],boundaries)
    reference,ref_periods=summarize(report['reference'],boundaries)
    matured=[dict(start=a['start'],end=a['end'],selected_return=a['net_return'],reference_return=b['net_return']) for a,b in zip(periods,ref_periods)]
    return accounts,dict(**report,selected_metrics=selected,reference_metrics=reference,
                         periods=matured,pending_orders=pending,executed_signal_ids=sorted(executed))


def observation(report,objective):
    if report is None:return dict(state='waiting_signal',degradation=False,mature_periods=0)
    selected,reference=report['selected_metrics'],report['reference_metrics']
    enough=(min(selected['completed_periods'],reference['completed_periods'])>=objective['min_periods'] and
            min(selected['observed_days'],reference['observed_days'])>=objective['min_observed_days'] and
            min(selected['trade_count'],reference['trade_count'])>=objective['min_trades'])
    reasons=[]
    if enough:
        primary=objective['primary']
        for failed,reason in [
            (selected[primary]<reference[primary]-1e-12,'前向主目标低于同期参照'),
            (selected['total_return']<reference['total_return']-objective['max_return_deterioration']-1e-12,'前向累计收益下降超限'),
            (selected['max_drawdown']<reference['max_drawdown']-objective['max_drawdown_deterioration']-1e-12,'前向回撤恶化超限'),
            (selected['worst_period_return']<reference['worst_period_return']-objective['max_worst_period_deterioration']-1e-12,'前向最差周期恶化超限')]:
            if failed:reasons.append(reason)
    return dict(state='degraded' if reasons else 'observing' if enough else 'insufficient_mature_labels',
                degradation=bool(reasons),reasons=reasons,mature_periods=len(report['periods']),
                note='与同期固定参照比较；同分不算退化，样本不足不告警；提示不自动更替或交易。')
