"""Decimal cash/share ledger, fixed orders and conservative daily-open execution."""
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .core import integer, number
from .factors import calculate

CENT = Decimal('.01')
ZERO = Decimal(0)


def dec(value):
    return Decimal(str(value))


def money(value):
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def price(value):
    original = dec(value)
    rounded = money(original)
    if rounded <= 0 or abs(original - rounded) > Decimal('.00000001'):
        raise ValueError('组合只支持0.01元价格单位的未复权股票，不能将指数或复权价当作成交价')
    return rounded


def parameters(value=None):
    default = dict(initial_cash=100000., top_k=2, rebalance_months=3, cash_buffer=.02,
                   commission_bps=3., min_commission=5., stamp_tax_bps=5., transfer_fee_bps=.1, slippage_bps=5.)
    if value is not None and (not isinstance(value, dict) or set(value)-set(default)):
        raise ValueError('组合参数字段无效')
    p={**default, **(value or {})}
    p['initial_cash']=float(money(number(p['initial_cash'],'初始资金',100,1000000000)))
    p['top_k']=integer(p['top_k'],'选股数',1,99)
    p['rebalance_months']=integer(p['rebalance_months'],'调仓月数',1,12)
    if p['rebalance_months'] not in (1,3,6,12):raise ValueError('调仓周期须为1/3/6/12月')
    p['cash_buffer']=number(p['cash_buffer'],'目标现金比例',0,.9)
    for key in ('commission_bps','stamp_tax_bps','transfer_fee_bps','slippage_bps'):
        p[key]=number(p[key],key,0,1000)
    p['min_commission']=float(money(number(p['min_commission'],'最低佣金',0,10000)))
    return p


class Account:
    def __init__(self, config):
        self.config=config
        self.initial=money(config['initial_cash']);self.cash=self.initial
        self.lots=defaultdict(list);self.realized=ZERO;self.fees=ZERO;self.slippage=ZERO
        self.trades=[];self.orders=[];self.curve=[];self.holdings_history=[]
        self.peak=self.initial;self.max_drawdown=ZERO;self.turnover_notional=ZERO

    def quantity(self, symbol):
        return sum(lot['quantity'] for lot in self.lots[symbol])

    def fee(self, side, gross):
        p=self.config
        commission=max(money(p['min_commission']),money(gross*dec(p['commission_bps'])/10000))
        transfer=money(gross*dec(p['transfer_fee_bps'])/10000)
        tax=money(gross*dec(p['stamp_tax_bps'])/10000) if side=='sell' else ZERO
        return commission,transfer,tax

    def equity(self, marks):
        return self.cash+sum((self.quantity(s)*price(marks[s]) for s in self.lots),ZERO)

    def holdings(self, marks):
        result=[]
        for symbol in sorted(self.lots):
            quantity=self.quantity(symbol)
            if not quantity:continue
            basis=sum((lot['basis'] for lot in self.lots[symbol]),ZERO)
            value=quantity*price(marks[symbol])
            result.append(dict(symbol=symbol,quantity=quantity,cost_basis=float(basis),market_value=float(value),
                               unrealized_pnl=float(value-basis),lots=[dict(quantity=lot['quantity'],cost_basis=float(lot['basis']),
                                      acquired_at=lot['date']) for lot in self.lots[symbol]]))
        return result

    def fill(self, order, stamp, bar, state):
        side,symbol,requested=order['side'],order['symbol'],order['quantity']
        record={**order,'execution_date':stamp,'filled_quantity':0,'status':'rejected'}
        def rejected(reason):
            record['reason']=reason;self.orders.append(record);return record
        if state['suspended']:return rejected('suspended')
        opened=price(bar['open'])
        slip=dec(self.config['slippage_bps'])/10000
        executed=(opened*(1+slip if side=='buy' else 1-slip)).quantize(CENT,rounding=ROUND_CEILING if side=='buy' else ROUND_FLOOR)
        if executed<=0:return rejected('invalid_execution_price')
        if not state['no_price_limit']:
            upper,lower=price(state['limit_up']),price(state['limit_down'])
            if side=='buy' and (opened>=upper or executed>=upper):return rejected('limit_up')
            if side=='sell' and (opened<=lower or executed<=lower):return rejected('limit_down')
        quantity=requested
        if type(quantity) is not int or quantity<=0 or quantity%100:raise ValueError('订单股数必须为正的100股整数手')
        if side=='sell':
            available=sum(lot['quantity'] for lot in self.lots[symbol] if lot['date']<stamp)
            quantity=min(quantity,available)//100*100
            if quantity==0:return rejected('t_plus_one_or_no_position')
        elif side=='buy':
            # Orders were sized at signal close; gaps only reduce fills, never enlarge orders.
            low,high=0,quantity//100
            while low<high:
                mid=(low+high+1)//2
                gross=executed*mid*100
                if gross+sum(self.fee('buy',gross))<=self.cash:low=mid
                else:high=mid-1
            quantity=low*100
            if quantity==0:return rejected('insufficient_cash')
        else:raise ValueError('订单方向无效')
        gross=executed*quantity
        commission,transfer,tax=self.fee(side,gross);fees=commission+transfer+tax
        if side=='sell' and self.cash+gross<fees:return rejected('insufficient_cash_for_sell_fees')
        before=self.cash;realized=ZERO
        if side=='buy':
            self.cash-=gross+fees
            self.lots[symbol].append(dict(quantity=quantity,basis=gross+fees,date=stamp))
        else:
            remaining=quantity;basis=ZERO
            for lot in self.lots[symbol]:
                if lot['date']>=stamp or remaining==0:continue
                used=min(remaining,lot['quantity'])
                allocated=lot['basis'] if used==lot['quantity'] else money(lot['basis']*used/lot['quantity'])
                lot['quantity']-=used;lot['basis']-=allocated;basis+=allocated;remaining-=used
            self.lots[symbol]=[lot for lot in self.lots[symbol] if lot['quantity']]
            realized=gross-fees-basis;self.realized+=realized;self.cash+=gross-fees
        self.fees+=fees;self.slippage+=abs(executed-opened)*quantity;self.turnover_notional+=gross
        if self.cash<0:raise ValueError('现金账本透支，停止计算')
        self.trades.append(dict(date=stamp,signal_date=order['signal_date'],symbol=symbol,side=side,quantity=quantity,
                                open_price=float(opened),price=float(executed),gross=float(gross),commission=float(commission),
                                transfer_fee=float(transfer),stamp_tax=float(tax),fees=float(fees),realized_pnl=float(realized),
                                cash_before=float(before),cash_after=float(self.cash)))
        record.update(status='filled' if quantity==requested else 'partial',filled_quantity=quantity,
                      reason='' if quantity==requested else 'cash_or_t_plus_one_remainder_cancelled')
        self.orders.append(record)
        return record

    def mark(self, stamp, marks):
        position_value=sum((self.quantity(s)*price(marks[s]) for s in self.lots),ZERO)
        equity=self.cash+position_value
        basis=sum((lot['basis'] for lots in self.lots.values() for lot in lots),ZERO)
        unrealized=position_value-basis
        if equity!=self.initial+self.realized+unrealized:raise ValueError('资金与盈亏对账不一致')
        self.peak=max(self.peak,equity)
        drawdown=equity/self.peak-1
        self.max_drawdown=min(self.max_drawdown,drawdown)
        point=dict(date=stamp,cash=float(self.cash),position_value=float(position_value),equity=float(equity),
                   nav=float(equity/self.initial),drawdown=float(drawdown),realized_pnl=float(self.realized),
                   unrealized_pnl=float(unrealized),fees=float(self.fees))
        self.curve.append(point)
        return point

    def report(self, marks):
        end=self.curve[-1]
        mean_equity=sum((dec(point['equity']) for point in self.curve),ZERO)/len(self.curve)
        return dict(summary=dict(initial_cash=float(self.initial),ending_cash=end['cash'],ending_equity=end['equity'],
                                 total_return=end['nav']-1,max_drawdown=float(self.max_drawdown),fees=float(self.fees),
                                 slippage_cost=float(self.slippage),trade_count=len(self.trades),
                                 rejected_orders=sum(o['status']=='rejected' for o in self.orders),
                                 partial_orders=sum(o['status']=='partial' for o in self.orders),
                                 realized_pnl=end['realized_pnl'],unrealized_pnl=end['unrealized_pnl'],
                                 gross_turnover=float(self.turnover_notional/mean_equity),
                                 note='gross_turnover=买卖成交总额/观察日平均权益，双边累计、不年化；终点持仓未强平。'),
                    curve=self.curve,trades=self.trades,orders=self.orders,holdings=self.holdings(marks),
                    holdings_history=self.holdings_history)


def simulate(*, dates, panel, daily, states, factor, config, signal_dates, checkpoint=None):
    """No fitting here. Freeze quantities at monthly signal close, fill next daily open."""
    selected,baseline=Account(config),Account(config)
    signal_indices={stamp.isoformat():i for i,stamp in enumerate(dates) if stamp.isoformat() in signal_dates}
    days=sorted(daily)
    if not days:raise ValueError('无可回放的日线')
    pending=[[],[]];decisions=[]
    for step,stamp in enumerate(days):
        if checkpoint:checkpoint(dict(processed_days=step,total_days=len(days),date=stamp))
        bars=daily[stamp];marks={s:row['close'] for s,row in bars.items()}
        for account,orders in zip((selected,baseline),pending):
            for order in sorted(orders,key=lambda o:(o['side']!='sell',o['symbol'])):
                account.fill(order,stamp,bars[order['symbol']],states[stamp][order['symbol']])
            account.mark(stamp,marks)
            if orders:account.holdings_history.append(dict(date=stamp,holdings=account.holdings(marks)))
        pending=[[],[]]
        if stamp in signal_indices:
            index=signal_indices[stamp]
            scores=calculate(panel,index,[factor])
            ranked=sorted(panel[index],key=lambda s:(-scores[s][factor['id']],s))[:config['top_k']]
            decisions.append(dict(date=stamp,factor_id=factor['id'],selected=ranked,
                                  scores={s:scores[s][factor['id']] for s in sorted(panel[index])},
                                  executed_after=stamp))
            for account,chosen,orders in zip((selected,baseline),(ranked,sorted(panel[index])),pending):
                capital=account.equity(marks)*(1-dec(config['cash_buffer']))
                targets={s:int(capital/len(chosen)/price(marks[s])/100)*100 for s in chosen}
                for symbol in sorted(set(account.lots)|set(targets)):
                    delta=targets.get(symbol,0)-account.quantity(symbol)
                    if delta:
                        orders.append(dict(signal_date=stamp,symbol=symbol,side='buy' if delta>0 else 'sell',quantity=abs(delta),
                                           target_quantity=targets.get(symbol,0),sizing_price=float(price(marks[symbol])),
                                           signal_equity=float(account.equity(marks))))
    for account,orders in zip((selected,baseline),pending):
        for order in orders:account.orders.append({**order,'execution_date':None,'filled_quantity':0,'status':'not_executed','reason':'no_next_bar'})
    if checkpoint:checkpoint(dict(processed_days=len(days),total_days=len(days),date=days[-1]))
    return dict(selected=selected.report(marks),baseline=baseline.report(marks),decisions=decisions)
