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
const code = fs.readFileSync(HTML, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

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
  // classList：commands.js 用它标 mismatch/bad。桩里只记类名，够断言用。
  classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
               contains(c) { return this._s.has(c); },
               toString() { return [...this._s].join(' '); } },
  onclick: null, min: null, max: null, type: '', title: '', disabled: false,
  // createElement 出来再赋 id 的元素，要能被 $ 找回来（cmd_p_n / cmd_dur 就是这么造的）。
  // 覆盖而不是"没有才登记"：登记在后的那个才是真正在页面上的。
  get id() { return this._id || ''; },
  set id(v) { this._id = v; if (v) els.set(v, this); },
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
  title: '', readyState: 'complete', addEventListener() {},
};
// prompt 默认给 null（模拟"用户不肯输令牌"）；跑带 CONTROL_TOKEN 的服务端时用
// CTL_TOKEN 环境变量喂进去，否则 401 分支会把所有下发都测成失败。
// confirm 固定 false：撤销是危险动作，回归脚本不该真的去撤服务端上的指令。
globalThis.window = {
  devicePixelRatio: 1, addEventListener() {},
  prompt: () => process.env.CTL_TOKEN || null,
  confirm: () => false,
};
globalThis.setInterval = () => 0;
// 桩成"由服务端提供页面"这一种情况：这样脚本走同源相对路径，
// 下面 fetch 桩再把相对路径补成绝对地址。file:// 分支另有单测覆盖。
globalThis.location = { protocol: 'http:', search: '', href: 'http://x/' };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

// 浏览器里相对路径按页面 origin 解析；node 没有 origin，这里补上
const nativeFetch = globalThis.fetch;
globalThis.fetch = (u, o) =>
  nativeFetch(String(u).startsWith('http') ? u : argOrigin + u, o);

const get = id => document.getElementById(id);
const txt = e => (e?.children || []).map(c => c.textContent).join('') || e?.textContent || '';

const api = new Function(
  code + '\n;return {buildSeries, trustOf, TRUST_TEXT, stopIntervals};')();
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

// 采集开关：按钮可用性必须跟真实状态一致，否则会出现"点停止反而把采集开回来"
const cd = await (await fetch('/api/v1/control')).json();
console.log('\n--- 采集开关 ---');
console.log('状态栏:', txt(get('ctl_state')));
console.log('开始键禁用:', get('btn_start').disabled, ' 停止键禁用:', get('btn_stop').disabled);
console.log('预期(采集开时):', '开始禁用=true 停止禁用=false');
console.log('停止区间:', JSON.stringify(api.stopIntervals(cd.history, Date.now())));

const rr = await (await fetch('/api/v1/readings?limit=3000')).json();
const s = api.buildSeries(rr.readings, api.stopIntervals(cd.history, Date.now()));
console.log('\n--- 分段/缺口判定 ---');
console.log('样本数:', rr.readings.length, ' 连续段数:', s.segs.length, ' 断开带:', s.bands.length);
for (const b of s.bands.slice(0, 10)) console.log('   带:', b.kind, b.reason);
console.log('SPL 为 null 的点:', s.pts.filter(p => p.spl === null).length);
console.log('|a| 落在 0.8~1.2 之外:', s.pts.filter(p => p.mag !== null && (p.mag < 0.8 || p.mag > 1.2)).length);
console.log('x 单调递增:', s.pts.every((p, i) => i === 0 || p.x >= s.pts[i - 1].x));
console.log('canvas 绘制调用:', draw.length, ' stroke:', draw.filter(d => d[0] === 'stroke').length);
console.log('服务端 trust.counts:', JSON.stringify(rr.trust.counts));

// ================= 第2周：远程指令面板 =================
// 这一段盯的是指令面板最容易坏、又最难用肉眼看出来的两件事：
//   1. 连点三次结果下发了三条（防抖失效）——只能真的去点、数 POST 次数来证明；
//   2. 参数范围前后端各写一份然后对不上——只能把服务端给的 ops 原样打出来对。
const CMD_JS = new URL('../static/commands.js', import.meta.url);
const cmdCode = fs.readFileSync(CMD_JS, 'utf8');

const posts = [];                       // 只数"下发指令"这一个端点，轮询 GET 不算
const stubFetch = globalThis.fetch;
globalThis.fetch = (u, o) => {
  if (o && o.method === 'POST' && String(u).includes('/api/v1/commands')) posts.push(String(u));
  return stubFetch(u, o);
};

const fail = msg => { console.error('FAIL: ' + msg); process.exit(1); };

new Function(cmdCode)();
await new Promise(r => setTimeout(r, 1600));

console.log('\n--- 远程指令面板 ---');
const opsBox = get('cmd_ops');
const btns = opsBox.children.filter(c => c.tagName === 'button');
// input 是数值参数，select 是枚举参数（notify 的 decision 就走 select）
const inps = opsBox.children.filter(c => c.tagName === 'input' || c.tagName === 'select');
if (!btns.length) fail('一个指令按钮都没渲染出来（/commands/ops 没取到？）');
console.log('按钮:', btns.map(b => b.textContent + (b.disabled ? '(禁用)' : '')).join('  '));
for (const i of inps) {
  if (i.tagName === 'select') {
    const opts = (i.children || []).map(o => o.value || o.textContent);
    console.log('  枚举框', i.id, '=', i.value, ' 可选', opts.join('/'));
    if (i.id === 'cmd_p_decision' && opts.join('/') !== 'ack/cancel')
      fail('notify 的 decision 枚举不对：' + opts.join('/'));
  } else {
    console.log('  参数框', i.id, '=', i.value, ' 范围', i.min, '~', i.max);
  }
}
console.log('统计栏:', txt(get('cmd_stats')));

const rows = get('cmd_tbody').children;
console.log('表格行数:', rows.length);
for (const tr of rows.slice(0, 6)) {
  const td = tr.children;
  if (td.length < 7) { console.log('  ', txt(td[0])); continue; }
  const badge = td[2].children[0];
  console.log('  ', txt(td[0]).slice(0, 34).padEnd(34),
    '|', (badge ? badge.className : '').padEnd(16), txt(badge),
    '| 样本', td[4].textContent, '|', txt(td[5]).slice(0, 40));
}

// --- 防抖断言：连点三次 ping，服务端只应该收到一次 POST ---
posts.length = 0;
get('cmd_mac').value = '94:A9:90:1C:6F:D4';
const ping = btns.find(b => String(b.textContent).startsWith('ping'));
if (!ping) fail('找不到 ping 按钮');
ping.onclick(); ping.onclick(); ping.onclick();
await new Promise(r => setTimeout(r, 1500));
console.log('\n--- 防抖断言 ---');
console.log('连点 3 次 ping → 实际 POST /api/v1/commands 次数:', posts.length);
if (posts.length !== 1) fail('期望 1 次，实际 ' + posts.length + ' 次（前端防抖失效）');
console.log('提示栏:', txt(get('cmd_note')));
console.log('按钮恢复可用:', btns.every(b => !b.disabled) ? '（下一次点击是新意图）' : 'FAIL 仍禁用');
console.log('OK: 前端防抖生效');
// ================= 第3周：按键事件面板 =================
// 这一段盯的是三件肉眼最容易放过去的事：
//   1. 事件行到底渲染出来没有——没事件时也必须有一行明确的空态提示，
//      而不是一片空白（空白和"面板挂了"看起来一模一样）；
//   2. 连点三次「回应」会不会让设备闪两遍灯：数 POST 次数 + 比对 client_token，
//      两件事都成立才叫幂等；
//   3. 指令状态是不是跟着行一起显示的（前端不存副本，另一个标签页回应了这里也能看到）。
const BTN_JS = new URL('../static/button.js', import.meta.url);
const btnCode = fs.readFileSync(BTN_JS, 'utf8');

const btnPosts = [];                  // 只数 respond 这一个端点，2s 轮询的 GET 不算
const stubFetch2 = globalThis.fetch;
globalThis.fetch = (u, o) => {
  if (o && o.method === 'POST' && String(u).includes('/api/v1/button/events')) {
    let tok = null;
    try { tok = JSON.parse(o.body || '{}').client_token; } catch (e) { /* 断言里会报 */ }
    btnPosts.push({ url: String(u), token: tok });
  }
  return stubFetch2(u, o);
};

new Function(btnCode)();
await new Promise(r => setTimeout(r, 1600));

console.log('\n--- 按键事件面板 ---');
console.log('统计栏:', txt(get('btn_stats')));
const brows = get('btn_tbody').children;
if (!brows.length) fail('按键面板一行都没渲染出来（连空态提示都没有）');
const emptyState = brows.length === 1 && brows[0].children.length === 1 &&
                   brows[0].children[0].colSpan > 0;
if (emptyState) {
  console.log('空态提示:', txt(brows[0].children[0]));
} else {
  console.log('表格行数:', brows.length);
  for (const tr of brows.slice(0, 6)) {
    const td = tr.children;
    if (td.length < 7) { console.log('  ', txt(td[0])); continue; }
    const st = td[4].children[0], cm = td[5].children[1];
    console.log('  ', txt(td[1]).padEnd(12), '|', txt(td[2]).slice(0, 22).padEnd(22),
      '|', (st ? st.className : '').padEnd(12), txt(st).padEnd(6),
      '|', txt(td[5]).slice(0, 34).padEnd(34), '|', txt(td[6]).slice(0, 16));
  }
}
console.log('提示栏:', txt(get('btn_note')) || '（空）');

// --- 回应防抖 + 幂等键断言：连点三次「回应」，服务端只应收到一次 POST，且 token 唯一 ---
const acks = [];
for (const tr of brows) for (const td of tr.children) for (const c of td.children)
  if (c.tagName === 'button' && c.textContent === '回应') acks.push(c);

if (!acks.length) {
  console.log('\n--- 回应防抖断言 ---');
  console.log('SKIP: 当前没有待回应事件（先 POST /api/v1/button 造一条再跑这一段）');
} else {
  // confirm 全局桩是 false（撤销是危险动作）。这里只点「回应」，而且只对这个
  // origin 生效，所以临时放开；点完立刻还原。
  const confirmStub = window.confirm;
  window.confirm = () => true;
  btnPosts.length = 0;
  acks[0].onclick(); acks[0].onclick(); acks[0].onclick();
  await new Promise(r => setTimeout(r, 1500));
  window.confirm = confirmStub;

  console.log('\n--- 回应防抖断言 ---');
  console.log('连点 3 次「回应」→ 实际 POST respond 次数:', btnPosts.length);
  if (btnPosts.length !== 1)
    fail('期望 1 次，实际 ' + btnPosts.length + ' 次（前端防抖失效，设备会闪两遍灯）');
  const tok = btnPosts[0].token;
  if (!tok || !String(tok).startsWith('web-'))
    fail('client_token 缺失或不是 web- 前缀: ' + JSON.stringify(tok));
  console.log('幂等键:', tok, ' →', btnPosts[0].url);
  console.log('提示栏:', txt(get('btn_note')));
  const after = get('btn_tbody').children;
  const stillAck = [...after].some(tr => (tr.children[6]?.children || [])
    .some(c => c.tagName === 'button' && c.textContent === '回应' && c.disabled));
  console.log('回应后按钮态:', stillAck ? 'FAIL 仍在等待且禁用' : '已恢复（下一次点击是新意图）');
  console.log('OK: 回应防抖 + 幂等键生效');
}
