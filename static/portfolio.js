'use strict';
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>(v*100).toFixed(3)+'%',num=v=>Number(v).toFixed(2),short=v=>v.slice(0,10);
const table=(heads,rows)=>`<table><thead><tr>${heads.map(v=>`<th>${esc(v)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
const terminal=['succeeded','failed','cancelled','timed_out','interrupted'];
let experiments=[],factors=[],specs=[],selected=null,job=null,busy=false;
async function api(path,payload){const r=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),b=await r.json();if(!r.ok)throw new Error(b.error||'请求失败');return b;}
async function action(fn){if(busy)return;busy=true;$('status').className='';$('status').textContent='正在处理…';try{await fn();$('status').textContent='操作完成。';}catch(e){$('status').className='error';$('status').textContent=e.message;}finally{busy=false;}}
function options(id,items,label,blank){const prev=$(id).value;$(id).replaceChildren();if(blank)$(id).add(new Option(blank,''));for(const i of items)$(id).add(new Option(label(i),i.id));if([...$(id).options].some(o=>o.value===prev))$(id).value=prev;}
function choose(){const e=experiments.find(e=>e.id===$('experiment').value);options('factor',factors.filter(f=>e?.request.factor_ids.includes(f.id)),f=>f.name+' · '+short(f.id));}
function buttons(container,rows,actions){$(container).querySelectorAll('tbody tr').forEach((tr,i)=>{const td=document.createElement('td');for(const [label,fn] of actions){const b=document.createElement('button');b.className='secondary';b.textContent=label;b.onclick=()=>action(()=>fn(rows[i]));td.append(b);}tr.append(td);});}
async function refresh(current=()=>true){
 const [runs,allExperiments,allFactors,snapshots,allSpecs]=await Promise.all([api('/api/portfolio/runs'),api('/api/workspace/experiments'),api('/api/workspace/factors'),api('/api/workspace/snapshots'),api('/api/portfolio/specs')]);
 if(!current())return;factors=allFactors;specs=allSpecs;experiments=allExperiments.filter(e=>e.status==='completed'&&snapshots.some(s=>s.id===e.request.snapshot_id&&s.kind==='monthly_price'));
 options('experiment',experiments,e=>short(e.id)+' · '+e.created_at);choose();
 options('daily',snapshots.filter(s=>s.kind==='daily'&&s.price_basis==='unadjusted'),s=>short(s.id)+' · '+(s.is_demo?'合成':'用户声明')+' · '+s.source);
 options('execution',snapshots.filter(s=>s.kind==='execution'),s=>short(s.id)+' · '+(s.is_demo?'合成':'用户声明')+' · '+s.source);
 options('parent',specs,s=>s.name+' · '+short(s.id),'初始版本');
 $('specs').innerHTML=table(['版本','因子','日线 / 状态','开发回放区间','初始资金 / 调仓','数据性质'],specs.map(s=>[s.name+' · '+short(s.id),short(s.factor_id),short(s.daily_id)+' / '+short(s.execution_id),s.start+' 至 '+s.development_end,num(s.config.initial_cash)+' / '+s.config.rebalance_months+'月',snapshots.find(d=>d.id===s.daily_id)?.is_demo?'合成数据':'用户声明']));
 buttons('specs',specs,[['提交回放',s=>start({spec_id:s.id})]]);
 $('runs').innerHTML=table(['回放','版本','状态','时间'],runs.map(r=>[short(r.id),short(r.spec_id),r.status==='completed'?'已完成':'失败：'+r.error,r.created_at]));
 buttons('runs',runs,[['查看',r=>show(r.id)]]);
}
const watcher=new TaskWatcher({request:api,onUpdate:next=>{
 job=next;
 if(!job){$('job-info').textContent='正在提交任务…';$('cancel').disabled=true;$('retry').disabled=true;return;}
 const p=job.progress;
 $('job-info').textContent=`任务 ${job.id} · ${job.status} · ${p.processed_days??0}/${p.total_days??'?'} 日 · ${job.error||''}`;
 $('cancel').disabled=terminal.includes(job.status)||!!job.stop_requested;
 $('retry').disabled=!['failed','cancelled','timed_out','interrupted'].includes(job.status);
},onFinish:async(job,current)=>{await refresh(current);if(current()&&job.result?.portfolio_run_id)await show(job.result.portfolio_run_id,current);},onError:error=>{$('status').className='error';$('status').textContent=error.message;}});
async function start(payload){await watcher.start(()=>api('/api/portfolio/runs',payload));}
function chart(report){
 const svg=$('nav-chart'),ns='http://www.w3.org/2000/svg',series=[report.selected.curve,report.baseline.curve];svg.replaceChildren();
 const values=series.flatMap(a=>a.map(p=>p.nav)),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||.1;
 const add=(tag,attrs,text)=>{const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;svg.append(e);};
 for(let i=0;i<3;i++){const y=20+i*105;add('line',{x1:60,y1:y,x2:880,y2:y,stroke:'#d4ded5'});add('text',{x:2,y:y+5,'font-size':13},(hi-span*i/2).toFixed(3));}
 series.forEach((a,i)=>add('polyline',{points:a.map((p,j)=>(60+820*j/Math.max(1,a.length-1))+','+(20+210*(hi-p.nav)/span)).join(' '),fill:'none',stroke:i?'#2969b0':'#18754b','stroke-width':2}));
 add('text',{x:60,y:255,'font-size':13},series[0][0].date);add('text',{x:790,y:255,'font-size':13},series[0].at(-1).date);
}
async function show(id,current=()=>true){
 const r=await api('/api/portfolio/runs/'+id);if(!current())return;selected=r;$('result').hidden=false;
 const spec=specs.find(s=>s.id===r.spec_id);$('result-json').textContent=JSON.stringify({spec,run:r},null,2);
 for(const k of ['metrics','holdings','trades','orders','curve'])$(k).replaceChildren();$('nav-chart').replaceChildren();
 $('replay').disabled=r.status!=='completed';
 if(r.status!=='completed'){$('result-note').textContent='回放失败：'+r.error;return;}
 const p=r.report,a=p.selected;
 $('result-note').textContent=(p.is_demo?'合成数据，仅验证流程。':'数据来源由用户声明。')+p.warnings.join(' ');
 $('metrics').innerHTML=table(['账本','期末权益','现金','累计收益','最大回撤','费用','滑点成本','双边累计换手','成交 / 拒单 / 部分'],[['筛选组合',p.selected.summary],['等权全池',p.baseline.summary]].map(([n,s])=>[n,num(s.ending_equity),num(s.ending_cash),pct(s.total_return),pct(s.max_drawdown),num(s.fees),num(s.slippage_cost),num(s.gross_turnover),[s.trade_count,s.rejected_orders,s.partial_orders].join(' / ')]));
 $('holdings').innerHTML=table(['代码','股数','含买费成本','市值','浮动盈亏'],a.holdings.map(h=>[h.symbol,h.quantity,num(h.cost_basis),num(h.market_value),num(h.unrealized_pnl)]));
 $('trades').innerHTML=table(['日期','代码','方向','股数','成交价','费用','税','成交后现金'],a.trades.slice(-50).map(t=>[t.date,t.symbol,t.side,t.quantity,num(t.price),num(t.fees),num(t.stamp_tax),num(t.cash_after)]));
 $('orders').innerHTML=table(['信号日期','执行日期','代码','方向','要求 / 成交','状态','原因'],a.orders.filter(o=>o.status!=='filled').map(o=>[o.signal_date,o.execution_date||'无下一观察日',o.symbol,o.side,o.quantity+' / '+o.filled_quantity,o.status,o.reason]));
 $('curve').innerHTML=table(['日期','现金','市值','权益','净值','回撤','已实现盈亏','浮动盈亏'],a.curve.slice(-50).map(p=>[p.date,num(p.cash),num(p.position_value),num(p.equity),p.nav.toFixed(4),pct(p.drawdown),num(p.realized_pnl),num(p.unrealized_pnl)]));chart(p);
}
$('experiment').onchange=choose;
$('spec-form').onsubmit=e=>{e.preventDefault();action(async()=>{const config={};for(const k of ['initial_cash','top_k','rebalance_months','cash_buffer','commission_bps','min_commission','stamp_tax_bps','transfer_fee_bps','slippage_bps'])config[k]=Number($(k).value);await api('/api/portfolio/specs',{name:$('name').value,parent_id:$('parent').value||null,experiment_id:$('experiment').value,factor_id:$('factor').value,daily_id:$('daily').value,execution_id:$('execution').value,config});await refresh();});};
$('demo').onclick=()=>action(async()=>{const d=await api('/api/portfolio/demo',{});await refresh();$('experiment').value=d.experiment_id;choose();$('daily').value=d.daily_id;$('execution').value=d.execution_id;});
$('refresh').onclick=()=>action(refresh);
$('cancel').onclick=()=>action(()=>watcher.cancel());
$('retry').onclick=()=>action(()=>watcher.retry());
$('replay').onclick=()=>action(()=>start({replay_id:selected.id}));
$('export').onclick=()=>{const url=URL.createObjectURL(new Blob([$('result-json').textContent],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='quantify-portfolio-ledger.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
action(refresh);
