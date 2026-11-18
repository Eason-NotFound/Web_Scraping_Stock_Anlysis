import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { apiPath, chartGeometry, chartSeries, dashboardMetrics, numberOrNull, percentChange, quoteState, replayPriceCrossing, safeSourceUrl, singleFlight, timestamp, translate, validateEnvelope } from '../public/app.js';

test('only explicit live envelopes can supply dashboard data',()=>{
  const envelope={data:[],meta:{data_mode:'live',generated_at:'2026-11-05T12:00:00Z'}};
  assert.equal(validateEnvelope(envelope),envelope);
  for(const invalid of [null,{data:[]},{data:[],meta:{data_mode:'demo'}},{data:null,meta:{data_mode:'live'}}])assert.throws(()=>validateEnvelope(invalid));
});

test('source links allow web documents but reject active schemes and embedded credentials',()=>{
  assert.equal(safeSourceUrl('https://example.org/article?id=10'),'https://example.org/article?id=10');
  for(const unsafe of ['javascript:alert(1)','data:text/html,<script>alert(1)</script>','file:///etc/passwd','https://user:secret@example.org/','//example.org','not a URL'])assert.equal(safeSourceUrl(unsafe),null);
});

test('filter query values remain data, not injected query parameters',()=>{
  const path=apiPath('news',{symbol:'AAPL',category:'Politics & Policy',sentiment:'positive&limit=999',limit:20,offset:40});
  const parsed=new URL(path,'https://dashboard.example');
  assert.equal(parsed.pathname,'/api/news');
  assert.equal(parsed.searchParams.get('category'),'Politics & Policy');
  assert.equal(parsed.searchParams.get('sentiment'),'positive&limit=999');
  assert.equal(parsed.searchParams.get('limit'),'20');
  assert.equal(apiPath('news',{symbol:'',category:'',limit:20,offset:0}),'/api/news?limit=20&offset=0');
  assert.throws(()=>apiPath('../private'));
});

test('freshness uses source time and market clock rather than successful page refresh',()=>{
  const now=Date.parse('2026-11-05T15:00:00Z');
  const quote={market_open:true,price:100,source_time:'2026-11-05T14:59:00Z'};
  assert.equal(quoteState(quote,now),'fresh');
  assert.equal(quoteState({...quote,source_time:'2026-11-05T14:50:00Z'},now),'stale');
  assert.equal(quoteState({...quote,source_time:'2026-11-05T15:05:00Z'},now),'stale');
  assert.equal(quoteState({...quote,source_time:'2026-11-05T14:59:00'},now),'stale');
  assert.equal(quoteState({...quote,market_open:false},now),'closed');
  assert.equal(quoteState({...quote,clock_status:'unavailable'},now),'clockUnknown');
  assert.equal(quoteState({...quote,price:NaN},now),'stale');
});

test('missing or invalid numeric data stays missing instead of becoming zero or a percentage',()=>{
  for(const value of [null,undefined,'',false,true,Infinity,'not-a-price'])assert.equal(numberOrNull(value),null);
  assert.equal(numberOrNull('10.2'),10.2);
  assert.equal(percentChange({price:110,previous_close:100}),10.000000000000009);
  assert.equal(percentChange({price:110,previous_close:0}),null);
  assert.equal(timestamp('2026-11-05T10:00:00'),null);
  assert.equal(timestamp('2026-11-05T18:00:00+08:00'),Date.parse('2026-11-05T10:00:00Z'));
});

test('chart data is ordered, deduplicated and finite, including a single constant price',()=>{
  const bars=[{time:'2026-11-02T00:00:00Z',close:102},{time:'2026-11-01T00:00:00Z',close:100},{time:'2026-11-02T00:00:00Z',close:103},{time:'2026-11-03T00:00:00Z',close:-1},{time:'invalid',close:104}];
  const original=structuredClone(bars);
  assert.deepEqual(chartSeries(bars).map(p=>p.close),[100,103]);
  assert.deepEqual(bars,original);
  const chart=chartGeometry([bars[0]]);
  assert.ok(chart.max>chart.min);
  assert.ok(chart.points.flat().every(Number.isFinite));
  assert.equal(chartGeometry([]),null);
});

test('concurrent refreshes collapse to one follow-up and never overlap',async()=>{
  let concurrent=0,maximum=0,calls=0;
  const releases=[];
  const request=singleFlight(async()=>{calls++;concurrent++;maximum=Math.max(maximum,concurrent);await new Promise(resolve=>releases.push(resolve));concurrent--;});
  const first=request(),second=request(),third=request();
  assert.equal(first,second);assert.equal(second,third);assert.equal(calls,1);
  releases.shift()();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,2);assert.equal(maximum,1);
  releases.shift()();await first;
  assert.equal(concurrent,0);assert.equal(calls,2);
});

test('a failed refresh does not permanently lock subsequent refreshes',async()=>{
  let calls=0,idle=0;
  const request=singleFlight(async()=>{if(++calls===1)throw new Error('offline');},()=>idle++);
  await assert.rejects(request(),/offline/);
  await request();assert.equal(calls,2);assert.equal(idle,2);
});

test('translation defaults to English and never rewrites arbitrary evidence',()=>{
  assert.equal(translate('market'),'Market');assert.equal(translate('market','zh'),'市场行情');
  const source='Revenue rose 12%. 原文证据 <img src=x onerror=alert(1)>';
  assert.equal(translate(source,'en'),source);assert.equal(translate(source,'zh'),source);
});

test('public UI uses safe DOM rendering and provides accessible tab relationships',async()=>{
  const source=await readFile(new URL('../public/app.js',import.meta.url),'utf8');
  assert.equal(/\.innerHTML\s*=|insertAdjacentHTML|document\.write\s*\(/.test(source),false);
  const html=await readFile(new URL('../public/index.html',import.meta.url),'utf8');
  for(const tab of ['market','news','alerts']){
    assert.ok(html.includes(`id="tab-${tab}" role="tab" aria-controls="panel-${tab}"`));
    assert.ok(html.includes(`id="panel-${tab}" role="tabpanel" aria-labelledby="tab-${tab}"`));
  }
  assert.ok(html.includes('id="language"'));
});


test('summary counts unique latest symbols, never historical quote rows',()=>{
  const status={data_available:true,counts:{quotes:812,alerts:3},news_total:29,analysis_counts:{success:5}};
  const values=dashboardMetrics(status,[{symbol:'AAPL'},{symbol:'MSFT'},{symbol:'AAPL'}]);
  assert.deepEqual(values,{stocks:2,news:29,analyses:5,alerts:3});
  assert.equal(dashboardMetrics(status,null).stocks,null);
});

test('unavailable or missing counts stay unavailable rather than fabricated zeros',()=>{
  assert.deepEqual(dashboardMetrics({data_available:false,news_total:0,analysis_counts:{success:0}},[]),{stocks:null,news:null,analyses:null,alerts:null});
  assert.deepEqual(dashboardMetrics({},null),{stocks:null,news:null,analyses:null,alerts:null});
  assert.deepEqual(dashboardMetrics({data_available:true},[]),{stocks:0,news:null,analyses:null,alerts:null});
  assert.deepEqual(dashboardMetrics({data_available:true,news_total:0,analysis_counts:{success:0},counts:{alerts:0}},[]),{stocks:0,news:0,analyses:0,alerts:0});
});

test('synthetic replay triggers only on a crossing, including arrival at the threshold', () => {
  assert.deepEqual(replayPriceCrossing('first'), {
    previous: null, current: 101, threshold: 100, triggered: false,
  });
  assert.deepEqual(replayPriceCrossing('cross'), {
    previous: 99, current: 100, threshold: 100, triggered: true,
  });
  assert.deepEqual(replayPriceCrossing('above'), {
    previous: 101, current: 102, threshold: 100, triggered: false,
  });
});

test('public price-crossing examples and outcomes have both English and Chinese labels', () => {
  for (const key of ['priceCrossingTitle', 'syntheticExample', 'triggeredExample', 'noTriggerExample']) {
    const en = translate(key, 'en');
    const zh = translate(key, 'zh');
    assert.notEqual(en, key);
    assert.ok(en.trim());
    assert.match(zh, /\p{Script=Han}/u);
    assert.notEqual(en, zh);
  }
});

test('Recorded alerts includes the linked price-crossing section and its three examples', async () => {
  const html = await readFile(new URL('../public/index.html', import.meta.url), 'utf8');
  const panel = html.match(/<section\b[^>]*\bid="panel-alerts"[^>]*>([\s\S]*?)<\/section>/)?.[1];
  assert.ok(panel, 'Recorded alerts panel missing');
  assert.match(panel, /\bid="price-crossing"/);
  assert.match(panel, /\bdata-i18n="priceCrossingTitle"/);
  const scenarios = Array.from(panel.matchAll(/\bdata-crossing-example="([^"]+)"/g), match => match[1]);
  assert.deepEqual(scenarios.sort(), ['above', 'cross', 'first']);
});
