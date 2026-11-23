"""Hermetic discovery UI/endpoint checks, without companion boot or live scans."""
import ast
import base64
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Optional
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ui():
    tree = ast.parse((ROOT / "companion/main.py").read_text())
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "_DISCOVERY_HTML"
                              for t in n.targets))
    return ast.literal_eval(assignment.value)


@pytest.fixture
def endpoints(monkeypatch):
    """Compile only discovery definitions, not main's config/log/profile boot."""
    tree = ast.parse((ROOT / "companion/main.py").read_text())
    start = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_discovery_worker")
    nodes = [n for n in tree.body if n.lineno >= start or
             isinstance(n, ast.FunctionDef) and n.name == "_require_token"]
    scope = dict(app=FastAPI(), CFG={"auth_token": "test-secret"},
                 _DISCOVERY_STATE=dict(running=False, stage="idle"),
                 Depends=Depends, Header=Header, Optional=Optional,
                 HTTPException=HTTPException, HTMLResponse=HTMLResponse,
                 datetime=datetime, timezone=timezone, logger=logging.getLogger(__name__))
    fake = ModuleType("discovery")
    fake.run_scan = Mock()
    fake.load_last = Mock(return_value={"jobs": [], "review_jobs": []})
    fake.load_config = Mock(return_value={"phases": [{"name": "saved"}]})
    fake.save_config = Mock()
    fake.resolve_market = Mock(return_value={"label": "German cities"})
    fake.CONFIG_PATH = Path("not-written.json")
    monkeypatch.setitem(sys.modules, "discovery", fake)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "discovery_only", "exec"), scope)
    thread = Mock()
    monkeypatch.setattr("threading.Thread", thread)
    return TestClient(scope["app"]), scope, fake, thread


def run_js(ui, code, *, extension=False):
    """Use a fake DOM, fetch, timers and hooks; never start a browser/network."""
    if extension:
        source = (ROOT / "extension/popup/popup.js").read_text()
        script = source.split("function verdictClass", 1)[1].split("function App()", 1)[0]
        script = "function verdictClass" + script
    else:
        script = ui.split("<script>", 1)[1].split("</script>", 1)[0]
        script = script.replace("%%AUTH_TOKEN%%", '""')
        script = script.replace("loadResults(); tick(); loadConfig();", "")
    harness = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map(), calls = [], intervals = new Map();
let nextTimer = 0, responses = [], states = [], stateIndex = 0, effects = [], refs = [], refIndex = 0;
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',style:{},value:'',checked:false,
    options:[{}], appendChild(o){ this.options.push(o); }});
  return elements.get(id);
};
const context = vm.createContext({assert, URL, URLSearchParams, console, setImmediate,
  document: { getElementById:element, createElement:()=>({}), querySelectorAll:()=>[] },
  fetch: async (url, options={}) => {
    calls.push({url, options});
    const value = responses.shift();
    if (value instanceof Error) throw value;
    if (!value) throw new Error('Unexpected fetch: '+url);
    return {ok:true,status:200,...value,json:async()=>value.body||{},text:async()=>value.text||''};
  },
  setInterval: fn => { const id=++nextTimer; intervals.set(id,fn); return id; },
  clearInterval: id => intervals.delete(id),
  useState: initial => { const i=stateIndex++; if (!(i in states)) states[i]=initial;
    return [states[i], v=>{states[i]=v;}]; },
  useRef: initial => { const i=refIndex++; if (!(i in refs)) refs[i]={current:initial}; return refs[i]; },
  useCallback: fn => fn,
  useEffect: (fn,deps) => effects.push({fn,deps}),
  html: (parts,...values) => ({parts,values}),
  companionUrl:()=> 'http://localhost:7878', companionHeaders:()=>({'X-Grapply-Token':'test'}),
  chrome:{tabs:{create:()=>{}}},
  element, calls, intervals,
  queue: value=>responses.push(value),
  networkError: ()=>responses.push(new Error('offline')),
  resetRender: ()=>{stateIndex=0;refIndex=0;effects=[];},
  getEffects:()=>effects,
  getStates:()=>states,
});
vm.runInContext(input.script, context);
(async()=>{await vm.runInContext('(async()=>{'+input.code+'})()',context);})()
  .catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", harness],
                            input=json.dumps({"script": script, "code": code}),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr or result.stdout


def test_defaults_and_preferred_cities_preset(ui):
    assert 'value="7" selected' in ui
    assert 'id="verified" type="checkbox" checked' in ui
    assert "Save search preferences" in ui
    assert "not automatically migrated" in ui
    assert "StepStone" not in ui
    run_js(ui, """
      element('preferred').onclick();
      assert.equal(element('market').value, 'german-cities');
      assert.equal(element('since').value, '7');
      assert.equal(element('verified').checked, true);
      assert.equal(element('pf').value, '45');
      queue({body:{started:true}}); queue({body:{running:true,done:0,total:1}});
      await element('run').onclick();
      const q = new URL(calls[0].url, 'http://local').searchParams;
      assert.equal(q.get('verified_only'), 'true'); assert.equal(q.get('since_days'), '7');
      assert.equal(q.get('market'), 'german-cities');
      assert.equal(calls.filter(c=>c.options.method==='POST').length, 1);
    """)


def test_evidence_review_and_escaping(ui):
    run_js(ui, """
      const attack = '<img src=x onerror=alert(1)>';
      render({generated:'2026-11-12T10:00:00Z', counts:{review:1,freshness_excluded:2},
        jobs:[{title:attack,company:attack,location:'Berlin',url:'javascript:alert(1)',
          posted:'2026-11-10',posted_kind:'employer_first_published',
          posted_source_url:'https://employer.example/job?x=1&y=2',board_posted:'2026-11-12',
          updated_at:'2026-11-11',source:'LinkedIn',stack_match:'agnostic',
          freshness:{status:'verified_fresh',reason:'Original publication confirmed'}}],
        review_jobs:[{title:'Review only',url:'data:text/html,evil',posted:'2026-11-12',
          posted_kind:'employer_last_published',posted_source_url:'javascript:evil()',
          freshness:{status:'unverified',reason:attack},review_reasons:['Location conflict: '+attack]}]});
      const accepted=element('tablewrap').innerHTML, review=element('reviewwrap').innerHTML;
      assert(accepted.includes('Employer first published'));
      assert(accepted.includes('2026-11-10')); assert(accepted.includes('Board posted / reposted'));
      assert(accepted.includes('Last updated')); assert(accepted.includes('Stack-agnostic'));
      assert(accepted.includes('https://employer.example/job?x=1&amp;y=2'));
      assert(!accepted.includes('<img')); assert(!accepted.includes('javascript:'));
      assert(!accepted.includes('Review only'));
      assert(review.includes('Review only')); assert(review.includes('Location unknown'));
      assert(review.includes('Employer last published (may be a repost)'));
      assert(review.includes('Location conflict: &lt;img'));
      assert(!review.includes('data:text')); assert(!review.includes('javascript:'));
      assert(!review.includes('<img'));
    """)


@pytest.mark.parametrize("status,label", [("old", "Older than window"), ("unknown", "Date unknown"),
    ("unverified", "Unverified original date"), ("future", "Future date"),
    ("unrestricted", "Freshness not required")])
def test_freshness_states(ui, status, label):
    run_js(ui, """
      render({jobs:[{title:'Legacy',freshness:{status:STATUS}}]});
      assert(element('tablewrap').innerHTML.includes(LABEL));
      assert(element('tablewrap').innerHTML.includes('Location unknown'));
      render({jobs:[],review_jobs:[{title:'Undated'}],generated:'2026-11-12'});
      assert(element('tablewrap').innerHTML.includes('No accepted matches'));
      assert(element('reviewwrap').innerHTML.includes('Undated'));
    """.replace('STATUS', json.dumps(status)).replace('LABEL', json.dumps(label)))


def test_reopen_polling_errors_and_completion(ui):
    run_js(ui, """
      queue({body:{running:true,stage:'reading',done:1,total:2}}); await tick();
      assert.equal(intervals.size,1); assert.equal(element('run').disabled,true);
      queue({ok:false,status:503,text:'unavailable'}); await tick();
      assert(element('status').textContent.includes('503')); assert.equal(intervals.size,1);
      queue({body:{running:false,error:'Source failed'}});
      queue({body:{generated:'2026-11-12',jobs:[]}}); await tick();
      assert.equal(intervals.size,0); assert.equal(element('run').disabled,false);
      assert(element('status').textContent.includes('Source failed'));
      networkError(); await loadResults();
      assert(element('resultstatus').textContent.includes('offline'));
      queue({body:{jobs:[]}}); await loadResults();
      assert.equal(element('resultstatus').textContent,'');
      networkError(); await element('run').onclick();
      assert.equal(element('run').disabled,false);
      assert(element('status').textContent.includes('offline'));
    """)


def test_broader_override_and_editor_errors(ui):
    run_js(ui, """
      element('since').value='0'; element('verified').checked=false;
      queue({body:{started:true}}); queue({body:{running:true}});
      await element('run').onclick();
      const q=new URL(calls[0].url,'http://local').searchParams;
      assert.equal(q.get('verified_only'),'false'); assert.equal(q.get('since_days'),'0');
      assert.equal(q.has('market'),false);
      queue({ok:false,status:401,text:'Auth token rejected'}); await loadConfig();
      assert(element('kwstatus').textContent.includes('401'));
      queue({body:{config:{phases:[],allow_stack_agnostic:true}}}); await loadConfig();
      assert.equal(element('agnostic').checked,true);
      networkError(); await element('savekw').onclick();
      assert(element('kwstatus').textContent.includes('offline'));
    """)


def test_stack_preference_load_save_preserves_config(ui):
    run_js(ui, """
      const config={allow_stack_agnostic:false,phases:[],custom:{preserve:true}};
      queue({body:{config,markets:{'german-cities':'Preferred cities'},browser:{available:true}}});
      await loadConfig(); assert.equal(element('agnostic').checked,false);
      assert.equal(calls.filter(c=>c.options.method==='POST').length,0);
      element('agnostic').checked=true;
      queue({body:{saved:true}}); await element('savekw').onclick();
      const saved=JSON.parse(calls[1].options.body).config;
      assert.equal(saved.allow_stack_agnostic,true); assert.equal(saved.custom.preserve,true);
      assert.equal(saved.phases.length,0);
    """)


@pytest.mark.parametrize("verified", [None, True, False])
def test_endpoint_forwards_strict_override(endpoints, verified):
    client, scope, fake, thread = endpoints
    params = {"since_days": 7, "market": "german-cities"}
    if verified is not None:
        params["verified_only"] = str(verified).lower()
    response = client.post("/discovery/run", params=params,
                           headers={"X-Grapply-Token": "test-secret"})
    assert response.status_code == 200
    thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
    assert fake.run_scan.call_args.kwargs["verified_only"] is verified
    assert fake.run_scan.call_args.kwargs["since_days"] == 7
    assert fake.run_scan.call_args.kwargs["market"] == "german-cities"
    fake.save_config.assert_not_called()


def test_endpoint_auth_busy_results_and_save(endpoints):
    client, scope, fake, thread = endpoints
    assert client.post("/discovery/run").status_code == 401
    headers = {"X-Grapply-Token": "test-secret"}
    scope["_DISCOVERY_STATE"]["running"] = True
    assert client.post("/discovery/run", headers=headers).status_code == 409
    thread.assert_not_called()
    evidence = {"jobs": [{"posted_kind": "employer_first_published"}],
                "review_jobs": [{"review_reasons": ["Location conflict"]}]}
    fake.load_last.return_value = evidence
    assert client.get("/discovery/results").json() == evidence
    config = {"phases": [{"name": "saved"}], "allow_stack_agnostic": True, "custom": 1}
    assert client.post("/discovery/config", json={"config": config}, headers=headers).status_code == 200
    fake.save_config.assert_called_once_with(config)


def test_endpoint_start_failure_and_token_embedding(endpoints):
    client, scope, fake, thread = endpoints
    # The HTML must remain executable for quotes and cannot contain an injected script end.
    scope['CFG']['auth_token'] = "quote'\"</script><script>evil()</script>"
    page = client.get('/discovery').text
    token = page.split('const TOKEN = ', 1)[1].split(';', 1)[0]
    assert json.loads(token) == scope['CFG']['auth_token']
    assert '</script>' not in token
    scope['CFG']['auth_token'] = 'test-secret'
    thread.return_value.start.side_effect = RuntimeError('Cannot start thread')
    client = TestClient(scope['app'], raise_server_exceptions=False)
    assert client.post('/discovery/run', headers={'X-Grapply-Token':'test-secret'}).status_code == 500
    assert scope['_DISCOVERY_STATE']['running'] is False
    assert scope['_DISCOVERY_STATE']['error'] == 'Cannot start thread'
    fake.run_scan.assert_not_called()


def test_extension_strict_scan_polling_and_safe_evidence(ui):
    run_js(ui, """
      DiscoveryPanel({settings:{},health:{status:'ok'}});
      const effect=getEffects()[0];
      assert(effect.deps.some(v=>v===true));
      queue({body:{jobs:[],review_jobs:[]}});
      queue({body:{running:true,done:1,total:2}});
      const cleanup=effect.fn(); await new Promise(r=>setImmediate(r));
      assert.equal(intervals.size,1);
      cleanup(); assert.equal(intervals.size,0);
      resetRender(); DiscoveryPanel({settings:{},health:{status:'ok'}});
      queue({body:{jobs:[]}}); queue({body:{running:true}});
      getEffects()[0].fn(); await new Promise(r=>setImmediate(r));
      assert.equal(intervals.size,1);
      assert.equal(discoveryUrl('javascript:evil()'),'');
      assert(discoveryFreshness({posted_kind:'employer_last_published'}).includes('last published'));
      assert(discoveryFreshness({stack_match:'agnostic'}).includes('Stack-agnostic'));
    """, extension=True)


def test_extension_explore_parameters(ui):
    # Execute the rendered control's actual callback, without Preact or a browser.
    run_js(ui, """
      const view=DiscoveryPanel({settings:{},health:{status:'ok'}});
      function callbacks(node){
        if(!node||typeof node!=='object')return [];
        if(Array.isArray(node))return node.flatMap(callbacks);
        return (node.values||[]).flatMap(v=>typeof v==='function'?[v]:callbacks(v));
      }
      queue({body:{started:true}});
      await callbacks(view)[0]();
      const q=new URL(calls[0].url).searchParams;
      assert.equal(q.get('since_days'),'7'); assert.equal(q.get('verified_only'),'true');
      assert.equal(q.has('market'),false);
    """, extension=True)


def test_browser_smoke_with_mocked_fetch(ui, tmp_path):
    chrome = (shutil.which('google-chrome') or shutil.which('chromium')
              or shutil.which('google-chrome-stable'))
    if not chrome and Path('/opt/google/chrome/chrome').is_file():
        chrome = '/opt/google/chrome/chrome'
    if not chrome:
        pytest.skip('No local Chrome/Chromium for the optional browser smoke test')
    result = {"generated":"2026-11-12T10:00:00Z", "jobs":[{
        "title":"Senior software engineer", "location":"Hamburg", "stack_match":"agnostic",
        "posted":"2026-11-10", "posted_kind":"employer_first_published",
        "posted_source_url":"https://employer.example/job",
        "freshness":{"status":"verified_fresh"}}],
        "review_jobs":[{"title":"Needs location review", "review_reasons":["Conflicting office location"]}]}
    mocks = """
window.fetch=async url=>({ok:true,json:async()=>String(url).includes('/results') ? RESULTS
  : String(url).includes('/config') ? {config:{phases:[],allow_stack_agnostic:false},
      markets:{'german-cities':'Preferred cities'},browser:{available:true}}
  : {running:false}});
""".replace('RESULTS', json.dumps(result))
    check = """
Promise.all([loadResults(),tick(),loadConfig()]).then(()=>{
  $('preferred').click();
  const ok=$('tablewrap').textContent.includes('Verified fresh')
    && $('tablewrap').textContent.includes('Stack-agnostic')
    && !$('tablewrap').textContent.includes('Needs location review')
    && $('reviewwrap').textContent.includes('Conflicting office location')
    && $('market').value==='german-cities' && $('since').value==='7'
    && $('verified').checked && !$('agnostic').checked;
  document.body.setAttribute('data-discovery-smoke',ok?'passed':'failed');
}).catch(()=>document.body.setAttribute('data-discovery-smoke','failed'));
"""
    page = ui.replace('%%AUTH_TOKEN%%', '""').replace('<script>', '<script>'+mocks, 1)
    page = page.replace('loadResults(); tick(); loadConfig();', check)
    url = 'data:text/html;base64,' + base64.b64encode(page.encode()).decode()
    proc = subprocess.run([chrome, '--headless', '--no-sandbox', '--disable-gpu',
        '--disable-background-networking', '--disable-breakpad', '--no-first-run',
        '--no-default-browser-check', '--host-resolver-rules=MAP * ~NOTFOUND',
        '--user-data-dir='+str(tmp_path / 'chrome'), '--virtual-time-budget=1000',
        '--dump-dom', url], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert 'data-discovery-smoke="passed"' in proc.stdout, proc.stderr[-1500:]
