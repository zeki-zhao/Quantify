"""Version-bound exploratory portfolio replay; never reads final-holdout returns."""
import random
import re
import uuid
from datetime import date, timedelta

from . import datasets, factors
from .core import integer
from .portfolio import parameters, price, simulate
from .research import parse_panel
from .workbench import engine_fingerprint


class PortfolioService:
    def __init__(self, workbench, jobs=None):
        self.workbench,self.store,self.jobs=workbench,workbench.store,jobs

    def create_spec(self, *, experiment_id, factor_id, daily_id, execution_id, name='组合研究版本', config=None, parent_id=None):
        name=datasets.text(name,'组合名称',100)
        experiment=self.store.get('experiment',experiment_id)
        if experiment['status']!='completed' or experiment['engine']!=engine_fingerprint():
            raise ValueError('需要当前引擎成功完成的开发实验')
        if factor_id not in experiment['request']['factor_ids']:raise ValueError('因子不属于所选开发实验')
        monthly=self.store.get('snapshot',experiment['request']['snapshot_id'])
        daily=self.store.get('snapshot',daily_id);states=self.store.get('snapshot',execution_id)
        if monthly['kind']!='monthly_price' or daily['kind']!='daily' or daily['price_basis']!='unadjusted' or states['kind']!='execution':
            raise ValueError('组合需要月度价格面板、未复权日线和显式执行状态，不能用含分红指数作股价')
        if not monthly['is_demo']==daily['is_demo']==states['is_demo']:raise ValueError('合成/来源声明的数据性质必须一致')
        p=parameters(config)
        if p['top_k']>=len(monthly['symbols']):raise ValueError('选股数必须小于固定股票池')
        if parent_id:self.store.get('portfolio_spec',parent_id)
        return self.store.put('portfolio_spec',dict(name=name,parent_id=parent_id,experiment_id=experiment_id,factor_id=factor_id,
                         monthly_id=monthly['id'],daily_id=daily_id,execution_id=execution_id,config=p,engine=experiment['engine'],
                         holdout_start=experiment['protocol']['holdout_start'],development_end=experiment['protocol']['development_end'],
                         start=experiment['report']['folds'][0]['test_signal_start'],research_only=True,
                         note='固定因子的开发窗历史组合回放，不是独立最终验收，也不继承单笔研究的启用资格。'))

    def submit(self, *, spec_id=None, replay_id=None, timeout_seconds=120):
        if not self.jobs:raise ValueError('组合请求需要运行中的任务服务')
        if bool(spec_id)==bool(replay_id):raise ValueError('请选择组合版本或已完成回放编号中的一个')
        if replay_id:
            old=self.store.get('portfolio_run',replay_id)
            if old['engine']!=engine_fingerprint():raise ValueError('引擎已改变，不能跨版本冒充复现')
            spec_id=old['spec_id']
        spec=self.store.get('portfolio_spec',spec_id)
        if spec['engine']!=engine_fingerprint():raise ValueError('组合版本属于旧引擎，请按当前实验创建新版本')
        return self.jobs.create('portfolio',dict(spec_id=spec_id,engine=engine_fingerprint()),integer(timeout_seconds,'组合运行时限',1,3600))

    def _inputs(self,spec,*,window_start=None,window_end=None):
        window_start=window_start or spec['start']
        window_end=window_end or spec['development_end']
        monthly=self.store.get('snapshot',spec['monthly_id'])
        daily=self.store.get('snapshot',spec['daily_id']);states=self.store.get('snapshot',spec['execution_id'])
        factor=self.store.get('factor',spec['factor_id'])
        rows=[row for row in monthly['rows'] if row['date']<=window_end]
        dates,panel=parse_panel(datasets.csv_text('monthly_price',rows),required_fields=factors.required_fields([factor]),return_basis='price')
        symbols=set(panel[0])
        if not monthly['is_demo'] and any(not re.fullmatch(r'(?:60\d{4}\.SH|00\d{4}\.SZ|30\d{4}\.SZ)',s) for s in symbols):
            raise ValueError('首版仅支持沪深主板/创业板100股单位，科创板/北交所及其他品种尚未覆盖')
        grouped={}
        for row in daily['rows']:
            if row['symbol'] in symbols and row['date']<=window_end:
                grouped.setdefault(row['date'],{})[row['symbol']]=row
        for stamp,month in zip(dates,panel):
            stamp=stamp.isoformat()
            if stamp not in grouped or set(grouped[stamp])!=symbols:
                raise ValueError('月度信号的日线收盘不齐全，不补过期价格')
            for symbol in symbols:
                if price(month[symbol]['close'])!=price(grouped[stamp][symbol]['close']):
                    raise ValueError('月度价格和绑定日线收盘不一致，不能混用版本')
        bars={stamp:month for stamp,month in grouped.items() if stamp>=window_start}
        execution={}
        for row in states['rows']:
            if row['symbol'] in symbols and window_start<=row['date']<=window_end:
                execution.setdefault(row['date'],{})[row['symbol']]=row
        if not bars or set(execution)!=set(bars):raise ValueError('执行状态日期必须完整覆盖回放日线，缺失不能视为可成交')
        for stamp,month in bars.items():
            if datasets.day(stamp).weekday()>=5 or set(month)!=symbols or set(execution[stamp])!=symbols:
                raise ValueError('日线/状态须为完整固定池工作日截面，缺失不填充')
            for symbol,row in month.items():
                state=execution[stamp][symbol]
                if row['available_at']>stamp or state['available_at']>stamp:raise ValueError('行情或执行状态在该日尚不可用')
                levels=[price(row[key]) for key in ('open','high','low','close')]
                if state['suspended']:
                    if row['volume']!=0 or len(set(levels))!=1:raise ValueError('停牌须显式提供零量平价估值栏，不能伪造成交')
                elif row['volume']<=0:raise ValueError('未停牌日成交量为零，数据状态矛盾')
                if not state['no_price_limit']:
                    low,high=price(state['limit_down']),price(state['limit_up'])
                    if min(levels)<low or max(levels)>high:raise ValueError('OHLC超出声明限价，先核对公司行动/状态数据')
        start_index=next(i for i,stamp in enumerate(dates) if stamp.isoformat()==spec['start'])
        signal_dates={dates[i].isoformat() for i in range(start_index,len(dates),spec['config']['rebalance_months']) if dates[i].isoformat()>=window_start}
        return dates,panel,bars,execution,factor,signal_dates,monthly,daily,states

    def run(self, *, spec_id, run_id=None, checkpoint=None):
        engine=engine_fingerprint()
        try:
            spec=self.store.get('portfolio_spec',spec_id)
            if spec['engine']!=engine:raise ValueError('组合引擎改变，禁止沿用旧版本')
            dates,panel,bars,states,factor,signals,monthly,daily,execution=self._inputs(spec)
            report=simulate(dates=dates,panel=panel,daily=bars,states=states,factor=factor,config=spec['config'],signal_dates=signals,checkpoint=checkpoint)
            report.update(is_demo=monthly['is_demo'],return_basis='unadjusted_price',
                          warnings=list(dict.fromkeys(monthly['warnings']+daily['warnings']+execution['warnings']+[
                              '固定池存在幸存者偏差；价格收益不含现金分红，公司行动未调整。',
                              '成本为用户固定假设，不代表各历史日期税费；无成交量参与率、盘口排队或真实交易日历核验。',
                              '月度信号后下一日线开盘模拟；信号日固定股数，失败订单不延续追单；终点持仓未强平。',
                              '固定因子的开发窗探索性组合回放；没有独立最终组合验证，模型准入仍只看原单笔研究。'])),
                          protocol=dict(holdout_start=spec['holdout_start'],holdout_evaluated=False,start=spec['start'],
                                        end=spec['development_end'],execution='next_observed_daily_open',lot_size=100,
                                        t_plus_one=True,config=spec['config'],independent_final_validation=False))
            body=dict(spec_id=spec_id,status='completed',engine=engine,report=report)
        except (ValueError,TypeError,KeyError) as exc:
            body=dict(spec_id=spec_id,status='failed',engine=engine,error=str(exc))
        return self.store.put('portfolio_run',body,run_id or uuid.uuid4().hex)

    def demo(self):
        rng=random.Random(20261016);rows=[];states=[];symbols=[f'PORT{i}' for i in range(1,6)]
        previous={s:20+i*10 for i,s in enumerate(symbols)}
        stamp=date(2017,1,2)
        while stamp<=date(2025,12,31):
            if stamp.weekday()<5:
                for symbol in symbols:
                    last=previous[symbol];lowlimit=round(last*.9,2);uplimit=round(last*1.1,2)
                    opened=round(last*(1+rng.uniform(-.005,.005)),2)
                    closed=round(opened*(1+rng.uniform(-.025,.025)),2)
                    rows.append(dict(date=stamp.isoformat(),symbol=symbol,open=opened,high=round(max(opened,closed)*1.002,2),
                                     low=round(min(opened,closed)*.998,2),close=closed,volume=1000000,total_return_index='',available_at=stamp.isoformat()))
                    states.append(dict(date=stamp.isoformat(),symbol=symbol,suspended=0,limit_up=uplimit,limit_down=lowlimit,
                                       no_price_limit=0,available_at=stamp.isoformat()))
                    previous[symbol]=closed
            stamp+=timedelta(days=1)
        daily=datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',rows),source='组合流程合成日线；工作日日历未对应真实交易所',price_basis='unadjusted',is_demo=True)
        execution=datasets.import_snapshot(self.store,kind='execution',raw_csv=datasets.csv_text('execution',states),source='组合流程合成开盘前状态；非实际停牌/涨跌停',price_basis='not_applicable',is_demo=True)
        monthly=datasets.monthly_from_daily(self.store,daily_id=daily['id'],return_basis='price')
        factor=next(f for f in self.store.list('factor') if f['kind']=='momentum' and not f['parent_id'])
        experiment=self.workbench.run(snapshot_id=monthly['id'],factor_ids=[factor['id']])
        if experiment['status']!='completed':raise ValueError(experiment['error'])
        spec=self.create_spec(experiment_id=experiment['id'],factor_id=factor['id'],daily_id=daily['id'],execution_id=execution['id'],name='合成组合账本示例')
        return dict(spec=spec,experiment_id=experiment['id'],daily_id=daily['id'],execution_id=execution['id'],monthly_id=monthly['id'])

    def get(self,path):
        if path=='/api/portfolio/specs':return self.store.list('portfolio_spec')
        if path=='/api/portfolio/runs':return [{k:v for k,v in row.items() if k!='report'} for row in self.store.list('portfolio_run')]
        if path.startswith('/api/portfolio/runs/'):return self.store.get('portfolio_run',path.removeprefix('/api/portfolio/runs/'))
        raise ValueError('组合路径不存在')

    def post(self,path,payload):
        if path=='/api/portfolio/specs':return self.create_spec(**payload)
        if path=='/api/portfolio/runs':return self.submit(**payload)
        if path=='/api/portfolio/demo':
            if payload:raise ValueError('合成组合示例无需参数')
            return self.demo()
        raise ValueError('组合路径不存在')
