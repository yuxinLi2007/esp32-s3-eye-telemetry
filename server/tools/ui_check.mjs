// 把 static/index.html 里的脚本抠出来，用最小 DOM 桩在 node 里跑一遍，
// 对着真实服务端数据检查：报警判定、分段/缺口判定、渲染路径是否抛异常。
//
// 存在的意义：界面的核心职责是"把失败显示出来"，而那恰恰是最难用肉眼回归的部分。
// 这里可以确定性地断言"该报警时必须报警"，而不必真的去拔电源。
//
// 用法： node tools/ui_check.mjs [--origin http://127.0.0.1:8001]
import fs from 'fs';

const argOrigin = (() => {
  const i = process.argv.indexOf('--origin');
  return i >= 0 ? process.argv[i + 1] : 'http://127.0.0.1:8000';
})();

const HTML = new URL('../static/index.html', import.meta.url);
const code = fs.readFileSync(HTML, 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];

const draw = [];
const rec = n => (...a) => draw.push([n, ...a]);
const makeCtx = () => ({
  setTransform: rec('setTransform'), clearRect: rec('clearRect'),
  fillText: rec('fillText'), beginPath: rec('beginPath'),
  moveTo: rec('moveTo'), lineTo: rec('lineTo'), stroke: rec('stroke'),
  fillRect: rec('fillRect'), save: rec('save'), restore: rec('restore'),
  setLineDash: rec('setLineDash'),
  fillStyle: '', strokeStyle: '', lineWidth: 1, font: '',
  textAlign: '', textBaseline: '', lineJoin: '', lineCap: '',
});

const els = new Map();
const makeEl = (tag = 'div') => ({
  tagName: tag, children: [], _text: '', className: '', style: {}, colSpan: 0,
  get textContent() { return this._text; },
  set textContent(v) { this._text = String(v); },
  set innerHTML(v) { if (v === '') this.children = []; },
  get innerHTML() { return this._text; },
  appendChild(c) { this.children.push(c); return c; },
  addEventListener() {}, clientWidth: 800, width: 0, height: 0,
  value: '3000',
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 170 }),
  getContext() { return this._ctx || (this._ctx = makeCtx()); },
});

globalThis.document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl()); return els.get(id); },
  createElement: t => makeEl(t),
  createTextNode: t => ({ children: [], textContent: String(t), appendChild() {} }),
};
globalThis.window = { devicePixelRatio: 1, addEventListener() {} };
globalThis.setInterval = () => 0;

// 浏览器里相对路径按页面 origin 解析；node 没有 origin，这里补上
const nativeFetch = globalThis.fetch;
globalThis.fetch = (u, o) =>
  nativeFetch(String(u).startsWith('http') ? u : argOrigin + u, o);

const get = id => document.getElementById(id);
const txt = e => (e?.children || []).map(c => c.textContent).join('') || e?.textContent || '';

const api = new Function(code + '\n;return {buildSeries, trustOf, TRUST_TEXT};')();
await new Promise(r => setTimeout(r, 1800));

const alarm = get('alarm');
console.log('报警等级:', alarm.className);
console.log('报警内容:', txt(alarm.children[0]), '|', txt(alarm.children[1]));

console.log('\n指标卡:');
for (const c of get('cards').children) {
  console.log('  ', (c.className || '').padEnd(10),
    c.children[0]?.textContent, '=', txt(c.children[1]));
}

const kv = get('prov').children[0];
if (kv) {
  console.log('\n来源面板:');
  for (let i = 0; i < kv.children.length; i += 2)
    console.log('  ', kv.children[i].textContent, '=', txt(kv.children[i + 1]));
}

const rr = await (await fetch('/api/v1/readings?limit=3000')).json();
const s = api.buildSeries(rr.readings);
console.log('\n--- 分段/缺口判定 ---');
console.log('样本数:', rr.readings.length, ' 连续段数:', s.segs.length, ' 断开带:', s.bands.length);
for (const b of s.bands.slice(0, 10)) console.log('   带:', b.reason);
console.log('SPL 为 null 的点:', s.pts.filter(p => p.spl === null).length);
console.log('|a| 落在 0.8~1.2 之外:', s.pts.filter(p => p.mag !== null && (p.mag < 0.8 || p.mag > 1.2)).length);
console.log('x 单调递增:', s.pts.every((p, i) => i === 0 || p.x >= s.pts[i - 1].x));
console.log('canvas 绘制调用:', draw.length, ' stroke:', draw.filter(d => d[0] === 'stroke').length);
console.log('服务端 trust.counts:', JSON.stringify(rr.trust.counts));
