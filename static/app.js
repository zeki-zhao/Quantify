'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (value, digits=2) => value == null ? '不适用' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits});
const pct = value => value == null ? '不适用' : `${fmt(value*100)}%`;
const num = id => Number($(id).value);
const table = (headers, rows) => `<table><thead><tr>${headers.map(h=>`<th>${escapeHTML(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${escapeHTML(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
let stocks = [], selected, analysis, research;
const factorNames = {momentum:'动量',quality:'盈利质量',value:'低估值',low_volatility:'低波动',combined:'等权组合'};

async function api(url, payload) {
  const response = await fetch(url, payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}
function scenarioInputs() {
  const basis = $('basis').value;
  const metric = selected.metrics[basis === 'pev' ? 'embedded_value' : 'net_profit'];
  const amount = metric.observations.at(-1).value;
  const implied = selected.price.value * selected.shares.value / amount;
  const notes = basis === 'pev' ? ['EV承压，估值折价扩大','EV温和增长，倍数不变','EV较快增长，折价收窄'] : ['利润下降，估值倍数收缩','利润持平，估值倍数不变','利润恢复，估值倍数扩张'];
  $('amount-label').textContent = basis === 'pev' ? '内含价值 EV（亿元）' : '归母净利润（亿元）';
  $('scenarios').innerHTML = ['悲观','中性','乐观'].map((label,i)=>`<tr><td>${label}</td><td><input aria-label="${label}利润或EV" id="amount-${i}" type="number" step="any" min="-1000000" max="1000000" value="${(amount*[.7,1,1.3][i]).toFixed(2)}" required></td><td><input aria-label="${label}估值倍数" id="multiple-${i}" type="number" step="any" min="0" max="1000" value="${(implied*[.8,1,1.2][i]).toFixed(2)}" required></td><td><input class="note-input" aria-label="${label}假设说明" id="note-${i}" maxlength="1000" value="${notes[i]}（演示假设）" required></td></tr>`).join('');
}
function chooseStock() {
  selected = stocks.find(s => s.symbol === $('stock').value);
  $('price').value = selected.price.value; $('shares').value = selected.shares.value;
  $('basis').querySelector('[value="pev"]').disabled = selected.kind !== 'insurance';
  $('basis').value = selected.kind === 'insurance' ? 'pev' : 'pe';
  for (const [id,key] of [['mu','mu'],['sigma','sigma'],['dividend','dividend_yield']]) $(id).value = selected.defaults[key]*100;
  $('paths').value = selected.defaults.paths; $('seed').value = selected.defaults.seed;
  $('stock-meta').textContent = `${selected.name} ${selected.symbol} · 合成价格 ${selected.price.value} 元/股 · 快照 ${selected.as_of} · ${selected.price.adjustment} · 来源：${selected.source}`;
  $('fundamentals').innerHTML = table(['指标','单位','2023-12-31','2024-12-31','2025-12-31','口径说明'], Object.values(selected.metrics).map(m=>[m.label,m.unit,...m.observations.map(o=>fmt(o.value)),m.note || '合成演示数值']));
  $('fund-note').textContent = selected.kind === 'insurance' ? '保险企业使用营运利润、NBV、EV与偿付能力等专用指标，不套用工业企业库存/现金流质量比率。可用日期为合成占位，非真实公告日。历史分位未计算。' : '观察归母/扣非利润趋势、经营现金流与利润匹配、库存增长及净负债。比率在计算结果中展示，不生成综合评分或胜率。可用日期为合成占位。历史分位未计算。';
  scenarioInputs(); invalidateAnalysis();
}
function invalidateAnalysis() { analysis = null; $('results').hidden = true; $('status').textContent = '参数已更新，请重新计算。'; }
function invalidateResearch() { research = null; $('research-result').hidden = true; $('research-status').textContent = '输入已更新，请重新运行。'; }
function renderChart() {
  if (!analysis) return;
  const sim = analysis.report.simulation, kind = $('return-kind').value, month = $('chart-month').value;
  const summary = sim.horizons[month][kind], hist = summary.histogram;
  const n = sim.parameters.paths, max = Math.max(...hist.counts)/n;
  const w=880,h=260,left=65,right=22,top=24,bottom=48,pw=w-left-right,ph=h-top-bottom;
  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${month}个月${kind==='price'?'价格收益':'含分红总回报'}分布直方图"><title>期末收益分布，未经校准</title>`;
  for(let i=0;i<=4;i++) {const y=top+ph-i*ph/4;svg+=`<line x1="${left}" x2="${w-right}" y1="${y}" y2="${y}" stroke="#e1e8e1"/><text x="${left-8}" y="${y+4}" text-anchor="end" font-size="11" fill="#607166">${fmt(max*i/4*100,1)}%</text>`;}
  hist.counts.forEach((count,i)=>{const bh=count/n/max*ph;svg+=`<rect x="${left+i*pw/32+1}" y="${top+ph-bh}" width="${pw/32-2}" height="${bh}" fill="#398568"><title>${pct(hist.edges[i])} 至 ${pct(hist.edges[i+1])}：${pct(count/n)}</title></rect>`;});
  for(let i=0;i<=4;i++){const index=i*8;svg+=`<text x="${left+pw*i/4}" y="${h-22}" text-anchor="middle" font-size="11" fill="#607166">${pct(hist.edges[index])}</text>`;}
  $('chart').innerHTML=svg+'</svg>';
  $('risk-table').innerHTML = table(['期限','平均收益','P5','P50','P95','期末上涨','期末下跌','期末持平','触及−10%','触及−20%','触及−30%','触及−40%'],Object.entries(sim.horizons).map(([m,kinds])=>{const s=kinds[kind];return [`${m}个月`,pct(s.mean),pct(s.quantiles['0.05']),pct(s.quantiles['0.5']),pct(s.quantiles['0.95']),pct(s.p_up),pct(s.p_down),pct(s.p_flat),...['10','20','30','40'].map(t=>pct(s.p_touch[t]))];}));
  $('sampling').textContent = `当前口径：${kind==='price'?'价格收益':'含分红总回报'}。${n} 条路径；单一概率估计最大抽样标准误差约 ${fmt(summary.max_mc_standard_error*100)} 个百分点，不含模型误差。0%/100% 不代表不可能/必然。`;
}
function download(name, content, mime) { const blob=new Blob([content],{type:mime});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000); }
function exportResult(result, prefix, kind) {if(!result)return;download(`${prefix}.${kind}`,kind==='json'?JSON.stringify(result.report,null,2):'\ufeff'+result.csv,kind==='json'?'application/json':'text/csv;charset=utf-8');}

$('analysis-form').addEventListener('input',invalidateAnalysis);
$('analysis-form').addEventListener('change',invalidateAnalysis);
$('stock').addEventListener('change',chooseStock);
$('basis').addEventListener('change',()=>{scenarioInputs();invalidateAnalysis();});
$('chart-month').addEventListener('change',renderChart);$('return-kind').addEventListener('change',renderChart);
$('analysis-form').addEventListener('submit',async event=>{
  event.preventDefault();const payload={symbol:selected.symbol,price:num('price'),shares:num('shares'),basis:$('basis').value,scenarios:[0,1,2].map(i=>({amount:num(`amount-${i}`),multiple:num(`multiple-${i}`),note:$(`note-${i}`).value})),simulation:{mu:num('mu')/100,sigma:num('sigma')/100,dividend_yield:num('dividend')/100,paths:num('paths'),seed:num('seed')}};
  const controls = [...$('analysis-form').querySelectorAll('input,select,button'),$('stock')];controls.forEach(c=>c.disabled=true);$('status').textContent='正在计算…';
  try { analysis=await api('/api/analyze',payload);const r=analysis.report;$('results').hidden=false;
    $('result-meta').textContent=`${r.stock.name} · 起点 ${r.user_inputs.price} 元 · 股本 ${r.user_inputs.shares} 亿股 · ${r.generated_at} · ${r.warning}`;
    $('valuation-result').innerHTML=table(['情景','目标价格（元）','价格收益','假设说明'],r.valuation.results.map(s=>[s.scenario,fmt(s.target_price),pct(s.price_return),s.warning||s.note]));
    $('valuation-result').innerHTML+= '<h3>基本面派生指标（演示快照价格/股本，非上方用户修改值）</h3>'+table(['指标','值'],Object.entries(r.fundamentals).map(([k,v])=>[k,k.includes('同比')||k==='资产负债率'?pct(v):fmt(v)]));
    const {horizons,...metadata}=r.simulation;$('model-meta').textContent=JSON.stringify(metadata,null,2);renderChart();$('status').textContent='完成；修改输入后需重新计算。';
  }catch(error){$('status').textContent=error.message;}finally{controls.forEach(c=>c.disabled=false);}
});
$('research-data').addEventListener('change',()=>{$('csv-fields').hidden=$('research-data').value!=='csv';});
$('research-form').addEventListener('input',invalidateResearch);$('research-form').addEventListener('change',invalidateResearch);
$('research-form').addEventListener('submit',async event=>{
  event.preventDefault();const controls=[...$('research-form').querySelectorAll('input,select,button')];controls.forEach(c=>c.disabled=true);$('research-status').textContent='正在进行滚动样本外实验…';
  try{const payload={horizon:num('research-horizon'),top_k:num('top-k'),cost_bps:num('cost-bps')};
    if($('research-data').value==='csv'){const file=$('csv-file').files[0];if(!file)throw new Error('请选择 CSV 文件。');if(file.size>1500000)throw new Error('文件不能超过 1.5 MB。');payload.csv_text=await file.text();payload.source=$('csv-source').value;if(!payload.source.trim())throw new Error('请填写数据来源和分红/复权口径。');}
    research=await api('/api/research',payload);const r=research.report;$('research-result').hidden=false;
    $('research-warning').textContent=`${r.status}。样本外上涨比例差：${fmt(r.positive_rate_difference*100)} 个百分点（可能为负，不保证提升）。数据：${r.source}；窗口 ${r.data_window.join(' 至 ')}。`;
    $('research-summary').innerHTML=table(['样本外分组','上涨比例','平均持有期净收益','最差单笔净收益','股票×入场月样本数'],[['因子筛选',r.selected],['同池未筛选基准',r.baseline]].map(([name,s])=>[name,pct(s.positive_rate),pct(s.mean_return),pct(s.worst_return),s.observations]));
    $('research-folds').innerHTML=table(['训练最后标签','测试信号起点','测试最后退出','选定因子','筛选上涨比例','基准上涨比例'],r.folds.map(f=>[f.train_last_label,f.test_signal_start,f.test_last_exit,factorNames[f.chosen_factor],pct(f.selected.positive_rate),pct(f.baseline.positive_rate)]));
    $('research-method').textContent=r.method+' '+r.limitations;$('research-status').textContent='实验完成。';
  }catch(error){$('research-status').textContent=error.message;}finally{controls.forEach(c=>c.disabled=false);}
});
$('export-json').addEventListener('click',()=>exportResult(analysis,'stock-analysis','json'));$('export-csv').addEventListener('click',()=>exportResult(analysis,'stock-analysis','csv'));
$('research-json').addEventListener('click',()=>exportResult(research,'factor-research','json'));$('research-csv').addEventListener('click',()=>exportResult(research,'factor-research','csv'));
api('/api/stocks').then(data=>{stocks=data;$('stock').innerHTML=stocks.map(s=>`<option value="${escapeHTML(s.symbol)}">${escapeHTML(s.name)}</option>`).join('');chooseStock();}).catch(error=>{$('stock-meta').textContent=`加载失败：${error.message}`;});
