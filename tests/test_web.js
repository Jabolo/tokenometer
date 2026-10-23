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
function runtime(payload=saved,fetcher=()=>new Promise(()=>{})){
  const nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,attributes:{},addEventListener(){},setAttribute(name,value){this.attributes[name]=String(value);}});return nodes.get(id);};
  const context=vm.createContext({Intl,Date,Map,Set,console,fixture:structuredClone(payload),setInterval:()=>0,requestAnimationFrame:fn=>fn(),document:{getElementById:node,querySelectorAll:()=>[],addEventListener(){}},fetch:fetcher});
  vm.runInContext(fs.readFileSync(path.join(root,'web/app.js'),'utf8'),context);
  vm.runInContext('data=fixture',context);
  return {context,run:code=>vm.runInContext(code,context),node};
}
test('saved export passes browser validation before it can replace the current view',()=>{
  assert.equal(runtime().run('validPayload(data)===data'),true);
});
function withAgentMetrics(){
  const payload=structuredClone(saved);
  payload.generated_at='2026-10-23T09:30:00Z';
  payload.agents={
    codex:{count:1731,observed_at:'2026-10-23T09:29:47.000000Z',cutoff:'2026-10-23T09:25:47Z',execution_status:'complete',conflicted_sessions:0},
    antigravity:{count:703,observed_at:'2026-10-23T09:29:47Z',cutoff:null,execution_status:'partial',conflicted_sessions:2},
  };
  return payload;
}
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
  app.run("data.providers.codex.latest_cutoff='2026-10-21T22:30:00Z'; platform='codex'");
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
