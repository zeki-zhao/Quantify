"""Version-bound local data pipelines, append-only forward observations and opt-in scheduling."""
import json
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone, timedelta

from . import datasets, factors
from .core import integer
from .forward_portfolio import replay, orders_for, observation
from .lifecycle import Lifecycle
from .market import closed_day
from .portfolio_lifecycle import PortfolioLifecycle
from .portfolio_service import PortfolioService
from .storage import canonical, digest
from .workbench import engine_fingerprint

CHINA=timezone(timedelta(hours=8))


def now():
    return datetime.now(timezone.utc)


class WaitingData(ValueError):
    pass


class BindingChanged(ValueError):
    pass


def append_only(previous,current):
    if any(previous[k]!=current[k] for k in ('kind','is_demo','price_basis','symbols')):
        raise ValueError('数据性质、口径或固定股票池改变')
    old={canonical(r) for r in previous['rows']};new={canonical(r) for r in current['rows']}
    if not old<=new:raise ValueError('历史数据缺失或被修订；前向流水线暂停，不改写旧账本')
    if previous['kind'] in ('daily','execution') and any(r['date']<=previous['date_range'][1] for r in current['rows'] if canonical(r) not in old):
        raise ValueError('检测到历史回填；拒绝改变已记录的观察日或成交顺序')


class PipelineService:
    def __init__(self,workbench,jobs=None,clock=None):
        self.workbench,self.store,self.jobs=workbench,workbench.store,jobs
        self.clock=clock or (lambda:now())
        self.scheduler_error=None
        self.single=Lifecycle(workbench);self.portfolio=PortfolioLifecycle(workbench)
        with closing(self.store.connect()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS pipeline_state (pipeline_id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS portfolio_forward_heads (pipeline_id TEXT PRIMARY KEY, report_id TEXT NOT NULL)')

    def runtime(self,pipeline_id):
        with closing(self.store.connect()) as db:
            r=db.execute('SELECT body FROM pipeline_state WHERE pipeline_id=?',(pipeline_id,)).fetchone()
        if not r:raise ValueError('流水线不存在')
        body=json.loads(r[0])
        if self.jobs and body.get('pending_job_id') and not body['blocked']:
            job=self.jobs.job(body['pending_job_id'])
            if job['status'] in ('failed','cancelled','timed_out','interrupted'):
                body.update(blocked='execution_failed',execution_error=job['error'])
        return body

    def update_runtime(self,pipeline_id,**changes):
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT body FROM pipeline_state WHERE pipeline_id=?',(pipeline_id,)).fetchone()
            if not r:raise ValueError('流水线不存在')
            body=json.loads(r[0]);body.update(changes)
            db.execute('UPDATE pipeline_state SET body=? WHERE pipeline_id=?',(canonical(body),pipeline_id))
        return body

    def binding(self,spec):
        if spec['engine']!=engine_fingerprint():raise BindingChanged('引擎已改变，请恢复原环境或重新建立经验证的版本')
        state=(self.single if spec['mode']=='single' else self.portfolio).state()
        if state['active_id']!=spec['freeze_id'] or not state['events'] or state['events'][-1]['seq']!=spec['activation_seq']:
            raise BindingChanged('启用事件已改变；更替、退役或回滚后须显式创建新流水线')
        if self.clock()<datetime.fromisoformat(state['events'][-1]['created_at']):
            raise BindingChanged('当前时间早于绑定启用事件')
        return state

    def create(self,*,name,mode,daily_root,execution_root=None,financials_root=None,universe_root=None):
        name=datasets.text(name,'流水线名称',100)
        if not isinstance(daily_root,str):raise ValueError('请选择明确的日线来源')
        if mode not in ('single','portfolio'):raise ValueError('流水线类型须为single或portfolio')
        state=(self.single if mode=='single' else self.portfolio).state()
        if not state['active_id']:raise ValueError('请先人工启用对应研究版本')
        frozen=self.store.get('freeze' if mode=='single' else 'portfolio_freeze',state['active_id'])
        if frozen['engine']!=engine_fingerprint():raise ValueError('活跃研究版本属于旧引擎')
        roots=dict(daily=daily_root,execution=execution_root,financials=financials_root,universe=universe_root)
        for kind,i in roots.items():
            if i is not None and self.store.get('snapshot',i)['kind']!=kind:raise ValueError('来源快照类型不匹配')
        if mode=='portfolio' and not execution_root:raise ValueError('组合前向需要显式执行状态来源')
        if mode=='single' and execution_root:raise ValueError('单笔流程不使用组合执行状态')
        record=dict(name=name,mode=mode,roots=roots,freeze_id=frozen['id'],activation_seq=state['events'][-1]['seq'],
                    engine=frozen['engine'],created= self.clock().isoformat(),research_only=True)
        self.binding(record)
        spec=self.store.put('pipeline_spec',record)
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT OR IGNORE INTO pipeline_state VALUES (?,?)',(spec['id'],canonical(dict(enabled=False,revision=0,blocked=None,latest_run_id=None,last_intent=None,pending_job_id=None))))
        return dict(spec=spec,runtime=self.runtime(spec['id']))

    def configure(self,*,pipeline_id,enabled,expected_revision):
        if type(enabled) is not bool:raise ValueError('自动运行开关必须为布尔值')
        expected_revision=integer(expected_revision,'调度设置版本',0,1000000)
        spec=self.store.get('pipeline_spec',pipeline_id)
        if enabled:self.binding(spec)
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE');r=db.execute('SELECT body FROM pipeline_state WHERE pipeline_id=?',(pipeline_id,)).fetchone()
            if not r:raise ValueError('流水线不存在')
            body=json.loads(r[0])
            if body['revision']!=expected_revision:raise ValueError('调度设置已改变，请刷新')
            body.update(enabled=enabled,revision=body['revision']+1)
            db.execute('UPDATE pipeline_state SET body=? WHERE pipeline_id=?',(canonical(body),pipeline_id))
        return body

    def claim_intent(self,pipeline_id,slot,clock):
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            body=json.loads(db.execute('SELECT body FROM pipeline_state WHERE pipeline_id=?',(pipeline_id,)).fetchone()[0])
            if not body['enabled'] or body['blocked'] or body.get('intent_inflight') or body['last_intent']==slot:return False
            jobs=db.execute("SELECT payload FROM jobs WHERE kind='pipeline' AND status IN ('queued','running')").fetchall()
            if any(json.loads(r[0]).get('pipeline_id')==pipeline_id for r in jobs):return False
            body.update(last_intent=slot,intent_at=clock.isoformat(),intent_inflight=True)
            db.execute('UPDATE pipeline_state SET body=? WHERE pipeline_id=?',(canonical(body),pipeline_id))
        return True

    def resolve(self,spec):
        snapshots=self.store.list('snapshot');by_id={s['id']:s for s in snapshots};result={}
        for kind,root in spec['roots'].items():
            if root is None:result[kind]=None;continue
            ids={root}
            while True:
                children={s['id'] for s in snapshots if s.get('parent_id') in ids}
                if children<=ids:break
                ids|=children
            parents={by_id[i].get('parent_id') for i in ids}
            leaves=ids-parents
            if len(leaves)!=1:raise ValueError('来源出现分支，不能静默选择其中一个；请核对后建立明确来源')
            result[kind]=next(iter(leaves))
        return result

    def submit(self,*,pipeline_id,timeout_seconds=300,automatic=False):
        if not self.jobs:raise ValueError('流水线需要运行中的队列服务')
        spec=self.store.get('pipeline_spec',pipeline_id)
        if any(j['kind']=='pipeline' and j['payload'].get('pipeline_id')==pipeline_id and j['status'] in ('queued','running') for j in self.jobs.jobs()):
            raise ValueError('该流水线已有待执行任务')
        # Resolve in the worker too; errors need durable quality reports, not lost scheduler exceptions.
        request=dict(pipeline_id=pipeline_id,engine=engine_fingerprint(),automatic=automatic)
        job=self.jobs.create('pipeline',request,integer(timeout_seconds,'流水线时限',1,3600))
        self.update_runtime(pipeline_id,pending_job_id=job['id'],intent_inflight=False)
        return job

    def view(self,snapshot,cutoff):
        rows=[r for r in snapshot['rows'] if r.get('date',r.get('period_end'))<=cutoff and r['available_at']<=cutoff]
        if not rows:raise WaitingData('尚无已完成且可用的数据')
        if rows==snapshot['rows']:return snapshot
        view=datasets.import_snapshot(self.store,kind=snapshot['kind'],raw_csv=datasets.csv_text(snapshot['kind'],rows),
              source=snapshot['source'],price_basis=snapshot['price_basis'],is_demo=snapshot['is_demo'])
        body={k:v for k,v in view.items() if k not in ('id','created_at')}
        body['temporal_source']=snapshot['id']
        return self.store.put('snapshot',body)

    def _last(self,pipeline_id):
        runtime=self.runtime(pipeline_id)
        if runtime.get('accepted_run_id'):return self.store.get('pipeline_run',runtime['accepted_run_id'])
        return None

    def portfolio_forward(self,spec,frozen,monthly,views,checkpoint):
        pipeline_id=spec['id'];signals=sorted([s for s in self.store.list('portfolio_forward_signal') if s['pipeline_id']==pipeline_id],key=lambda s:(s['generated_at'],s['id']))
        if signals:
            # A process can stop after the signal commit but before the report/head commit.
            # Its inputs are already observed, even without an accepted pipeline_run yet.
            latest=signals[-1]
            append_only(self.store.get('snapshot',latest['daily_id']),views['daily'])
            append_only(self.store.get('snapshot',latest['execution_id']),views['execution'])
            append_only(self.store.get('snapshot',latest['monthly_id']),monthly)
        bound={**frozen['spec'], 'monthly_id':monthly['id'],'daily_id':views['daily']['id'],'execution_id':views['execution']['id']}
        end=views['daily']['date_range'][1]
        dates,panel,bars,states,factor,_,*_=PortfolioService(self.workbench)._inputs(bound,window_start=frozen['start'],window_end=end)
        config=frozen['spec']['config'];ref=frozen['reference_spec'];refconfig=ref['config'] if ref else config
        accounts,report=replay(signals=signals,daily=bars,states=states,config=config,reference_config=refconfig,checkpoint=checkpoint)
        monthly_stamp=dates[-1].isoformat();clock=self.clock();generation_date=clock.astimezone(CHINA).date().isoformat()
        def due(strategy):
            a=datasets.day(strategy['start']);b=dates[-1]
            return (b.year*12+b.month-a.year*12-a.month)%strategy['config']['rebalance_months']==0
        selected_due=due(frozen['spec']);reference_due=due(ref) if ref else selected_due
        new_signal=None
        if monthly_stamp>frozen['end'] and (selected_due or reference_due) and not any(s['monthly_signal_date']==monthly_stamp for s in signals):
            if report and report['pending_orders']:raise WaitingData('前一信号仍待之后的观察日，暂不追加调仓信号')
            marks={s:r['close'] for s,r in bars[end].items()}
            scores=factors.calculate(panel,len(panel)-1,[factor]);chosen=sorted(panel[-1],key=lambda s:(-scores[s][factor['id']],s))[:config['top_k']]
            if ref:
                rf=self.store.get('factor',ref['factor_id']);rv=factors.calculate(panel,len(panel)-1,[rf]);rc=sorted(panel[-1],key=lambda s:(-rv[s][rf['id']],s))[:refconfig['top_k']]
            else:rc=sorted(panel[-1])
            body=dict(pipeline_id=pipeline_id,freeze_id=frozen['id'],activation_seq=spec['activation_seq'],
                      monthly_id=monthly['id'],daily_id=views['daily']['id'],execution_id=views['execution']['id'],
                      monthly_signal_date=monthly_stamp,valuation_date=end,generated_at=clock.isoformat(),generation_date=generation_date,
                      selected_due=selected_due,reference_due=reference_due,selected_symbols=chosen,reference_symbols=rc,
                      orders=dict(selected=orders_for(accounts['selected'],chosen,marks,config,monthly_stamp) if selected_due else [],
                                  reference=orders_for(accounts['reference'],rc,marks,refconfig,monthly_stamp) if reference_due else []),
                      is_demo=monthly['is_demo'],note='股数在实际生成时按最新已完成日线估值冻结，仅在生成日期之后的首个观察日模拟成交。')
            sid=digest(dict(pipeline_id=pipeline_id,monthly=monthly_stamp))
            with closing(self.store.connect()) as db,db:
                db.execute('BEGIN IMMEDIATE')
                active=db.execute('SELECT freeze_id,seq FROM portfolio_lifecycle_events ORDER BY seq DESC LIMIT 1').fetchone()
                if active!=(spec['freeze_id'],spec['activation_seq']):raise BindingChanged('活跃组合已改变')
                existing=db.execute("SELECT body FROM artifacts WHERE kind='portfolio_forward_signal' AND id=?",(sid,)).fetchone()
                if not existing:db.execute('INSERT INTO artifacts VALUES (?,?,?,?)',('portfolio_forward_signal',sid,clock.isoformat(),canonical(body)))
            new_signal=self.store.get('portfolio_forward_signal',sid);signals.append(new_signal)
            signals.sort(key=lambda s:(s['generated_at'],s['id']))
            accounts,report=replay(signals=signals,daily=bars,states=states,config=config,reference_config=refconfig,checkpoint=checkpoint)
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT report_id FROM portfolio_forward_heads WHERE pipeline_id=?',(pipeline_id,)).fetchone()
        if row and report:
            old=self.store.get('portfolio_forward_report',row[0])['report']
            if old:
                for key in ('selected','reference'):
                    for field in ('curve','trades','orders'):
                        if report[key][field][:len(old[key][field])]!=old[key][field]:raise ValueError('已有前向账目被改写，拒绝提交')
        labels=[]
        if report:
            endpoints={s['valuation_date']:s for s in signals if s['selected_due']}
            for period in report['periods']:
                end_signal=endpoints[period['end']]
                label=dict(pipeline_id=pipeline_id,freeze_id=frozen['id'],**period,
                           start_signal_id=endpoints[period['start']]['id'],end_signal_id=end_signal['id'],
                           matured_at=end_signal['generated_at'],daily_id=end_signal['daily_id'],
                           note='真实记录的相邻调仓决策间成熟净值标签；不重叠，不回填。')
                labels.append(self.store.put('portfolio_forward_label',label,digest(dict(pipeline_id=pipeline_id,start=period['start'],end=period['end']))))
        result=dict(pipeline_id=pipeline_id,freeze_id=frozen['id'],signals=signals,report=report,
                    labels=labels,pending_labels=1 if any(s['selected_due'] for s in signals) else 0,
                    observation=observation(report,frozen['objective']),is_demo=monthly['is_demo'],
                    input_ids={k:(v['id'] if v else None) for k,v in views.items()},monthly_id=monthly['id'],
                    note='仅已记录的实际信号；生成前无回填交易；现金起步，新启用事件需新流水线，不继承历史持仓。')
        artifact=self.store.put('portfolio_forward_report',result)
        self.binding(spec)
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO portfolio_forward_heads VALUES (?,?) ON CONFLICT(pipeline_id) DO UPDATE SET report_id=excluded.report_id',(pipeline_id,artifact['id']))
        return artifact,new_signal

    def run(self,*,pipeline_id,run_id=None,checkpoint=None):
        run_id=run_id or uuid.uuid4().hex;spec=self.store.get('pipeline_spec',pipeline_id)
        progress={}
        def stage(name,**values):
            progress.update(stage=name,**values)
            if checkpoint:checkpoint(progress)
        try:
            stage('binding');self.binding(spec)
            stage('sources');ids=self.resolve(spec);clock=self.clock();cutoff=closed_day(clock)
            views={kind:self.view(self.store.get('snapshot',i),cutoff) if i else None for kind,i in ids.items()}
            prior=self._last(pipeline_id)
            for kind,view in views.items():
                if view:
                    if view['is_demo']!=views['daily']['is_demo']:raise ValueError('来源的数据性质不一致')
                    root=self.view(self.store.get('snapshot',spec['roots'][kind]),cutoff)
                    append_only(root,view)
                    if prior and prior.get('view_ids',{}).get(kind):append_only(self.store.get('snapshot',prior['view_ids'][kind]),view)
            if (clock.astimezone(CHINA).date()-datasets.day(views['daily']['date_range'][1])).days>7:
                raise WaitingData('日线已超过7天，等待近期已完成数据')
            cross_sections={}
            for row in views['daily']['rows']:cross_sections.setdefault(row['date'],set()).add(row['symbol'])
            if any(symbols!=set(views['daily']['symbols']) for symbols in cross_sections.values()):
                raise ValueError('日线存在不完整固定池截面，不补价或静默删股')
            frozen=self.store.get('freeze' if spec['mode']=='single' else 'portfolio_freeze',spec['freeze_id'])
            basis=('price' if frozen['price_basis']=='unadjusted' else 'total_return') if spec['mode']=='single' else 'price'
            if spec['mode']=='portfolio':
                for kind,key in (('daily','daily_id'),('execution','execution_id')):append_only(self.store.get('snapshot',frozen['spec'][key]),views[kind])
            stage('monthly')
            monthly=datasets.monthly_from_daily(self.store,daily_id=views['daily']['id'],financials_id=views['financials']['id'] if views['financials'] else None,
                                                universe_id=views['universe']['id'] if views['universe'] else None,return_basis=basis)
            previous=self.store.get('snapshot',frozen['request']['snapshot_id'] if spec['mode']=='single' else frozen['spec']['monthly_id'])
            append_only(previous,monthly)
            if prior and prior.get('monthly_id'):append_only(self.store.get('snapshot',prior['monthly_id']),monthly)
            if (clock.astimezone(CHINA).date()-datasets.day(monthly['date_range'][1])).days>45:raise WaitingData('月度截面超过45天，等待完整新月份')
            stage('signals',monthly_id=monthly['id'])
            if spec['mode']=='single':
                signal=None
                if monthly['date_range'][1]>frozen['data_end']:
                    signal=self.single.signal(snapshot_id=monthly['id'],expected_seq=spec['activation_seq'])
                stage('mature_labels')
                report=self.single.observe(strategy_id=spec['freeze_id'],snapshot_id=monthly['id'],expected_seq=spec['activation_seq'])
                output=dict(signal_id=signal['id'] if signal else None,forward=report)
                observation_state=report['state'];degradation=report['degradation']
            else:
                stage('forward_ledger')
                report,signal=self.portfolio_forward(spec,frozen,monthly,views,checkpoint)
                output=dict(signal_id=signal['id'] if signal else None,portfolio_report_id=report['id'])
                observation_state=report['observation']['state'];degradation=report['observation']['degradation']
            self.binding(spec);stage('committing')
            body=dict(pipeline_id=pipeline_id,status='completed',state=observation_state,degradation=degradation,
                      source_ids=ids,view_ids={k:v['id'] if v else None for k,v in views.items()},monthly_id=monthly['id'],
                      output=output,is_demo=monthly['is_demo'],checked_at=clock.isoformat())
        except WaitingData as exc:
            body=dict(pipeline_id=pipeline_id,status='completed',state='waiting_data',degradation=False,message=str(exc),checked_at=self.clock().isoformat())
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            category='version_changed' if isinstance(exc,BindingChanged) else 'data_quality'
            if category=='data_quality':
                try:self.binding(spec)
                except BindingChanged:category='version_changed'
            body=dict(pipeline_id=pipeline_id,status='failed',state=category,
                      degradation=False,error=str(exc),stage=progress.get('stage'),checked_at=self.clock().isoformat())
        result=self.store.put('pipeline_run',body,run_id)
        changes=dict(latest_run_id=result['id'],blocked=body['state'] if body['status']=='failed' else None)
        if body['status']=='completed' and body['state']!='waiting_data':changes['accepted_run_id']=result['id']
        self.update_runtime(pipeline_id,**changes)
        return result

    def get(self,path):
        if path=='/api/pipelines/status':return dict(scheduler_error=self.scheduler_error,notice='自动运行默认关闭；应用运行时检查追加，失败暂停后需显式验证恢复。')
        if path=='/api/pipelines/specs':return [dict(spec=s,runtime=self.runtime(s['id'])) for s in self.store.list('pipeline_spec')]
        if path=='/api/pipelines/runs':return [{k:v for k,v in r.items() if k!='output'} for r in self.store.list('pipeline_run')]
        if path.startswith('/api/pipelines/reports/'):return self.store.get('portfolio_forward_report',path.removeprefix('/api/pipelines/reports/'))
        if path.startswith('/api/pipelines/runs/'):return self.store.get('pipeline_run',path.removeprefix('/api/pipelines/runs/'))
        if path.startswith('/api/pipelines/portfolio/'):
            pipeline_id=path.removeprefix('/api/pipelines/portfolio/')
            with closing(self.store.connect()) as db:r=db.execute('SELECT report_id FROM portfolio_forward_heads WHERE pipeline_id=?',(pipeline_id,)).fetchone()
            if not r:raise ValueError('该流水线尚无组合前向报告')
            return self.store.get('portfolio_forward_report',r[0])
        raise ValueError('流水线路径不存在')

    def post(self,path,payload):
        if path=='/api/pipelines/specs':return self.create(**payload)
        if path=='/api/pipelines/config':return self.configure(**payload)
        if path=='/api/pipelines/run':
            if set(payload)-{'pipeline_id','timeout_seconds'}:raise ValueError('流水线运行参数无效')
            return self.submit(**payload)
        raise ValueError('流水线路径不存在')


class PipelineScheduler:
    def __init__(self,service,start_thread=True):
        self.service=service;self.stop=threading.Event();self.thread=None
        for spec in service.store.list('pipeline_spec'):
            if service.runtime(spec['id']).get('intent_inflight'):
                service.update_runtime(spec['id'],blocked='execution_failed',intent_inflight=False,scheduler_error='服务在调度意图提交后中断；请显式运行验证恢复')
        if start_thread:
            self.thread=threading.Thread(target=self._loop,name='quantify-pipelines',daemon=True);self.thread.start()

    def tick(self):
        service=self.service
        for spec in service.store.list('pipeline_spec'):
            i=spec['id'];state=service.runtime(i)
            if not state['enabled'] or state['blocked'] or state.get('intent_inflight'):continue
            pending=state.get('pending_job_id')
            if pending:
                job=service.jobs.job(pending)
                if job['status'] in ('queued','running'):continue
                if job['status'] in ('failed','cancelled','timed_out','interrupted'):
                    service.update_runtime(i,blocked='execution_failed' if not state['blocked'] else state['blocked']);continue
            clock=service.clock().astimezone(CHINA)
            if (clock.hour,clock.minute)<(16,30) or clock.weekday()>=5:continue
            try:
                service.binding(spec);sources=service.resolve(spec)
                slot=digest(dict(day=clock.date().isoformat(),sources=sources))
                if not service.claim_intent(i,slot,clock):continue
                service.submit(pipeline_id=i,automatic=True)
            except (ValueError,TypeError) as exc:
                service.update_runtime(i,blocked='version_changed' if isinstance(exc,BindingChanged) else 'data_quality',scheduler_error=str(exc))

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
                self.service.scheduler_error=None
            except Exception as exc:
                self.service.scheduler_error=dict(type=type(exc).__name__,at=self.service.clock().isoformat())
            self.stop.wait(30)

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=5)
