'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const states={queued:'排队中',running:'计算中',waiting_input:'等待提案',paused:'已暂停',succeeded:'已完成',failed:'失败',cancelled:'已取消',timed_out:'超时',interrupted:'已中断'};
const pct=value=>value===undefined?'—':(value*100).toFixed(3)+'%';
const terminal=['succeeded','failed','cancelled','timed_out','interrupted'];
const table=(heads,rows)=>`<table><thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
let selected=null,job=null,context=null,busy=false,polling=false;
async function api(path,payload){
  const response=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const body=await response.json();if(!response.ok)throw new Error(body.error||'请求失败');return body;
}
function download(name,data){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function action(fn){if(busy)return;busy=true;$('status').className='';$('status').textContent='正在处理…';try{await fn();$('status').textContent='操作完成。';}catch(e){$('status').className='error';$('status').textContent=e.message;}finally{busy=false;}}
async function baselines(){const runs=await api('/api/workspace/experiments');const previous=$('baseline').value;$('baseline').replaceChildren();for(const r of runs.filter(r=>r.status==='completed'))$('baseline').add(new Option(`${r.id.slice(0,10)} · ${r.request.factor_ids.length} 个因子 · ${r.created_at}`,r.id));if([...$('baseline').options].some(o=>o.value===previous))$('baseline').value=previous;}
async function refresh(){
 const jobs=await api('/api/lab/jobs');$('jobs').innerHTML=table(['编号','任务','状态','已累计计算秒数'],jobs.map(j=>[j.id.slice(0,10),j.goal,states[j.status],j.elapsed_seconds.toFixed(1)]));
 $('jobs').querySelectorAll('tbody tr').forEach((tr,i)=>{const td=document.createElement('td'),b=document.createElement('button');b.className='secondary';b.textContent='查看';b.onclick=()=>action(async()=>{selected=jobs[i].id;context=null;$('context').value='';$('download-package').disabled=true;await detail();});td.append(b);tr.append(td);});
 if(selected)await detail();
}
async function detail(){
 const id=selected,record=await api('/api/lab/jobs/'+id);if(id!==selected)return;job=record;$('detail').hidden=false;
 $('job-info').textContent=`${job.id} · ${states[job.status]}${job.error?' · '+job.error:''}`;
 const p=job.progress,c=job.payload;
 $('job-progress').textContent=job.kind==='research'?`轮次 ${p.rounds}/${c.max_rounds} · 候选 ${p.candidates}/${c.max_candidates} · 连续无改进 ${p.no_improvement}/${c.patience} · 累计计算 ${job.elapsed_seconds.toFixed(1)}/${job.timeout_seconds} 秒 · 最佳实验 ${p.best_id}`:`${job.kind==='history'?'历史下载 '+(p.downloaded_symbols??0)+'/'+(p.total_symbols??'?')+' 只':'单次实验'} · ${job.elapsed_seconds.toFixed(1)}/${job.timeout_seconds} 秒`;
 $('pause').disabled=job.kind!=='research'||terminal.includes(job.status)||job.status==='paused'||!!job.stop_requested;
 $('resume').disabled=job.status!=='paused';$('cancel').disabled=terminal.includes(job.status)||!!job.stop_requested;
 $('retry').disabled=!['failed','cancelled','timed_out','interrupted'].includes(job.status);
 $('manual').hidden=job.kind!=='research';$('import').disabled=!['waiting_input','paused'].includes(job.status);
 const labels={improved:'改善（待独立验证）',not_improved:'未改善',rejected:'拒绝',failed:'实验失败',stopped_by_proposal:'提案要求停止'};
 $('outcomes').innerHTML=table(['轮次','提案','公式','结果','上涨比例','平均净收益','最差净收益','样本数','均值差（百分点）','原因'],(p.outcomes||[]).map(o=>[o.round,o.proposal.name,o.proposal.expression,labels[o.status]||o.status,pct(o.selected?.positive_rate),pct(o.selected?.mean_return),pct(o.selected?.worst_return),o.selected?.observations??'—',o.comparison?(o.comparison.difference_right_minus_left.mean_return*100).toFixed(3):'—',o.error||o.proposal.rationale]));
 $('job-json').textContent=JSON.stringify(job,null,2);
}
$('session-form').onsubmit=e=>{e.preventDefault();action(async()=>{const j=await api('/api/lab/sessions',{baseline_id:$('baseline').value,goal:$('goal').value,max_rounds:Number($('rounds').value),max_candidates:Number($('candidates').value),patience:Number($('patience').value),timeout_seconds:Number($('timeout').value),min_mean_improvement:Number($('mean').value)/100,max_tail_deterioration:Number($('tail').value)/100});selected=j.id;context=null;$('context').value='';$('download-package').disabled=true;await refresh();$('detail').scrollIntoView({behavior:'smooth'});});};
for(const name of ['pause','resume','cancel'])$(name).onclick=()=>action(async()=>{await api('/api/lab/control',{job_id:selected,action:name});await refresh();});
$('retry').onclick=()=>action(async()=>{const j=await api('/api/lab/retry',{job_id:selected});selected=j.id;context=null;$('context').value='';$('download-package').disabled=true;await refresh();});
$('package').onclick=()=>action(async()=>{context=await api('/api/lab/jobs/'+selected+'/package');$('context').value=JSON.stringify(context,null,2);$('download-package').disabled=false;});
$('download-package').onclick=()=>{if(context)download('quantify-chatgpt-context.json',context);};
$('export-job').onclick=()=>{if(job)download('quantify-job.json',job);};
$('import').onclick=()=>action(async()=>{await api('/api/lab/proposals',{job_id:selected,proposals:$('proposals').value});context=null;$('context').value='运行后请重新生成协作包。';$('download-package').disabled=true;await refresh();});
$('refresh').onclick=()=>action(async()=>{await baselines();await refresh();});
$('proposals').value=JSON.stringify({action:'propose',name:'动量与低波动假设',expression:'rank(momentum(12, 1)) - rank(volatility(6, 0))',rationale:'检验较长趋势与近期低波动组合是否改善开发窗结果；不是收益承诺。'},null,2);
action(async()=>{await baselines();await refresh();});
setInterval(async()=>{if(busy||polling||document.hidden)return;polling=true;try{await refresh();}catch(e){$('status').textContent=e.message;$('status').className='error';}finally{polling=false;}},1500);
