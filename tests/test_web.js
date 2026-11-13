'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const {createHash}=require('node:crypto');
const root=path.join(__dirname,'..');
const saved=JSON.parse(fs.readFileSync(path.join(root,'web/data.json'),'utf8'));
test('asset URLs change with their contents so cached old code cannot outlive new HTML',()=>{
  const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
  for(const name of ['app.js','styles.css']){
    const version=createHash('sha256').update(fs.readFileSync(path.join(root,'web',name))).digest('hex').slice(0,12);
    assert.ok(html.includes(`${name}?v=${version}"`),`${name} URL must use its current content hash`);
  }
});
function runtime(payload=saved,fetcher=()=>new Promise(()=>{}),zone){
  const nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,attributes:{},addEventListener(){},setAttribute(name,value){this.attributes[name]=String(value);}});return nodes.get(id);};
  const browserIntl=zone?{NumberFormat:Intl.NumberFormat,DateTimeFormat:function(locale,options){return new Intl.DateTimeFormat(locale,{timeZone:zone,...options});}}:Intl;
  const context=vm.createContext({Intl:browserIntl,Date,Map,Set,console,fixture:structuredClone(payload),setInterval:()=>0,requestAnimationFrame:fn=>fn(),document:{getElementById:node,querySelectorAll:()=>[],addEventListener(){}},fetch:fetcher});
  vm.runInContext(fs.readFileSync(path.join(root,'web/app.js'),'utf8'),context);
  vm.runInContext('data=fixture',context);
  return {context,run:code=>vm.runInContext(code,context),node};
}
test('saved export passes browser validation before it can replace the current view',()=>{
  assert.equal(runtime().run('validPayload(data)===data'),true);
});
function withSupplement(){
  const payload=structuredClone(saved);
  payload.timezone='UTC';
  payload.supplemental_daily=[{date:'2026-01-01',total:12345}];
  return payload;
}
test('supplemental totals appear in dated Codex usage without inventing a breakdown',()=>{
  const app=runtime(withSupplement());
  assert.equal(app.run('validPayload(data)===data'),true);
  const base=saved.providers.codex.totals;
  app.run("platform='codex'");
  assert.equal(app.run("dailyRows().get('2026-01-01').total"),12345);
  assert.equal(app.run('selectedTotals().total'),base.total+12345);
  for(const key of ['fresh_input','cache_read','cache_write','output'])assert.equal(app.run(`selectedTotals().${key}`),base[key]);
  assert.equal(app.run('selectedTotals().total-[...dailyRows().values()].reduce((sum,row)=>sum+row.total,0)'),0);
  app.run("platform='all'");
  assert.equal(app.run('selectedTotals().total'),saved.totals.total+12345);
  app.run("platform='antigravity'");
  assert.equal(app.run("dailyRows().has('2026-01-01')"),false);
  assert.equal(app.run('selectedTotals().total'),saved.providers.antigravity.totals.total);
  const shared=withSupplement();
  const other=shared.daily.find(r=>r.platform==='antigravity'&&!shared.daily.some(s=>s.platform==='codex'&&s.date===r.date));
  shared.supplemental_daily[0].date=other.date;
  const combined=runtime(shared);
  assert.equal(combined.run('validPayload(data)===data'),true);
  assert.equal(combined.run(`dailyRows().get('${other.date}').total`),other.total+12345);
  assert.equal(combined.run('selectedTotals().total-[...dailyRows().values()].reduce((sum,row)=>sum+row.total,0)'),shared.quality.undated_totals.total);
});
test('supplements reject overlap, incomplete days, unsafe data and ambiguous local coverage',()=>{
  for(const mutate of [
    p=>{p.timezone='Europe/Warsaw';},
    p=>{p.supplemental_daily={};},
    p=>{p.supplemental_daily[0].date='2026-02-30';},
    p=>{p.supplemental_daily[0].date='9999-12-31';},
    p=>{p.supplemental_daily[0].date=p.daily.find(r=>r.platform==='codex').date;},
    p=>{p.supplemental_daily.push({...p.supplemental_daily[0]});},
    p=>{p.supplemental_daily[0].total=0;},
    p=>{p.supplemental_daily[0].total=Number.MAX_SAFE_INTEGER;},
    p=>{p.supplemental_daily[0].account='private';},
    p=>{p.supplemental_daily[0].date=p.generated_at.slice(0,10);},
    p=>{p.providers.codex.last_successful_cutoff='2026-01-01T23:59:59Z';},
    p=>{p.providers.codex.last_successful_cutoff=null;p.providers.codex.execution_status='partial';},
    p=>{p.providers.codex.undated_events=1;},
    p=>{p.quality.conflicted_events=1;},
  ]){
    const payload=withSupplement();mutate(payload);
    assert.throws(()=>runtime(payload).run('validPayload(data)'),/Invalid aggregate data/);
  }
  const boundary=withSupplement();boundary.providers.codex.last_successful_cutoff='2026-01-02T00:00:00Z';
  assert.equal(runtime(boundary).run('validPayload(data)===data'),true);
});
function withAgentMetrics(source=saved){
  const payload=structuredClone(source);
  // These scenarios pin a historical clock independently of later reports.
  delete payload.supplemental_daily;
  // Pin the comparison's successful read; latest cutoffs still cover saved daily rows.
  for(const provider of Object.values(payload.providers)){
    provider.last_successful_cutoff='2026-10-23T09:25:47Z';
  }
  payload.generated_at='2026-10-23T09:30:00Z';
  payload.agents={
    codex:{count:1731,observed_at:'2026-10-23T09:29:47.000000Z',cutoff:'2026-10-23T09:25:47Z',execution_status:'complete',conflicted_sessions:0},
    antigravity:{count:703,observed_at:'2026-10-23T09:29:47Z',cutoff:null,execution_status:'partial',conflicted_sessions:2},
  };
  return payload;
}
test('agent scenarios stay deterministic when daily exports advance',()=>{
  for(const day of ['2026-10-24','2026-10-25','2027-01-01']){
    const rolling=structuredClone(saved);
    for(const provider of Object.values(rolling.providers)){
      provider.latest_cutoff=provider.last_successful_cutoff=`${day}T08:00:00Z`;
    }
    const scenario=withAgentMetrics(rolling);
    const app=runtime(scenario);app.run('renderAgentStats()');
    assert.equal(app.node('codex-agent-meta').hidden,true);
    assert.equal(app.node('antigravity-agent-meta').textContent,'Partial data · 2 conflicts excluded');
    assert.equal(rolling.providers.codex.latest_cutoff,`${day}T08:00:00Z`);
    assert.equal(rolling.providers.codex.last_successful_cutoff,`${day}T08:00:00Z`);
  }
});
test('legacy exports without agent observations remain valid and agent fields stay provider-scoped',()=>{
  const legacy=structuredClone(saved);delete legacy.agents;
  assert.equal(runtime(legacy).run('validPayload(data)===data'),true);
  assert.equal(runtime(withAgentMetrics()).run('validPayload(data)===data'),true);
});
test('agent observations require exact keys, integer-or-null counts, UTC timestamps, and provider cutoff semantics',()=>{
  for(const mutate of [
    p=>{p.agents.extra={};},
    p=>{delete p.agents.codex.count;},
    p=>{p.agents.codex.extra=false;},
    p=>{p.agents.codex.count='1,731';},
    p=>{p.agents.codex.conflicted_sessions=-1;},
    p=>{p.agents.codex.observed_at='2026-10-23T09:29:47+00:00';},
    p=>{p.agents.codex.observed_at='2026-02-30T09:29:47Z';},
    p=>{p.agents.codex.cutoff=null;},
    p=>{p.agents.codex.cutoff='2026-10-23T09:30:00Z';},
    p=>{p.generated_at='2026-10-23T09:29:46Z';},
    p=>{p.agents.antigravity.cutoff='2026-10-23T09:29:47Z';},
    p=>{p.agents.codex.execution_status='unknown';},
    p=>{p.agents.antigravity.execution_status='unobserved';},
    p=>{p.agents.codex.count=null;},
    p=>{p.agents.codex.execution_status='partial';p.agents.codex.count=null;},
  ]){
    const payload=withAgentMetrics();mutate(payload);
    assert.throws(()=>runtime(payload).run('validPayload(data)'),/Invalid aggregate data/);
  }
  const unobserved=withAgentMetrics();
  unobserved.agents.antigravity={count:null,observed_at:null,cutoff:null,execution_status:'unobserved',conflicted_sessions:0};
  assert.equal(runtime(unobserved).run('validPayload(data)===data'),true);
  const partialRetained=withAgentMetrics();partialRetained.agents.codex.execution_status='partial';
  assert.equal(runtime(partialRetained).run('validPayload(data)===data'),true);
  const partialBeforeFirstRead=withAgentMetrics();
  partialBeforeFirstRead.agents.codex={count:null,observed_at:null,cutoff:null,execution_status:'partial',conflicted_sessions:0};
  assert.equal(runtime(partialBeforeFirstRead).run('validPayload(data)===data'),true);
});
test('agent metrics display exact values, their own observation time, and provider filtering',()=>{
  const app=runtime(withAgentMetrics());
  app.run("data=fixture;platform='all';$('range').value='30';renderAgentStats()");
  assert.equal(app.node('agent-stats').hidden,false);
  assert.equal(app.node('codex-agent-count').textContent,'1,731');
  assert.equal(app.node('antigravity-agent-count').textContent,'703');
  assert.equal(app.node('codex-agent-meta').hidden,true);
  assert.equal(app.node('codex-agent-meta').textContent,'');
  assert.ok(app.node('codex-agent-count').title.includes(app.run('timeText(data.agents.codex.observed_at)')));
  assert.ok(app.node('codex-agent-detail').textContent.includes(app.run('timeText(data.agents.codex.cutoff)')));
  assert.equal(app.node('agent-details').hidden,false);
  assert.equal(app.node('antigravity-agent-meta').textContent,'Partial data · 2 conflicts excluded');
  app.run("$('range').value='365';renderAgentStats()");
  assert.equal(app.node('codex-agent-count').textContent,'1,731');
  app.run("platform='codex';renderAgentStats()");
  assert.equal(app.node('codex-agent-stat').hidden,false);
  assert.equal(app.node('antigravity-agent-stat').hidden,true);
  app.run("platform='antigravity';renderAgentStats()");
  assert.equal(app.node('codex-agent-stat').hidden,true);
  assert.equal(app.node('antigravity-agent-stat').hidden,false);
});
test('absent agent metrics hide the row and null counts render as unknown rather than zero',()=>{
  const legacy=structuredClone(saved);delete legacy.agents;
  const app=runtime(legacy);
  app.run('data=fixture;renderAgentStats()');
  assert.equal(app.node('agent-stats').hidden,true);
  const metrics=withAgentMetrics();metrics.agents.antigravity={count:null,observed_at:null,cutoff:null,execution_status:'partial',conflicted_sessions:0};
  const observed=runtime(metrics);
  observed.run('data=fixture;renderAgentStats()');
  assert.equal(observed.node('antigravity-agent-count').textContent,'—');
  assert.equal(observed.node('antigravity-agent-meta').textContent,'Partial data · unavailable');
  assert.match(observed.node('antigravity-agent-detail').textContent,/no usable observation yet/);
});
test('routine agent dates stay in details while older independent snapshots remain visible',()=>{
  const payload=withAgentMetrics();
  payload.agents.antigravity.execution_status='complete';payload.agents.antigravity.conflicted_sessions=0;
  const app=runtime(payload);app.run('renderAgentStats()');
  for(const name of ['codex','antigravity'])assert.equal(app.node(`${name}-agent-meta`).hidden,true);
  app.run("data.agents.codex.observed_at='2026-10-22T09:29:47Z';data.agents.codex.cutoff='2026-10-22T09:25:47Z';renderAgentStats()");
  assert.equal(app.node('codex-agent-meta').textContent,'Older snapshot');
  assert.equal(app.node('codex-agent-meta').hidden,false);
  app.run("data.agents.codex.observed_at='2026-10-23T09:29:47Z';data.agents.codex.cutoff='2026-10-23T09:25:47Z';renderAgentStats()");
  assert.equal(app.node('codex-agent-meta').hidden,true);
  app.run("platform='antigravity';renderAgentStats()");
  assert.equal(app.node('codex-agent-detail').hidden,true);
  assert.equal(app.node('antigravity-agent-detail').hidden,false);
});
test('malformed agent reload preserves the previous assigned data and displayed count',async()=>{
  const good=withAgentMetrics();
  const malformed=withAgentMetrics();malformed.agents.codex.cutoff=null;
  let requests=0;
  const app=runtime(good,()=>++requests===1?new Promise(()=>{}):Promise.resolve({ok:true,json:async()=>malformed}));
  app.node('main-count').textContent='previous view';
  await app.run('load()');
  assert.equal(app.run('data===fixture'),true);
  assert.equal(app.node('main-count').textContent,'previous view');
  assert.equal(app.node('load-state').className,'load-state error');
});
test('completed-day window follows the saved cutoff in its timezone, across DST',()=>{
  const app=runtime();
  assert.equal(app.run("localDay('2026-10-24T22:30:00Z','Europe/Warsaw')"),'2026-10-25');
  assert.equal(app.run("localDay('2026-10-25T23:30:00Z','Europe/Warsaw')"),'2026-10-26');
  app.run("data.timezone='Europe/Warsaw';data.providers.codex.latest_cutoff='2026-10-21T22:30:00Z'; platform='codex'");
  assert.equal(app.run('windowEnd()'),'2026-10-22');
  assert.equal(app.run('recentActivity(dailyRows()).end'),'2026-10-21');
  assert.equal(app.run('recentActivity(dailyRows()).start'),'2026-09-22');
});
test('provider filter uses its own saved read, not last activity or current time',()=>{
  const app=runtime();
  app.run("data.providers.codex.latest_cutoff='2026-10-22T13:00:00Z';data.providers.antigravity.latest_cutoff='2026-10-18T13:00:00Z';platform='antigravity'");
  assert.equal(app.run('windowEnd()'),'2026-10-18');
  assert.equal(app.run('recentActivity(dailyRows()).end'),'2026-10-17');
  app.run("platform='all'");
  assert.equal(app.run('windowEnd()'),'2026-10-22');
});
test('zero-token recorded events count as a recorded day; absent rows remain absent',()=>{
  const app=runtime();
  app.run("data.daily=[{date:'2026-10-20',platform:'codex',total:0,events:1}];data.providers.codex.latest_cutoff='2026-10-22T13:00:00Z';platform='codex'");
  assert.equal(app.run('recentActivity(dailyRows()).days'),1);
  assert.equal(app.run("dailyRows().has('2026-10-21')"),false);
  assert.equal(app.run("dailyRows().get('2026-10-20').observed.includes('antigravity')"),false);
});
test('day details distinguish an absent record from an observed zero',()=>{
  const app=runtime();
  app.run("data.daily=[{date:'2026-10-20',platform:'codex',total:0,events:1}]; data.providers.codex.latest_cutoff='2026-10-22T13:00:00Z'; platform='codex'; plotted=['2026-10-20','2026-10-21']; showDay('2026-10-20',dailyRows())");
  assert.equal(app.node('selected-total').textContent,'0 recorded tokens');
  app.run("showDay('2026-10-21',dailyRows())");
  assert.equal(app.node('selected-total').textContent,'No saved record for this UTC day');
  assert.equal(app.node('selected-date').textContent,'21 Oct 2026 · UTC');
});
test('no saved source read gives an unknown recent-use statistic',()=>{
  const app=runtime();
  app.run('data.providers.codex.latest_cutoff=null;data.providers.antigravity.latest_cutoff=null');
  assert.equal(app.run('recentActivity(dailyRows())'),null);
});
test('freshness uses calendar days in the dataset timezone, including DST',()=>{
  const app=runtime();
  for(const [cutoff,now,expected] of [
    ['2026-10-22T22:30:00Z','2026-10-23T08:00:00Z','Updated today'],
    ['2026-10-22T21:59:00Z','2026-10-22T22:01:00Z','Updated yesterday'],
    ['2026-10-24T22:30:00Z','2026-10-25T23:30:00Z','Updated yesterday'],
    ['2026-03-28T23:30:00Z','2026-03-29T22:30:00Z','Updated yesterday'],
    ['2026-10-20T13:00:00Z','2026-10-23T08:00:00Z','Updated 3 days ago'],
    ['2026-10-23T12:00:00Z','2026-10-23T08:00:00Z','Source read time is in the future'],
  ])assert.equal(app.run(`relativeReadTime('${cutoff}',new Date('${now}'),'Europe/Warsaw')`),expected);
});
test('new exports and failed attempts do not make old provider data appear fresh',()=>{
  const app=runtime();
  app.run("data.generated_at='2026-10-23T08:00:00Z';data.providers.codex.last_successful_cutoff='2026-10-20T13:00:00Z';data.providers.codex.latest_cutoff='2026-10-23T07:00:00Z';data.providers.codex.execution_status='partial';data.providers.antigravity.last_successful_cutoff='2026-10-22T13:00:00Z'");
  assert.equal(app.run("freshnessSummary(new Date('2026-10-23T08:00:00Z'))"),'Updated 3 days ago');
  app.run("platform='antigravity'");
  assert.equal(app.run("freshnessSummary(new Date('2026-10-23T08:00:00Z'))"),'Updated yesterday');
});
test('unobserved and never-successful sources have no invented update date',()=>{
  const app=runtime();
  app.run("data.providers.codex.last_successful_cutoff=null;data.providers.codex.execution_status='partial';platform='codex'");
  assert.equal(app.run('freshnessSummary()'),'No successful source read yet');
  app.run("platform='all';data.providers.antigravity.last_successful_cutoff='2026-10-22T13:00:00Z'");
  assert.equal(app.run('freshnessSummary()'),'Some sources have no successful read yet');
});
test('malformed reload exports fail before data assignment or rendering',()=>{
  for(const mutate of [
    p=>{p.timezone='Invalid/Zone';},
    p=>{delete p.timezone;},
    p=>{p.daily[0].date='2026-02-30';},
    p=>{p.daily.push(p.daily[0]);},
    p=>{delete p.providers.codex.totals.output;},
    p=>{p.providers.codex.latest_cutoff='invalid';},
    p=>{p.quality.undated_totals.total++;},
    p=>{p.totals.total++;},
    p=>{p.providers.codex.execution_status='invented';},
    p=>{p.providers.codex.events--;p.providers.antigravity.events++;},
    p=>{p.quality.coverage_status='qualified';},
  ]){
    const payload=structuredClone(saved);mutate(payload);
    assert.throws(()=>runtime(payload).run('validPayload(data)'),/Invalid aggregate data/);
  }
});

test('history visibility diagnostics retain missingness and reject private or impossible claims',()=>{
  const value=structuredClone(saved);
  value.history_audit={codex:{retained_events_not_in_latest:1,retained_tokens_not_in_latest:120},antigravity:{retained_events_not_in_latest:0,retained_tokens_not_in_latest:0}};
  value.providers.codex.coverage_status='partial';value.quality.coverage_status='partial';
  assert.equal(runtime(value).run('validPayload(data)===data'),true);
  for(const mutate of [p=>p.history_audit.codex.account='private',p=>p.history_audit.codex.retained_events_not_in_latest=-1,p=>p.history_audit.codex.retained_tokens_not_in_latest=p.totals.total+1,p=>p.providers.codex.coverage_status='qualified']){
    const bad=structuredClone(value);mutate(bad);assert.throws(()=>runtime(bad).run('validPayload(data)'),/Invalid aggregate data/);
  }
});
test('visible freshness includes precise oldest successful read rather than export time',()=>{
  const app=runtime();
  app.run("data.providers.codex.last_successful_cutoff='2026-11-09T08:16:21Z';data.providers.antigravity.last_successful_cutoff='2026-11-10T09:00:00Z';renderFreshness()");
  assert.match(app.node('collected-label').textContent,/through/);
  assert.ok(app.node('collected-label').textContent.includes(app.run("timeText('2026-11-09T08:16:21Z')")));
});

test('travelling changes the visitor read time but never dataset dates, totals or missing-day meaning',()=>{
  const results=[];
  for(const zone of ['Europe/Warsaw','America/Los_Angeles','Asia/Tokyo','Pacific/Kiritimati']) {
    const app=runtime(saved,undefined,zone);
    app.run("data.timezone='UTC'; data.providers.codex.latest_cutoff='2026-10-23T22:35:02Z'; platform='codex'; plotted=['2026-10-23','2026-10-24']; showDay('2026-10-24',dailyRows())");
    results.push(app.run('JSON.stringify({rows:[...dailyRows()],total:selectedTotals().total,end:windowEnd(),basis:dayBasisText()})'));
    assert.equal(app.node('selected-date').textContent,'24 Oct 2026 · UTC');
    assert.equal(app.node('selected-total').textContent,'No saved record for this UTC day');
    assert.ok(app.run("visitorTimeText('2026-10-23T22:35:02Z')").includes(`(${zone})`));
  }
  assert.equal(new Set(results).size,1);
});
test('visitor read time uses the date-specific daylight-saving offset and handles midnight',()=>{
  const app=runtime();
  assert.match(app.run("visitorTimeText('2026-10-23T22:35:02Z','Europe/Warsaw')"),/24 Oct 2026.*00:35:02.*GMT\+2/);
  assert.match(app.run("visitorTimeText('2026-10-25T22:35:02Z','Europe/Warsaw')"),/25 Oct 2026.*23:35:02.*GMT\+1/);
  assert.match(app.run("visitorTimeText('2026-03-29T01:30:00Z','Europe/Warsaw')"),/03:30:00.*GMT\+2/);
  assert.match(app.run("visitorTimeText('2026-10-23T22:35:02Z','Invalid/Zone')"),/UTC.*local time unavailable/);
});
test('freshness labels separate dataset clock from visitor clock and hide local time without a complete read',()=>{
  const app=runtime(saved,undefined,'Asia/Tokyo');
  app.run("renderFreshness()");
  assert.match(app.node('collected-label').textContent,/\(UTC\).*through.*UTC/);
  assert.match(app.node('local-read-time').textContent,/Source read in your time:.*Asia\/Tokyo/);
  app.run('data.providers.codex.latest_cutoff=null;data.providers.codex.last_successful_cutoff=null;renderFreshness()');
  assert.equal(app.node('local-read-time').hidden,true);
  assert.equal(app.node('local-read-time').textContent,'');
});
