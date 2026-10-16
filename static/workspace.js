'use strict';
const $ = id => document.getElementById(id);
const esc = text => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const short = id => id.slice(0, 10);
const pct = value => `${(value * 100).toFixed(2)}%`;
const table = (heads, rows) => `<table><thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${esc(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
let snapshots = [], factors = [], runs = [], currentData = null, currentRun = null;
let busy = false;
const kindNames = {daily:'日线',execution:'执行状态',monthly:'月度总回报',monthly_price:'月度价格收益',financials:'财务',universe:'股票池'};
const help = {
  daily:'date,symbol,open,high,low,close,volume,total_return_index,available_at。价格单位元/股，成交量为股；total_return_index 可留空，留空时不能生成总回报研究。日期为 YYYY-MM-DD，日线可用时间按当日收盘之后解释。',
  monthly:'date,symbol,total_return_index,quality,value,available_at。含分红指数必填；财务列可留空，但只能用于动量/低波动。完整研究需要连续月度和固定股票池，封存 12 个月时，3/6/12 月持有期分别至少需要 86/92/104 个月。',
  monthly_price:'date,symbol,close,quality,value,available_at。仅未复权价格；收益不含现金分红，拆股/送转/配股跳变未调整。财务列可留空；未提供时只能选价格类因子。',
  financials:'period_end,symbol,quality,value,available_at。质量与价值是由你计算并声明口径的因子，数值越大越好；修订使用新的实际披露日期。由于只有日期，财务数据在披露日之后才用于收盘信号。',
  execution:'date,symbol,suspended,limit_up,limit_down,no_price_limit,available_at。suspended/no_price_limit 为0或1；限价为元。无价格限制须显式声明并留空上下限，否则上下限必填。available_at 表示该日开盘前已知（由导入者声明）。',
  universe:'date,symbol,member,available_at。date 为生效日期，member=1 加入、0 退出；必须包含初始股票池成员。历史事件可保存和查询，动态池研究尚不支持。'
};
async function api(path, payload) {
  const response = await fetch('/api/workspace/' + path, payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '请求失败');
  return body;
}
async function queuedExperiment(payload) {
  const response=await fetch('/api/lab/experiments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  let job=await response.json();
  if(!response.ok) throw new Error(job.error);
  while(['queued','running'].includes(job.status)) {
    $('status').textContent=`后台任务 ${short(job.id)} · ${job.status}。可在 AI 研究室查看或取消，离开页面不影响计算。`;
    await new Promise(resolve=>setTimeout(resolve,500));
    const polled=await fetch('/api/lab/jobs/'+job.id);
    job=await polled.json();
    if(!polled.ok) throw new Error(job.error);
  }
  if(job.result?.experiment_id) return api('experiments/'+job.result.experiment_id);
  throw new Error(job.error || '任务已停止：'+job.status);
}
function download(filename, body) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(body,null,2)],{type:'application/json'}));
  const link = document.createElement('a'); link.href=url; link.download=filename; link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function action(task) {
  if (busy) return;
  busy=true;
  const controls=[...document.querySelectorAll('button,input,select')];
  controls.forEach(control=>control.disabled=true);
  $('status').className=''; $('status').textContent='正在处理…';
  try { await task(); $('status').textContent='操作完成。'; }
  catch(error) { $('status').className='error'; $('status').textContent=error.message; }
  finally { busy=false; controls.forEach(control=>control.disabled=false); }
}
function options(id, items, label, blank=null) {
  const previous=$(id).value;
  $(id).replaceChildren();
  if (blank !== null) $(id).add(new Option(blank,''));
  for (const item of items) $(id).add(new Option(label(item),item.id));
  if ([...$(id).options].some(option=>option.value===previous)) $(id).value=previous;
}
const snapshotLabel = item => `${kindNames[item.kind]} · ${item.is_demo?'合成':'导入声明'} · ${short(item.id)} · ${item.row_count} 行`;
const factorLabel = item => `${item.name} · ${short(item.id)}`;
function describeImport() {
  $('template').href=`/api/workspace/templates/${$('kind').value}.csv`;
  $('schema-help').textContent=help[$('kind').value];
}
async function refresh() {
  [snapshots,factors,runs] = await Promise.all([api('snapshots'),api('factors'),api('experiments')]);
  $('summary').innerHTML=`<span>${snapshots.length} 个数据快照</span><span>${factors.length} 个因子版本</span><span>${runs.length} 次实验（含失败）</span>`;
  $('snapshots').innerHTML=table(['快照','类型','性质','日期范围','行数','来源','质量提示'],snapshots.map(s=>[short(s.id),kindNames[s.kind],s.is_demo?'合成':'用户声明真实',s.date_range.join(' 至 '),s.row_count,s.source,s.warnings.join(' ')]));
  $('snapshots').querySelectorAll('tbody tr').forEach((row,index)=>{
    const cell=document.createElement('td'), button=document.createElement('button');
    button.type='button';button.className='secondary';button.textContent='详情';
    button.onclick=()=>action(async()=>showData(await api('snapshots/'+snapshots[index].id)));
    cell.append(button);row.append(cell);
  });
  options('parent',snapshots,snapshotLabel,'新数据集');
  options('asof-snapshot',snapshots,snapshotLabel);
  options('daily-snapshot',snapshots.filter(s=>s.kind==='daily'),snapshotLabel);
  options('finance-snapshot',snapshots.filter(s=>s.kind==='financials'),snapshotLabel,'不使用财务因子');
  options('universe-snapshot',snapshots.filter(s=>s.kind==='universe'),snapshotLabel,'使用日线全部代码（固定池）');
  options('run-snapshot',snapshots.filter(s=>['monthly','monthly_price'].includes(s.kind)),snapshotLabel);
  options('factor-parent',factors,factorLabel);
  $('factor-list').innerHTML=factors.map(f=>`<div class="factor-card"><h3>${esc(f.name)}</h3><code>${short(f.id)}</code><p>${f.kind==='expression'?esc(f.expression):`窗口 ${f.lookback} 月 · 跳过 ${f.skip} 月`}</p><p>${esc(f.reason)}</p><p class="muted">${f.parent_id?'父版本 '+short(f.parent_id):'内置基线'}</p></div>`).join('');
  const checked=new Set([...document.querySelectorAll('#factor-options input:checked')].map(i=>i.value));
  $('factor-options').innerHTML=factors.map(f=>`<label><input type="checkbox" value="${f.id}" ${checked.has(f.id)?'checked':''}>${esc(factorLabel(f))}</label>`).join('');
  $('runs').innerHTML=table(['实验','状态','数据快照','因子数量','时间'],runs.map(r=>[short(r.id),r.status==='completed'?'已完成':'失败',short(String(r.request.snapshot_id)),r.request.factor_ids?.length??0,r.created_at]));
  $('runs').querySelectorAll('tbody tr').forEach((row,index)=>{
    const cell=document.createElement('td'),button=document.createElement('button');button.type='button';button.className='secondary';button.textContent='查看';
    button.onclick=()=>action(async()=>showRun(await api('experiments/'+runs[index].id)));
    cell.append(button);row.append(cell);
  });
  const successful=runs.filter(r=>r.status==='completed');
  options('compare-left',successful,r=>`${short(r.id)} · ${r.request.factor_ids.length} 个因子`);
  options('compare-right',successful,r=>`${short(r.id)} · ${r.request.factor_ids.length} 个因子`);
}
function showData(data) {
  currentData=data;$('data-details').hidden=false;$('data-details').open=true;
  $('data-meta').textContent=(data.warnings?.join(' ') || '时点查询结果')+' 下方预览最多显示 30 行，下载包含完整数据。';
  $('data-json').textContent=JSON.stringify(Array.isArray(data)?data.slice(0,30):{...data,raw_import:'完整原始输入包含在 JSON 下载中',rows:data.rows?.slice(0,30)},null,2);
}
function showRun(run) {
  currentRun=run;$('result').hidden=false;$('result-json').textContent=JSON.stringify(run,null,2);
  $('result-metrics').replaceChildren();$('result-folds').replaceChildren();
  if (run.status!=='completed') { $('result-note').textContent='实验失败并已留档：'+run.error;return; }
  const report=run.report;
  $('result-note').textContent=`${report.status}。封存起点 ${run.protocol.holdout_start}，未计算封存期收益。${run.protocol.note}`;
  $('result-metrics').innerHTML=table(['分组','上涨比例','平均单笔净收益','最差单笔净收益','样本数'],[['因子筛选',report.selected],['同期基准',report.baseline]].map(([name,s])=>[name,pct(s.positive_rate),pct(s.mean_return),pct(s.worst_return),s.observations]));
  const names=Object.fromEntries(report.factor_versions.map(f=>[f.id,f.name]));
  $('result-folds').innerHTML=table(['测试信号','最后退出','所选因子','平均单笔收益'],report.folds.map(f=>[f.test_signal_start,f.test_last_exit,names[f.chosen_factor]||'全池',pct(f.selected.mean_return)]));
}
$('kind').onchange=()=>{
  const kind=$('kind').value;
  $('basis').value=kind==='monthly'?'total_return_index':['daily','monthly_price'].includes(kind)?'unadjusted':'not_applicable';describeImport();
};
$('parent').onchange=()=>{
  const parent=snapshots.find(s=>s.id===$('parent').value);if(!parent)return;
  $('kind').value=parent.kind;$('basis').value=parent.price_basis;$('is-demo').value=String(parent.is_demo);$('source').value=parent.source;describeImport();
};
$('factor-parent').onchange=()=>{
  const parent=factors.find(f=>f.id===$('factor-parent').value);if(!parent)return;
  $('lookback').value=parent.lookback;$('skip').value=parent.skip;$('factor-expression').value=parent.expression||'';
};
$('demo').onclick=()=>action(async()=>{await api('demo',{});await refresh();});
$('import-form').onsubmit=event=>{event.preventDefault();action(async()=>{
  const file=$('csv-file').files[0];if(!file||file.size>8000000)throw new Error('请选择不超过 8 MB 的 CSV 文件');
  const snapshot=await api('import',{kind:$('kind').value,raw_csv:await file.text(),source:$('source').value,price_basis:$('basis').value,is_demo:$('is-demo').value==='true',parent_id:$('parent').value||null});
  await refresh();showData(await api('snapshots/'+snapshot.id));
});};
$('monthly-form').onsubmit=event=>{event.preventDefault();action(async()=>{
  await api('monthly',{daily_id:$('daily-snapshot').value,financials_id:$('finance-snapshot').value||null,universe_id:$('universe-snapshot').value||null,return_basis:$('return-basis').value});await refresh();
});};
$('asof-form').onsubmit=event=>{event.preventDefault();action(async()=>showData(await api('as-of',{snapshot_id:$('asof-snapshot').value,cutoff:$('asof-date').value})));};
$('factor-form').onsubmit=event=>{event.preventDefault();action(async()=>{
  await api('factors',{parent_id:$('factor-parent').value,name:$('factor-name').value,reason:$('reason').value,expression:$('factor-expression').value.trim()||null,lookback:Number($('lookback').value),skip:Number($('skip').value)});await refresh();
});};
$('run-form').onsubmit=event=>{event.preventDefault();action(async()=>{
  const factor_ids=[...document.querySelectorAll('#factor-options input:checked')].map(input=>input.value);
  if(!factor_ids.length||factor_ids.length>10)throw new Error('请选择 1 至 10 个因子版本');
  const run=await queuedExperiment({request:{snapshot_id:$('run-snapshot').value,factor_ids,horizon:Number($('horizon').value),top_k:Number($('top-k').value),cost_bps:Number($('cost').value),holdout_months:Number($('holdout').value),objective:{primary:$('objective').value,min_improvement:Number($('objective-min').value)/100,max_mean_deterioration:Number($('objective-mean').value)/100,max_tail_deterioration:Number($('objective-tail').value)/100,min_observations:Number($('objective-samples').value)}}});
  await refresh();showRun(run);if(run.status==='failed')throw new Error('实验失败并已保存：'+run.error);
});};
$('compare-form').onsubmit=event=>{event.preventDefault();action(async()=>{
  const result=await api('compare',{left_id:$('compare-left').value,right_id:$('compare-right').value});
  const delta=result.difference_right_minus_left;
  $('comparison').innerHTML=table(['平均收益差','上涨比例差','最差收益差','样本数差'],[[pct(delta.mean_return),pct(delta.positive_rate),pct(delta.worst_return),delta.observations]])+`<p>${esc(result.note)}</p>`;
});};
$('replay').onclick=()=>action(async()=>{const run=await queuedExperiment({replay_id:currentRun.id});await refresh();showRun(run);if(run.status==='failed')throw new Error(run.error);});
$('download-data').onclick=()=>{if(currentData)download('quantify-snapshot.json',currentData);};
$('download-run').onclick=()=>{if(currentRun)download('quantify-experiment.json',currentRun);};
describeImport();action(refresh);
