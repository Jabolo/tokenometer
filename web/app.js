'use strict';
const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('en-US');
const short = new Intl.NumberFormat('en-US', {notation:'compact', maximumFractionDigits:2});
const dateFormat = new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});
const svgNS = 'http://www.w3.org/2000/svg';
let data, platform='all', chosenDate=null, plotted=[];
const dateObject = text => new Date(text+'T12:00:00Z');
const dateText = text => dateFormat.format(dateObject(text));
const dayString = value => value.toISOString().slice(0,10);
const addDays = (text,offset) => {const value=dateObject(text);value.setUTCDate(value.getUTCDate()+offset);return dayString(value);};
const selectedProviders = () => platform==='all' ? ['codex','antigravity'] : [platform];
function svg(name, attrs={}) {const node=document.createElementNS(svgNS,name);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,String(value));return node;}
function label(node,text) {const title=svg('title');title.textContent=text;node.append(title);}
function validPayload(value) {
  if(!value || value.schema_version!==1 || !Array.isArray(value.daily) || !value.providers || !value.quality)throw Error('Invalid aggregate data');
  for(const provider of ['codex','antigravity']) {
    const source=value.providers[provider];
    if(!source || !source.totals || !Number.isSafeInteger(source.totals.total) || source.totals.total<0)throw Error('Invalid provider totals');
  }
  for(const row of value.daily)if(!/^\d{4}-\d{2}-\d{2}$/.test(row.date)||!['codex','antigravity'].includes(row.platform)||!Number.isSafeInteger(row.total)||row.total<0)throw Error('Invalid daily data');
  return value;
}
function dailyRows() {
  const result=new Map();
  for(const row of data.daily) {
    if(!selectedProviders().includes(row.platform))continue;
    if(!result.has(row.date))result.set(row.date,{date:row.date,codex:0,antigravity:0,total:0});
    const day=result.get(row.date);day[row.platform]+=row.total;day.total+=row.total;
  }
  return result;
}
function selectedTotals() {
  const result={fresh_input:0,cache_read:0,cache_write:0,output:0,total:0};
  for(const name of selectedProviders())for(const category of Object.keys(result))result[category]+=data.providers[name].totals[category];
  return result;
}
function chosenCutoff() {return selectedProviders().map(name=>data.providers[name].latest_cutoff).filter(Boolean).sort().at(-1);}
function timeText(value) {
  if(!value)return 'not yet read';
  return new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:data.timezone,timeZoneName:'short'}).format(new Date(value));
}
function showDay(text,rows) {
  const day=rows.get(text)||{total:0,codex:0,antigravity:0};
  $('selected-date').textContent=dateText(text);
  $('selected-total').textContent=number.format(day.total)+' recorded tokens';
  $('selected-tools').textContent=platform==='all' ? `Codex ${short.format(day.codex)} · Antigravity ${short.format(day.antigravity)}` : '';
  document.querySelectorAll('.day-bar').forEach(node=>{
    const selected=node.dataset.date===text;
    node.classList.toggle('is-selected',selected);
    node.setAttribute('tabindex',selected?'0':'-1');
    node.setAttribute('aria-pressed',String(selected));
  });
}
function renderBars(rows) {
  const known=[...rows.keys()].sort();const end=known.at(-1)||data.last_recorded_date||data.generated_at.slice(0,10);
  const range=$('range').value;
  const start=range==='all' ? known[0]||end : addDays(end,1-Number(range));
  const days=[];for(let day=start;day<=end;day=addDays(day,1))days.push(day);
  plotted=days;
  const max=Math.max(1,...days.map(day=>rows.get(day)?.total||0));
  const chart=$('bars');chart.replaceChildren();
  const description=svg('desc');description.textContent=`Daily recorded volume from ${dateText(start)} to ${dateText(end)}. Use arrow keys between days.`;chart.append(description);
  for(const y of [20,110,200])chart.append(svg('line',{x1:0,x2:1000,y1:y,y2:y,class:'gridline'}));
  const width=1000/days.length, gap=Math.min(3,width*.25);
  days.forEach((day,index)=>{
    const values=rows.get(day)||{codex:0,antigravity:0,total:0};const group=svg('g',{'class':'day-bar','data-date':day,'tabindex':-1,'role':'button','aria-label':`${dateText(day)}: ${number.format(values.total)} recorded tokens`});
    let y=210;
    for(const name of ['codex','antigravity']) {
      const height=values[name] ? Math.max(.6,values[name]/max*190) : 0;
      y-=height;
      group.append(svg('rect',{x:index*width+gap/2,y,width:Math.max(.2,width-gap),height,class:'bar-'+name,rx:1}));
    }
    group.append(svg('rect',{x:index*width,y:0,width,height:215,class:'hit'}));
    group.addEventListener('pointerenter',()=>showDay(day,rows));
    group.addEventListener('focus',()=>showDay(day,rows));
    group.addEventListener('click',()=>{chosenDate=day;showDay(day,rows);});
    group.addEventListener('keydown',event=>{
      if(event.key==='Enter'||event.key===' '){event.preventDefault();chosenDate=day;showDay(day,rows);}
      const step=event.key==='ArrowRight'?1:event.key==='ArrowLeft'?-1:0;
      if(step){event.preventDefault();chart.querySelectorAll('.day-bar')[Math.max(0,Math.min(days.length-1,index+step))].focus();}
    });
    chart.append(group);
  });
  $('range-start').textContent=dateText(start);$('range-end').textContent=dateText(end);
  $('scale-label').textContent=`Peak ${short.format(max===1 && ![...rows.values()].some(day=>day.total)?0:max)} / day`;
  $('chart-empty').hidden=[...rows.values()].some(day=>day.date>=start&&day.date<=end&&day.total);
  chosenDate=days.includes(chosenDate)?chosenDate:end;showDay(chosenDate,rows);
  $('legend').querySelectorAll('span').forEach((node,index)=>node.hidden=!selectedProviders().includes(['codex','antigravity'][index]));
}
function renderCalendar(rows) {
  const end=[...rows.keys()].sort().at(-1)||data.last_recorded_date||data.generated_at.slice(0,10);
  const start=addDays(end,-364);const offset=(dateObject(start).getUTCDay()+6)%7;
  const positive=[...rows.entries()].filter(([day,value])=>day>=start&&day<=end&&value.total>0).map(([,value])=>value.total).sort((a,b)=>a-b);
  const thresholds=[.25,.5,.75].map(q=>positive[Math.floor((positive.length-1)*q)]||0);
  const chart=$('calendar');chart.replaceChildren();chart.setAttribute('viewBox','0 0 1000 160');
  label(chart,`${positive.length} days with recorded tokens in the last 365 days. Brighter squares indicate more recorded tokens.`);
  const weeks=Math.ceil((365+offset)/7), step=960/weeks, size=Math.min(14,step-3);let lastMonth=-1;
  for(let i=0;i<365;i++) {
    const day=addDays(start,i);const value=rows.get(day)?.total||0;
    const column=Math.floor((i+offset)/7);const row=(i+offset)%7;
    const level=value===0?0:1+thresholds.filter(t=>value>t).length;
    const rect=svg('rect',{x:35+column*step,y:25+row*18,width:size,height:14,rx:2,class:'cell-'+level});
    label(rect,`${dateText(day)}: ${number.format(value)} recorded tokens`);chart.append(rect);
    const month=dateObject(day).getUTCMonth();
    if(month!==lastMonth && dateObject(day).getUTCDate()===1) {
      const text=svg('text',{x:35+column*step,y:13,class:'calendar-label'});text.textContent=new Intl.DateTimeFormat('en-GB',{month:'short',timeZone:'UTC'}).format(dateObject(day));chart.append(text);lastMonth=month;
    }
  }
  for(const [text,row] of [['M',0],['W',2],['F',4]]) {const node=svg('text',{x:5,y:36+row*18,class:'calendar-label'});node.textContent=text;chart.append(node);}
  requestAnimationFrame(()=>{const scroller=document.querySelector('.calendar-scroll');scroller.scrollLeft=scroller.scrollWidth-scroller.clientWidth;});
  $('calendar-subtitle').textContent=`Last 365 days · ${positive.length} days with recorded tokens. Brighter means more use.`;
}
function render() {
  const totals=selectedTotals();const rows=dailyRows();const dates=[...rows.keys()].sort();
  $('main-count').textContent=short.format(totals.total);$('exact-count').textContent=number.format(totals.total)+' tokens observed across saved local history';
  $('main-count').classList.remove('number-change');requestAnimationFrame(()=>$('main-count').classList.add('number-change'));
  $('first-date').textContent=dates.length?dateText(dates[0]):'Not dated';$('active-days').textContent=number.format(dates.length);
  const partial=data.quality.coverage_status==='partial'||selectedProviders().some(name=>data.providers[name].coverage_status!=='qualified');
  $('coverage-label').textContent=partial?'Recorded local history · partial coverage':'Recorded local history';
  const cutoff=chosenCutoff();
  const cutoffs=selectedProviders().map(name=>data.providers[name].latest_cutoff);
  $('collected-label').textContent=platform==='all' && new Set(cutoffs).size>1 ? selectedProviders().map(name=>data.providers[name].label+' '+timeText(data.providers[name].latest_cutoff)).join(' · ') : cutoff?'Latest read '+timeText(cutoff):'No source read yet';
  $('new-context').textContent=short.format(totals.fresh_input+totals.cache_write);$('reused-context').textContent=short.format(totals.cache_read);$('generated-output').textContent=short.format(totals.output);
  const undated=totals.total-[...rows.values()].reduce((sum,value)=>sum+value.total,0);
  $('undated-note').textContent=undated?`${short.format(undated)} recorded tokens have no reliable day. They count above, but stay out of the charts.`:'Only dated records appear in the charts. Some local history may still be missing.';
  $('provider-status').replaceChildren();
  for(const name of selectedProviders()) {
    const source=data.providers[name];const p=document.createElement('p');
    p.textContent=`${source.label}: latest read ${timeText(source.latest_cutoff)} · ${source.execution_status==='unobserved'?'not yet observed':source.execution_status==='unknown'?'read diagnostics unavailable':source.execution_status==='partial'?'some sources unreadable':'read finished'}${source.source_errors?` · ${source.source_errors} source errors`:''}.`;
    $('provider-status').append(p);
  }
  if(data.quality.conflicted_events) {const p=document.createElement('p');p.textContent=`${number.format(data.quality.conflicted_events)} conflicting event identities are held out of the counter for review.`;$('provider-status').append(p);}
  $('export-date').textContent=`Saved view generated ${timeText(data.generated_at)}. Day boundaries: ${data.timezone}.`;
  renderBars(rows);renderCalendar(rows);
}
async function load() {
  $('reload').disabled=true;
  try {
    const response=await fetch('data.json',{cache:'no-store'});if(!response.ok)throw Error('Missing export');
    const next=validPayload(await response.json());data=next;render();$('content').hidden=false;$('load-state').hidden=true;
  } catch(error) {
    $('load-state').hidden=false;$('load-state').className='load-state error';
    $('load-state').textContent=data?'Could not reload the saved data. The previous view is still shown. Try again in a moment.':'No saved usage data yet. Run the local refresh command, then reload this page.';
  } finally {$('reload').disabled=false;}
}
document.querySelectorAll('[data-platform]').forEach(button=>button.addEventListener('click',()=>{
  platform=button.dataset.platform;document.querySelectorAll('[data-platform]').forEach(node=>node.setAttribute('aria-pressed',String(node===button)));if(data)render();
}));
$('range').addEventListener('change',()=>{if(data)renderBars(dailyRows());});$('reload').addEventListener('click',load);
load();
