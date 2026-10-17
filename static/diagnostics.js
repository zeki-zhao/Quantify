'use strict';
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const short=v=>v.slice(0,10),pct=v=>v==null?'无样本':(v*100).toFixed(3)+'%',val=v=>v==null?'未定义':v.toFixed(4);
const table=(heads,rows)=>`<table><thead><tr>${heads.map(v=>`<th>${esc(v)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
let busy=false,job=null,selected=null,factors=[],experiments=[],audit=null,trialSequence=0;
async function api(path,payload){const r=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),b=await r.json();if(!r.ok)throw new Error(b.error||'请求失败');return b;}
async function action(fn){if(busy)return;busy=true;$('status').className='';$('status').textContent='正在处理…';try{await fn();$('status').textContent='操作完成。';}catch(e){$('status').className='error';$('status').textContent=e.message;}finally{busy=false;}}
function chooseFactors(preserve=false){const checked=new Set([...$('diagnostic-factors').querySelectorAll('input:checked')].map(i=>i.value));const experiment=experiments.find(e=>e.id===$('experiment').value);$('diagnostic-factors').replaceChildren();for(const f of factors){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=f.id;input.checked=preserve?checked.has(f.id):!!experiment?.request.factor_ids.includes(f.id);label.append(input,document.createTextNode(f.name+' · '+short(f.id)));$('diagnostic-factors').append(label);}}
async function refresh(current=()=>true){
 const [e,f,reports]=await Promise.all([api('/api/workspace/experiments'),api('/api/workspace/factors'),api('/api/diagnostics/reports')]);if(!current())return;factors=f;experiments=e.filter(e=>e.status==='completed');
 const prev=$('experiment').value;$('experiment').replaceChildren();for(const e of experiments)$('experiment').add(new Option(short(e.id)+' · '+e.created_at,e.id));if(experiments.some(e=>e.id===prev))$('experiment').value=prev;chooseFactors(experiments.some(e=>e.id===prev));
 $('reports').innerHTML=table(['报告','源实验','因子数 / 分组','状态','时间'],reports.map(r=>[short(r.id),short(r.request.experiment_id),r.request.factor_ids.length+' / '+r.request.group_count,r.status==='completed'?'已完成':'失败：'+r.error,r.created_at]));
 $('reports').querySelectorAll('tbody tr').forEach((tr,i)=>{const td=document.createElement('td'),b=document.createElement('button');b.textContent='查看';b.className='secondary';b.onclick=()=>action(()=>show(reports[i].id));td.append(b);tr.append(td);});await trials();
}
async function trials(){
 const sequence=++trialSequence,scope=$('trial-scope').value,exp=$('experiment').value;
 const current=()=>sequence===trialSequence&&$('trial-scope').value===scope&&(scope==='all'||$('experiment').value===exp);
 audit=null;$('export-trials').disabled=true;$('trial-note').textContent='正在加载当前范围…';
 for(const id of ['trial-counts','trial-statuses','trial-repeats','trial-experiments','trial-json'])$(id).replaceChildren();
 if(scope==='experiment'&&!exp)throw new Error('请选择开发实验');
 let result;try{result=await api('/api/diagnostics/trials'+(scope==='experiment'?'/'+exp:''));}catch(error){if(!current())return;throw error;}
 if(!current())return;audit=result;$('export-trials').disabled=false;const c=audit.counts;
 $('trial-note').textContent=`截至 ${audit.as_of}。${audit.note}`;
 $('trial-counts').innerHTML=table(['收益实验 / 额外重复','会话','已消费候选','有效公式种类','重复候选','非法公式','停止提案','已提交未消费提案'],[[c.experiments+' / '+c.repeated_experiments,c.sessions,c.proposals_consumed,c.unique_valid_formulas,c.repeated_proposals,c.invalid_formulas,c.stop_proposals,c.submitted_unconsumed]]);
 $('trial-statuses').innerHTML=table(['类型','状态','条数'],[...Object.entries(c.experiment_statuses).map(([k,v])=>['收益实验',k,v]),...Object.entries(c.proposal_statuses).map(([k,v])=>['候选尝试',k,v])]);
 $('trial-repeats').innerHTML=table(['结构签名','尝试次数','会话数','会话 / 轮次 / 状态'],audit.repeated_formula_groups.map(r=>[short(r.signature),r.attempts,r.sessions,r.records.map(v=>short(v.session_id)+' / '+v.round+' / '+v.status).join('；')]));
 $('trial-experiments').innerHTML=table(['同协议与定义签名','实验编号（含失败/重放）'],audit.repeated_experiment_groups.map(g=>[short(g.signature),g.experiment_ids.map(short).join('；')]));$('trial-json').textContent=JSON.stringify(audit,null,2);
}
const watcher=new TaskWatcher({request:api,onUpdate:next=>{
 job=next;
 if(!job){$('job-info').textContent='正在提交任务…';$('cancel').disabled=true;$('retry').disabled=true;return;}
 const p=job.progress,terminal=['succeeded','failed','cancelled','timed_out','interrupted'];
 $('job-info').textContent=`任务 ${job.id} · ${job.status} · ${p.cached?'读取固定报告':(p.processed_windows??0)+'/'+(p.total_windows??'?')+' 信号窗'} · ${job.error||''}`;
 $('cancel').disabled=terminal.includes(job.status)||!!job.stop_requested;$('retry').disabled=!['failed','cancelled','timed_out','interrupted'].includes(job.status);
},onFinish:async(job,current)=>{await refresh(current);if(current()&&job.result?.diagnostic_id)await show(job.result.diagnostic_id,current);},onError:error=>{$('status').className='error';$('status').textContent=error.message;}});
async function start(payload){await watcher.start(()=>api('/api/diagnostics/reports',payload));}
async function show(id,current=()=>true){
 const result=await api('/api/diagnostics/reports/'+id);if(!current())return;selected=result;$('result').hidden=false;$('result-json').textContent=JSON.stringify(selected,null,2);
 for(const k of ['metrics','groups','correlations','stability','windows'])$(k).replaceChildren();$('replay').disabled=selected.status!=='completed';
 if(selected.status!=='completed'){$('result-note').textContent='诊断失败：'+selected.error;return;}
 const r=selected.report,names=Object.fromEntries(r.factors.map(f=>[f.factor.id,f.factor.name]));
 $('result-note').textContent=(r.is_demo?'合成数据，仅验证流程。':'来源由用户声明。')+`信号 ${r.protocol.first_signal} 至 ${r.protocol.last_exit}，留出起点 ${r.protocol.holdout_start}；共同成功截面 ${r.common_scored_windows}/${r.expected_windows}。`+r.warnings.join(' ')+r.notes.join(' ');
 $('metrics').innerHTML=table(['因子','计算覆盖 / 输入覆盖','IC均值','IC标准差','正IC比例','有效IC / 总窗口','高低组差 / 有效窗口'],r.factors.map(f=>{const s=f.summary;return [f.factor.name,pct(s.score_coverage)+' / '+pct(s.input_coverage),val(s.mean_rank_ic),val(s.rank_ic_std),pct(s.positive_ic_fraction),s.ic_windows+' / '+s.windows,pct(s.mean_high_minus_low)+' / '+s.spread_windows];}));
 $('groups').innerHTML=table(['因子','组（1最低）','平均单笔净收益','股票×窗口数'],r.factors.flatMap(f=>f.summary.groups.map(g=>[f.factor.name,g.group,pct(g.mean_return),g.observations])));
 $('correlations').innerHTML=table(['因子A','因子B','平均秩相关','共同成功窗','有效相关窗'],r.correlations.map(c=>[names[c.left_id],names[c.right_id],val(c.mean_rank_correlation),c.common_scored_windows,c.valid_windows]));
 $('stability').innerHTML=table(['因子','分段','IC均值','高低组差','有效IC窗 / 总窗'],r.factors.flatMap(f=>[...f.by_year.map(s=>[f.factor.name,s.year,val(s.mean_rank_ic),pct(s.mean_high_minus_low),s.ic_windows+' / '+s.windows]),...f.by_fold.map(s=>[f.factor.name,'测试块 '+s.fold,val(s.mean_rank_ic),pct(s.mean_high_minus_low),s.ic_windows+' / '+s.windows])]));
 $('windows').innerHTML=table(['因子','信号 / 入场 / 退出','IC','高低组差','状态或原因'],r.factors.flatMap(f=>f.windows.map(w=>[f.factor.name,[w.signal,w.entry,w.exit].join(' / '),val(w.rank_ic),pct(w.high_minus_low),w.status==='completed'?'成功；不同分值数 '+w.distinct_scores:w.reason])));
}
function download(name,data){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('diagnostic-form').onsubmit=e=>{e.preventDefault();action(()=>start({experiment_id:$('experiment').value,factor_ids:[...$('diagnostic-factors').querySelectorAll('input:checked')].map(i=>i.value),group_count:Number($('group-count').value)}));};
const reloadTrials=()=>trials().catch(error=>{$('status').className='error';$('status').textContent=error.message;});
$('experiment').onchange=()=>{chooseFactors();if($('trial-scope').value==='experiment')reloadTrials();};$('trial-scope').onchange=reloadTrials;
$('refresh').onclick=()=>action(refresh);$('replay').onclick=()=>action(()=>start({replay_id:selected.id}));$('export').onclick=()=>download('quantify-factor-diagnostics.json',selected);$('export-trials').onclick=()=>{if(audit)download('quantify-trial-audit.json',audit);};
$('cancel').onclick=()=>action(()=>watcher.cancel());$('retry').onclick=()=>action(()=>watcher.retry());action(refresh);
