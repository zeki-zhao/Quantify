'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const split=value=>value.split(/[\s,，]+/).filter(Boolean);
const table=(heads,rows)=>`<table><thead><tr>${heads.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
const labels={no_data:'暂无报价',fresh:'交易时段近期报价',stale:'行情已过期',session_closed:'非交易时段最近报价',fetch_failed:'获取失败（保留旧报价）',queued:'排队中',running:'下载中',succeeded:'下载完成',failed:'下载失败',timed_out:'超时',cancelled:'已取消',interrupted:'已中断'};
const categories={network_policy:'代理阻止连接',network:'网络连接失败',timeout:'请求超时',http:'接口返回错误',response:'数据格式异常'};
let state=null,busy=false,polling=false;
async function api(path,payload){const response=await fetch('/api/market/'+path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const body=await response.json();if(!response.ok)throw new Error(body.error||'请求失败');return body;}
async function action(task){if(busy)return;busy=true;$('status').textContent='正在处理…';$('status').className='';try{await task();$('status').textContent='操作完成。';}catch(e){$('status').textContent=e.message;$('status').className='error';}finally{busy=false;}}
async function refresh(fill=false){state=await api('state');if(fill){const c=state.config;$('symbols').value=c.symbols.join(', ');$('quotes-enabled').value=String(c.quotes_enabled);$('interval').value=c.interval_seconds;$('history-symbols').value=c.history_symbols.join(', ');$('history-start').value=c.history_start;$('auto-daily').value=String(c.auto_daily);}
 $('quote-info').textContent=`${state.calendar_notice}。最新查询 ${state.last_attempt.attempted_at||'尚未查询'}${state.last_attempt.category?' · '+(categories[state.last_attempt.category]||'查询失败'):''}`;
 const firstError=Object.values(state.last_attempt.errors||{})[0];if(firstError)$('quote-info').textContent+='。'+firstError;
 $('quotes').innerHTML=table(['代码','名称','最新价（元）','涨跌幅','日内累计成交量（股）','行情时间','接收时间','状态','错误'],state.quotes.map(row=>[row.symbol,row.quote?.name||'—',row.quote?.price??'—',row.quote?row.quote.change_percent.toFixed(2)+'%':'—',row.quote?.volume??'—',row.quote?.quote_at||'—',row.quote?.received_at||'—',labels[row.state],row.error||'']));
 const h=state.history;$('history-info').textContent=`${labels[h.status]||'尚未下载'}${h.job_id?' · 任务 '+h.job_id.slice(0,10):''}${state.latest_snapshot_id?' · 最近快照 '+state.latest_snapshot_id.slice(0,10):''}${state.latest_snapshot_summary?' · '+state.latest_snapshot_summary.date_range.join(' 至 ')+' · '+state.latest_snapshot_summary.row_count+' 行':''}${h.error?' · '+h.error:''}`;
 $('history-json').textContent=JSON.stringify({history:h,latest_snapshot_id:state.latest_snapshot_id,schedule:state.history_schedule,last_auto_day:state.last_auto_day,service_error:state.service_error},null,2);
}
$('config-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('config',{symbols:split($('symbols').value),quotes_enabled:$('quotes-enabled').value==='true',interval_seconds:Number($('interval').value),history_symbols:split($('history-symbols').value),history_start:$('history-start').value,auto_daily:$('auto-daily').value==='true'});await refresh(true);});};
$('refresh-quotes').onclick=()=>action(async()=>{await api('refresh',{});await refresh();});
$('download').onclick=()=>action(async()=>{if(!state.config.history_symbols.length)throw new Error('请先保存历史股票列表与起点');const mode=$('history-mode').value;await api('history',{symbols:state.config.history_symbols,start_date:state.config.history_start,end_date:$('history-end').value,parent_id:mode==='new'?null:state.latest_snapshot_id,full_refresh:mode==='full'});await refresh();});
const china=new Date(Date.now()+8*3600000);const end=new Date(china);if(end.getUTCHours()<16||(end.getUTCHours()===16&&end.getUTCMinutes()<30))end.setUTCDate(end.getUTCDate()-1);while([0,6].includes(end.getUTCDay()))end.setUTCDate(end.getUTCDate()-1);$('history-end').value=end.toISOString().slice(0,10);
action(()=>refresh(true));setInterval(async()=>{if(busy||polling||document.hidden)return;polling=true;try{await refresh();}catch(e){$('status').textContent=e.message;$('status').className='error';}finally{polling=false;}},2000);
