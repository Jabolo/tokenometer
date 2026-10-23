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
  const fail=()=>{throw Error('Invalid aggregate data');};
  const count=n=>Number.isSafeInteger(n)&&n>=0;
  const timestamp=s=>typeof s==='string'&&Number.isFinite(Date.parse(s))&&/Z$/.test(s);
  const utcTimestamp=s=>typeof s==='string'&&/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?Z$/.test(s)&&Number.isFinite(Date.parse(s))&&new Date(s).toISOString().slice(0,10)===s.slice(0,10);
  const exactKeys=(object,keys)=>object&&typeof object==='object'&&!Array.isArray(object)&&Object.keys(object).sort().join('|')===keys.slice().sort().join('|');
  const day=s=>typeof s==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(s)&&Number.isFinite(+dateObject(s))&&dayString(dateObject(s))===s;
  const totals=t=>t&&['fresh_input','cache_read','cache_write','output','total'].every(k=>count(t[k]))&&t.total===t.fresh_input+t.cache_read+t.cache_write+t.output;
  if(!value || value.schema_version!==1 || !Array.isArray(value.daily) || !value.providers || !value.quality || !timestamp(value.generated_at))fail();
  try{new Intl.DateTimeFormat('en-GB',{timeZone:value.timezone}).format();}catch{fail();}
  if(typeof value.timezone!=='string'||!totals(value.totals)||!count(value.events))fail();
  for(const provider of ['codex','antigravity']) {
    const source=value.providers[provider];
    if(!source || !totals(source.totals) || !count(source.events) || !count(source.undated_events) || !count(source.source_errors) || typeof source.label!=='string' || !['complete','partial','unknown','unobserved'].includes(source.execution_status) || !['qualified','partial','unknown','unobserved'].includes(source.coverage_status))fail();
    for(const key of ['latest_cutoff','latest_capture_completed_at','last_successful_cutoff'])if(source[key]!==null&&!timestamp(source[key]))fail();
  }
  if(Object.prototype.hasOwnProperty.call(value,'agents')) {
    if(!exactKeys(value.agents,['codex','antigravity']))fail();
    const agentKeys=['count','observed_at','cutoff','execution_status','conflicted_sessions'];
    for(const name of ['codex','antigravity']) {
      const metric=value.agents[name];
      if(!exactKeys(metric,agentKeys)||(metric.count!==null&&!count(metric.count))||(metric.observed_at!==null&&!utcTimestamp(metric.observed_at))||(metric.cutoff!==null&&!utcTimestamp(metric.cutoff))||!['complete','partial','unobserved'].includes(metric.execution_status)||!count(metric.conflicted_sessions))fail();
      if(name==='antigravity'&&metric.cutoff!==null)fail();
      if(metric.execution_status==='complete') {
        if(metric.count===null||!utcTimestamp(metric.observed_at)||Date.parse(metric.observed_at)>Date.parse(value.generated_at)||(name==='codex'&&(!utcTimestamp(metric.cutoff)||Date.parse(metric.cutoff)>Date.parse(metric.observed_at))))fail();
      } else if(metric.execution_status==='unobserved') {
        if(metric.count!==null||metric.observed_at!==null||metric.cutoff!==null||metric.conflicted_sessions!==0)fail();
      } else if(metric.count===null) {
        if(metric.observed_at!==null||metric.cutoff!==null||metric.conflicted_sessions!==0)fail();
      } else if(!utcTimestamp(metric.observed_at)||Date.parse(metric.observed_at)>Date.parse(value.generated_at)||(name==='codex'&&(!utcTimestamp(metric.cutoff)||Date.parse(metric.cutoff)>Date.parse(metric.observed_at))))fail();
    }
  }
  const seen=new Set();
  for(const row of value.daily){
    if(!row||!day(row.date)||!['codex','antigravity'].includes(row.platform)||!totals(row)||!count(row.events)||!row.events||seen.has(row.date+row.platform))fail();
    seen.add(row.date+row.platform);
    if(value.providers[row.platform].latest_cutoff&&row.date>localDay(value.providers[row.platform].latest_cutoff,value.timezone))fail();
  }
  const q=value.quality;
  if(!['qualified','partial'].includes(q.coverage_status)||!count(q.conflicted_events)||!count(q.undated_events)||!count(q.source_errors)||!totals(q.undated_totals)||!totals(q.dated_totals))fail();
  const providers=Object.values(value.providers);
  if(value.events!==providers.reduce((s,p)=>s+p.events,0)||value.events!==value.daily.reduce((s,r)=>s+r.events,0)+q.undated_events)fail();
  if(q.undated_events!==providers.reduce((s,p)=>s+p.undated_events,0)||q.source_errors!==providers.reduce((s,p)=>s+p.source_errors,0))fail();
  if(q.coverage_status==='qualified'&&(q.source_errors||q.conflicted_events||q.undated_events||providers.some(p=>p.execution_status!=='complete'||p.coverage_status!=='qualified')))fail();
  for(const name of ['codex','antigravity']){
    const provider=value.providers[name],rows=value.daily.filter(row=>row.platform===name);
    if(provider.events!==rows.reduce((s,r)=>s+r.events,0)+provider.undated_events)fail();
    for(const key of ['fresh_input','cache_read','cache_write','output','total'])if(provider.totals[key]<rows.reduce((s,r)=>s+r[key],0))fail();
  }
  for(const key of ['fresh_input','cache_read','cache_write','output','total']){
    if(value.totals[key]!==providers.reduce((s,p)=>s+p.totals[key],0)||value.totals[key]!==q.dated_totals[key]+q.undated_totals[key]||q.dated_totals[key]!==value.daily.reduce((s,r)=>s+r[key],0))fail();
  }
  return value;
}
function localDay(timestamp,zone=data.timezone) {
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(timestamp));
  const get=key=>parts.find(part=>part.type===key).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
function windowEnd() {return localDay(chosenCutoff()||data.generated_at);}
function recentActivity(rows) {
  if(!chosenCutoff())return null;
  const end=addDays(windowEnd(),-1),start=addDays(end,-29);
  return {start,end,days:[...rows.keys()].filter(day=>day>=start&&day<=end).length};
}
function dailyRows() {
  const result=new Map();
  for(const row of data.daily) {
    if(!selectedProviders().includes(row.platform))continue;
    if(!result.has(row.date))result.set(row.date,{date:row.date,codex:0,antigravity:0,total:0,observed:[]});
    const day=result.get(row.date);day[row.platform]+=row.total;day.total+=row.total;day.observed.push(row.platform);
  }
  return result;
}
function selectedTotals() {
  const result={fresh_input:0,cache_read:0,cache_write:0,output:0,total:0};
  for(const name of selectedProviders())for(const category of Object.keys(result))result[category]+=data.providers[name].totals[category];
  return result;
}
function chosenCutoff() {return selectedProviders().map(name=>data.providers[name].latest_cutoff).filter(Boolean).sort().at(-1);}
function successfulCutoff(source) {
  return source.last_successful_cutoff || (source.execution_status==='complete'?source.latest_cutoff:null);
}
function relativeReadTime(cutoff,now=new Date(),zone=data.timezone) {
  if(!cutoff)return 'No successful source read yet';
  if(new Date(cutoff)>now)return 'Source read time is in the future';
  const days=Math.round((Date.parse(localDay(now,zone)+'T00:00:00Z')-Date.parse(localDay(cutoff,zone)+'T00:00:00Z'))/86400000);
  return days===0?'Updated today':days===1?'Updated yesterday':`Updated ${number.format(days)} days ago`;
}
function freshnessSummary(now=new Date()) {
  const reads=selectedProviders().map(name=>successfulCutoff(data.providers[name]));
  if(reads.every(value=>!value))return 'No successful source read yet';
  if(reads.some(value=>!value))return 'Some sources have no successful read yet';
  if(reads.some(value=>new Date(value)>now))return 'Source read time is in the future';
  const oldest=reads.reduce((a,b)=>new Date(a)<new Date(b)?a:b);
  return relativeReadTime(oldest,now);
}
function renderFreshness() {
  $('collected-label').textContent=freshnessSummary();
  $('collected-label').title=selectedProviders().map(name=>`${data.providers[name].label}: successful read ${timeText(successfulCutoff(data.providers[name]))}`).join(' · ');
}
function renderAgentStats() {
  const section=$('agent-stats'),details=$('agent-details');
  section.hidden=details.hidden=!data.agents;
  if(!data.agents)return;
  for(const name of ['codex','antigravity']) {
    const metric=data.agents[name],card=$(`${name}-agent-stat`),detail=$(`${name}-agent-detail`);
    card.hidden=detail.hidden=!selectedProviders().includes(name);
    if(card.hidden)continue;
    const count=$(`${name}-agent-count`);
    count.textContent=metric.count===null?'—':number.format(metric.count);
    const visible=[],precise=[];
    if(metric.execution_status==='unobserved') {
      visible.push('Not yet recorded');precise.push('No usable agent observation yet');
    } else if(metric.count===null) {
      visible.push('Partial data · unavailable');precise.push('Metadata collection was partial; no usable observation yet');
    } else {
      if(metric.execution_status==='partial')visible.push('Partial data');
      const tokenRead=successfulCutoff(data.providers[name]);
      if(tokenRead&&localDay(metric.observed_at)<localDay(tokenRead))visible.push('Older snapshot');
      precise.push(`Last usable metadata read: ${timeText(metric.observed_at)}`);
      if(name==='codex')precise.push(`Sessions through ${timeText(metric.cutoff)}`);
      else precise.push('Recorded parent–child links; creation times are not established');
    }
    if(metric.conflicted_sessions) {
      visible.push(`${number.format(metric.conflicted_sessions)} ${metric.conflicted_sessions===1?'conflict':'conflicts'} excluded`);
      precise.push(`${number.format(metric.conflicted_sessions)} conflicting session identities excluded`);
    }
    const meta=$(`${name}-agent-meta`);
    meta.textContent=visible.join(' · ');meta.hidden=visible.length===0;
    count.title=precise.join(' · ');
    detail.textContent=`${data.providers[name].label} agents: ${precise.join('. ')}.`;
  }
}
function timeText(value) {
  if(!value)return 'not yet read';
  return new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:data.timezone,timeZoneName:'short'}).format(new Date(value));
}
function showDay(text,rows) {
  chosenDate=text;
  const day=rows.get(text);
  $('selected-date').textContent=dateText(text)+(text===windowEnd()?' · partial day':'');
  $('selected-total').textContent=day?number.format(day.total)+' recorded tokens':'No retained record';
  $('selected-tools').textContent=platform==='all'&&day ? ['codex','antigravity'].map(name=>`${data.providers[name].label} ${day.observed.includes(name)?short.format(day[name]):'no record'}`).join(' · ') : '';
  $('previous-day').disabled=plotted.indexOf(text)<=0;
  $('next-day').disabled=plotted.indexOf(text)>=plotted.length-1;
  document.querySelectorAll('.day-bar').forEach(node=>{
    const selected=node.dataset.date===text;
    node.classList.toggle('is-selected',selected);
    node.setAttribute('tabindex',selected?'0':'-1');
    node.setAttribute('aria-pressed',String(selected));
  });
}
function renderBars(rows) {
  const known=[...rows.keys()].sort();const end=windowEnd();
  const range=$('range').value;
  const start=range==='all' ? known[0]||end : addDays(end,1-Number(range));
  const days=[];for(let day=start;day<=end;day=addDays(day,1))days.push(day);
  plotted=days;
  const peak=Math.max(0,...days.map(day=>rows.get(day)?.total||0)),max=Math.max(1,peak);
  const chart=$('bars');chart.replaceChildren();
  const description=svg('desc');description.textContent=`Daily recorded volume from ${dateText(start)} to ${dateText(end)}. Use arrow keys between days.`;chart.append(description);
  for(const y of [20,110,200])chart.append(svg('line',{x1:0,x2:1000,y1:y,y2:y,class:'gridline'}));
  const width=1000/days.length, gap=Math.min(3,width*.25);
  days.forEach((day,index)=>{
    const recorded=rows.has(day),values=rows.get(day)||{codex:0,antigravity:0,total:0};const group=svg('g',{'class':'day-bar','data-date':day,'tabindex':-1,'role':'button','aria-label':`${dateText(day)}: ${recorded?number.format(values.total)+' recorded tokens':'no retained record'}${day===end?', partial day':''}`});
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
  $('scale-label').textContent=`Peak ${short.format(peak)} / day`;
  $('chart-empty').hidden=[...rows.values()].some(day=>day.date>=start&&day.date<=end&&day.total);
  chosenDate=days.includes(chosenDate)?chosenDate:end;showDay(chosenDate,rows);
  $('legend').querySelectorAll('span').forEach((node,index)=>node.hidden=!selectedProviders().includes(['codex','antigravity'][index]));
}
function renderCalendar(rows) {
  const end=windowEnd();
  const start=addDays(end,-364);const offset=(dateObject(start).getUTCDay()+6)%7;
  const positive=[...rows.entries()].filter(([day,value])=>day>=start&&day<=end&&value.total>0).map(([,value])=>value.total).sort((a,b)=>a-b);
  const thresholds=[.25,.5,.75].map(q=>positive[Math.floor((positive.length-1)*q)]||0);
  const chart=$('calendar');chart.replaceChildren();chart.setAttribute('viewBox','0 0 1000 160');
  const recorded=[...rows.keys()].filter(day=>day>=start&&day<=end).length;
  label(chart,`${recorded} days with recorded activity from ${dateText(start)} to ${dateText(end)}. Blank squares mean no retained record. Brighter squares indicate more recorded tokens.`);
  const weeks=Math.ceil((365+offset)/7), step=960/weeks, size=Math.min(14,step-3);let lastMonth=-1;
  for(let i=0;i<365;i++) {
    const day=addDays(start,i);const value=rows.get(day)?.total||0;
    const column=Math.floor((i+offset)/7);const row=(i+offset)%7;
    const level=value===0?0:1+thresholds.filter(t=>value>t).length;
    const rect=svg('rect',{x:35+column*step,y:25+row*18,width:size,height:14,rx:2,class:rows.has(day)&&value===0?'cell-zero':'cell-'+level});
    label(rect,`${dateText(day)}: ${rows.has(day)?number.format(value)+' recorded tokens':'no retained record'}`);chart.append(rect);
    const month=dateObject(day).getUTCMonth();
    if(month!==lastMonth && dateObject(day).getUTCDate()===1) {
      const text=svg('text',{x:35+column*step,y:13,class:'calendar-label'});text.textContent=new Intl.DateTimeFormat('en-GB',{month:'short',timeZone:'UTC'}).format(dateObject(day));chart.append(text);lastMonth=month;
    }
  }
  for(const [text,row] of [['M',0],['W',2],['F',4]]) {const node=svg('text',{x:5,y:36+row*18,class:'calendar-label'});node.textContent=text;chart.append(node);}
  requestAnimationFrame(()=>{const scroller=document.querySelector('.calendar-scroll');scroller.scrollLeft=scroller.scrollWidth-scroller.clientWidth;});
  $('calendar-subtitle').textContent=`${dateText(start)} – ${dateText(end)} · ${recorded} recorded days. Blank means no retained record.`;
}
function render() {
  const totals=selectedTotals();const rows=dailyRows();const dates=[...rows.keys()].sort();
  $('main-count').textContent=short.format(totals.total);$('exact-count').textContent=number.format(totals.total)+' tokens observed across saved local history';
  $('main-count').classList.remove('number-change');requestAnimationFrame(()=>$('main-count').classList.add('number-change'));
  const recent=recentActivity(rows);
  $('recent-days').textContent=recent?number.format(recent.days):'—';
  $('recent-window').textContent=recent?`${dateText(recent.start)} – ${dateText(recent.end)} · recorded activity`:'No source read yet';
  $('first-date').textContent=dates.length?`Earliest retained record: ${dateText(dates[0])}.`:'No dated records retained.';
  $('active-days').textContent=number.format(dates.length);
  renderFreshness();
  renderAgentStats();
  $('new-context').textContent=short.format(totals.fresh_input+totals.cache_write);$('reused-context').textContent=short.format(totals.cache_read);$('generated-output').textContent=short.format(totals.output);
  const undated=totals.total-[...rows.values()].reduce((sum,value)=>sum+value.total,0);
  $('undated-note').hidden=!undated;
  $('undated-note').textContent=undated?`${short.format(undated)} recorded tokens have no reliable day. They count above, but stay out of the charts.`:'';
  $('provider-status').replaceChildren();
  for(const name of selectedProviders()) {
    const source=data.providers[name];const p=document.createElement('p');
    p.textContent=`${source.label}: latest attempt ${timeText(source.latest_cutoff)} · ${source.execution_status==='unobserved'?'not yet observed':source.execution_status==='unknown'?'read diagnostics unavailable':source.execution_status==='partial'?'some sources unreadable':'read finished'} · ${source.coverage_status} coverage${source.source_errors?` · ${source.source_errors} source errors`:''}. Successful read: ${timeText(successfulCutoff(source))}.`;
    $('provider-status').append(p);
  }
  if(data.quality.conflicted_events) {const p=document.createElement('p');p.textContent=`${number.format(data.quality.conflicted_events)} conflicting event identities are held out of the counter for review.`;$('provider-status').append(p);}
  $('export-date').textContent=`Saved view generated ${timeText(data.generated_at)}. Day boundaries: ${data.timezone}.`;
  renderBars(rows);renderCalendar(rows);
}
async function load() {
  $('reload').disabled=true;
  const previous=data;
  try {
    const response=await fetch('data.json',{cache:'no-store'});if(!response.ok)throw Error('Missing export');
    const next=validPayload(await response.json());data=next;render();$('content').hidden=false;$('load-state').hidden=true;
  } catch(error) {
    data=previous;
    $('load-state').hidden=false;$('load-state').className='load-state error';
    $('load-state').textContent=data?'Could not reload the saved data. The previous view is still shown. Try again in a moment.':'No saved usage data yet. Run the local refresh command, then reload this page.';
  } finally {$('reload').disabled=false;}
}
document.querySelectorAll('[data-platform]').forEach(button=>button.addEventListener('click',()=>{
  platform=button.dataset.platform;document.querySelectorAll('[data-platform]').forEach(node=>node.setAttribute('aria-pressed',String(node===button)));if(data)render();
}));
$('range').addEventListener('change',()=>{if(data)renderBars(dailyRows());});$('reload').addEventListener('click',load);
for(const [id,step] of [['previous-day',-1],['next-day',1]])$(id).addEventListener('click',()=>{
  const index=plotted.indexOf(chosenDate)+step;
  if(data&&index>=0&&index<plotted.length)showDay(plotted[index],dailyRows());
});
$('explore-calendar').addEventListener('click',()=>{
  if(!data)return;
  $('range').value='365';renderBars(dailyRows());
  $('bars').querySelector('[tabindex="0"]')?.focus();
});
setInterval(()=>{if(data)renderFreshness();},60000);
document.addEventListener('visibilitychange',()=>{if(data&&!document.hidden)renderFreshness();});
load();
