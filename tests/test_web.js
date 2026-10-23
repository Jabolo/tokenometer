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
function runtime(payload=saved){
  const nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,addEventListener(){}});return nodes.get(id);};
  const context=vm.createContext({Intl,Date,Map,Set,console,fixture:structuredClone(payload),setInterval:()=>0,document:{getElementById:node,querySelectorAll:()=>[],addEventListener(){}},fetch:()=>new Promise(()=>{})});
  vm.runInContext(fs.readFileSync(path.join(root,'web/app.js'),'utf8'),context);
  vm.runInContext('data=fixture',context);
  return {context,run:code=>vm.runInContext(code,context)};
}
test('saved export passes browser validation before it can replace the current view',()=>{
  assert.equal(runtime().run('validPayload(data)===data'),true);
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
