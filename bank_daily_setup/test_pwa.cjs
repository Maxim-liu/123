/** Regression tests for report updates and offline cache migration. Run with node --test. */
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const base = 'https://report.example/';
const template = fs.readFileSync(path.join(__dirname, 'report_template.html'), 'utf8');
const pwaSource = template.match(/<script id="pwa-app-script">([\s\S]*?)<\/script>/)[1];
const swSource = fs.readFileSync(path.join(__dirname, '..', 'sw.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function pageEnvironment(metadata = {date:'2026-11-20', revision:'revision-a'}) {
  const state = {metadata, error:null, status:200, fetches:[], navigations:[], intervals:[], timeouts:new Set(), listeners:{}, registrations:[]};
  const banner = {style:{}, setAttribute(){}};
  const button = {disabled:false, textContent:'立即更新', addEventListener(type, fn){state.click = fn;}};
  const document = {hidden:false, getElementById(id){return id === 'app-update-banner' ? banner : button;}, addEventListener(type, fn){state.listeners[type] = fn;}};
  const window = {addEventListener(type, fn){state.listeners[type] = fn;}};
  const location = {protocol:'https:', hostname:'report.example', href:base+'index.html#all', hash:'#all', replace(url){state.navigations.push(url);}};
  const navigator = {serviceWorker:{register:async (url, options)=>{state.registrations.push({url,options});}}};
  const fetch = async (url, options) => {
    state.fetches.push({url,options});
    if (state.error) throw state.error;
    if (state.hang) return await new Promise((resolve, reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
    return {ok:state.status===200, json:async()=>{if(state.badJson)throw new SyntaxError('bad JSON');return state.metadata;}};
  };
  const script = pwaSource.replace(/const BUILD='[^']*'/, "const BUILD='2026-11-20'").replace(/const REV='[^']*'/, "const REV='revision-a'");
  vm.runInNewContext(script, {document,window,location,navigator,fetch,URL,Date,AbortController,
    setTimeout(fn){state.timeouts.add(fn);return fn;}, clearTimeout(fn){state.timeouts.delete(fn);},
    setInterval(fn, delay){state.intervals.push({fn,delay});}
  });
  return {state,banner,button,document};
}

function workerEnvironment() {
  const stores = new Map();
  const state = {fetches:[], offline:false, status:200, body:'network-current-report', skipped:0, claimed:0};
  const key = input => new URL(typeof input === 'string' ? input : input.url, base).href;
  const caches = {
    async keys(){return [...stores.keys()];},
    async delete(name){return stores.delete(name);},
    async open(name){
      if(!stores.has(name)) stores.set(name,new Map());
      const store=stores.get(name);
      return {async match(url){return store.get(key(url))?.clone();}, async put(url,response){store.set(key(url),response.clone());}};
    }
  };
  const handlers={};
  const self={location:{href:base+'sw.js'}, addEventListener(type,fn){handlers[type]=fn;},
    async skipWaiting(){state.skipped++;}, clients:{async claim(){state.claimed++;}}};
  const fetch=async (input,options)=>{
    state.fetches.push({url:key(input),options});
    if(state.offline)throw new TypeError('offline');
    return new Response(state.body,{status:state.status,headers:{'Content-Type':'text/html'}});
  };
  vm.runInNewContext(swSource,{self,caches,fetch,URL,Response});
  async function lifecycle(type){let promise;handlers[type]({waitUntil(p){promise=p;}});await promise;}
  function request(url,mode='navigate',method='GET'){
    let promise;
    handlers.fetch({request:{url:new URL(url,base).href,mode,method},respondWith(p){promise=Promise.resolve(p);}});
    return promise;
  }
  return {state,caches,lifecycle,request};
}

test('same-day content changes show an update; equal revisions do not', async()=>{
  const p=pageEnvironment();await flush();
  assert.equal(p.banner.style.display,'none');
  p.state.metadata={date:'2026-11-20',revision:'revision-b'};
  p.state.listeners.visibilitychange();await flush();
  assert.equal(p.banner.style.display,'flex');
  assert.equal(p.state.navigations.length,0);
  assert.equal(p.state.fetches[0].options.cache,'no-store');
  assert.equal(p.state.registrations[0].options.updateViaCache,'none');
});

test('legacy metadata recognizes a corrected earlier date and settles on the same date', async()=>{
  const p=pageEnvironment({date:'2026-11-19'});await flush();
  assert.equal(p.banner.style.display,'flex');
  p.state.metadata={report_date:'2026-11-20'};p.state.listeners.online();await flush();
  assert.equal(p.banner.style.display,'none');
});

test('one user action navigates once; repeated events cannot trigger a reload loop', async()=>{
  const p=pageEnvironment({date:'2026-11-20',revision:'revision-b'});await flush();
  p.state.click();p.state.click();p.state.listeners.pageshow();p.state.intervals[0].fn();await flush();
  assert.equal(p.state.navigations.length,1);
  const target=new URL(p.state.navigations[0]);
  assert.equal(target.origin,new URL(base).origin);assert.equal(target.pathname,'/');
  assert.equal(target.hash,'#all');assert(target.searchParams.has('v'));
  assert.equal(p.button.disabled,true);
});

test('polls every minute only in foreground; visibility, pageshow, and online also recheck', async()=>{
  const p=pageEnvironment();await flush();
  assert.deepEqual(p.state.intervals.map(x=>x.delay),[60000]);
  let calls=p.state.fetches.length;p.document.hidden=true;p.state.intervals[0].fn();await flush();
  assert.equal(p.state.fetches.length,calls);
  p.document.hidden=false;
  for(const event of ['visibilitychange','pageshow','online']){p.state.listeners[event]();await flush();calls++;assert.equal(p.state.fetches.length,calls);}
  p.state.intervals[0].fn();await flush();assert.equal(p.state.fetches.length,calls+1);
});

test('bad metadata and temporary network failures preserve an already known update', async()=>{
  const p=pageEnvironment({revision:'revision-b'});await flush();assert.equal(p.banner.style.display,'flex');
  p.state.error=new Error('offline');p.state.listeners.online();await flush();assert.equal(p.banner.style.display,'flex');
  p.state.error=null;p.state.badJson=true;p.state.listeners.online();await flush();assert.equal(p.banner.style.display,'flex');
  p.state.badJson=false;p.state.status=503;p.state.listeners.online();await flush();assert.equal(p.banner.style.display,'flex');
  p.state.status=200;p.state.metadata={date:'invalid'};p.state.listeners.online();await flush();assert.equal(p.banner.style.display,'none');
});

test('hung checks time out and can be retried, without overlapping requests', async()=>{
  const p=pageEnvironment();await flush();p.state.hang=true;p.state.listeners.online();
  p.state.listeners.pageshow();p.state.intervals[0].fn();assert.equal(p.state.fetches.length,2);
  for(const timeout of p.state.timeouts)timeout();await flush();
  p.state.hang=false;p.state.metadata={revision:'revision-b'};p.state.listeners.online();await flush();
  assert.equal(p.state.fetches.length,3);assert.equal(p.banner.style.display,'flex');
});

test('cache migration preserves the last offline report and unrelated caches', async()=>{
  const w=workerEnvironment();const old=await w.caches.open('bank-report-v2');
  await old.put('./index.html',new Response('previous-offline-report'));
  await old.put('./latest.json',new Response('stale-metadata'));
  await w.caches.open('unrelated-app');await w.lifecycle('activate');
  const keys=await w.caches.keys();assert(keys.includes('bank-report-v3'));assert(keys.includes('unrelated-app'));assert(!keys.includes('bank-report-v2'));
  const cache=await w.caches.open('bank-report-v3');assert.equal(await (await cache.match('./index.html')).text(),'previous-offline-report');
  assert.equal(await cache.match('./latest.json'),undefined);assert.equal(w.state.claimed,1);
});

test('navigation uses fresh HTML online, then the saved HTML offline, including refresh query strings', async()=>{
  const w=workerEnvironment();await (await w.caches.open('bank-report-v3')).put('./index.html',new Response('old-report'));
  assert.equal(await (await w.request('./?v=123')).text(),'network-current-report');
  assert.equal(w.state.fetches[0].options.cache,'no-store');
  w.state.offline=true;assert.equal(await (await w.request('./index.html?v=456')).text(),'network-current-report');
});

test('latest and install manifest ignore poisoned cache entries and fail offline', async()=>{
  const w=workerEnvironment();const cache=await w.caches.open('bank-report-v3');
  for(const path of ['latest.json','manifest.webmanifest']){
    await cache.put(path,new Response('stale-poison'));w.state.body='fresh-'+path;
    assert.equal(await (await w.request(path+'?check=123','cors')).text(),'fresh-'+path);
    assert.equal(w.state.fetches.at(-1).options.cache,'no-store');
    w.state.offline=true;await assert.rejects(w.request(path,'cors'),/offline/);w.state.offline=false;
  }
});

test('temporary server errors fall back to the saved report; missing pages and empty offline caches are explicit', async()=>{
  const w=workerEnvironment();await (await w.caches.open('bank-report-v3')).put('./index.html',new Response('saved-report'));
  w.state.status=503;assert.equal(await (await w.request('./')).text(),'saved-report');
  w.state.status=404;assert.equal((await w.request('./')).status,404);
  await w.caches.delete('bank-report-v3');w.state.offline=true;const response=await w.request('./');
  assert.equal(response.status,503);assert((await response.text()).includes('当前离线'));
});

test('optional missing icons do not block worker installation; cross-origin and POST requests are untouched', async()=>{
  const w=workerEnvironment();w.state.offline=true;await w.lifecycle('install');assert.equal(w.state.skipped,1);
  assert.equal(w.request('https://other.example/'),undefined);assert.equal(w.request('./','navigate','POST'),undefined);
});
