// 第6周语音/文字前端的确定性检查（node 桩 DOM，无浏览器）：
//   1. 文字停止按钮路径（doCancel）：停播报文案、发"取消"给服务端、渲染取消确认与首答计时；
//   2. 意图判别（isCancelIntent）：只有 cancel 才算取消，query 不算；
//   3. 计时渲染（applyTiming）：有 timing 才输出 "⏱ 首答 Nms"，没有就不打扰；
//   4. 文字入口走同一条链路（onAsk）：取消/普通回答都走 askText，context_id 全程带上。
// 与 ui_check.mjs 同一套桩思路：脚本从 index.html 里原样抠出来跑，改的是页面就查得出。
//
// 用法： node tools/ui_voice_check.mjs
import fs from 'fs';

const HTML = new URL('../static/index.html', import.meta.url);
const code = fs.readFileSync(HTML, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

const makeEl = (tag = 'div') => ({
  tagName: tag, children: [], _text: '', className: '', style: {},
  classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
               toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); },
               contains(c) { return this._s.has(c); } },
  onclick: null, disabled: false, value: '',
  get textContent() { return this._text; },
  set textContent(v) { this._text = String(v); },
  set innerHTML(v) { if (v === '') this.children = []; },
  get innerHTML() { return this._text; },
  appendChild(c) { this.children.push(c); return c; },
  addEventListener() {},
  getContext: () => null, clientWidth: 800, width: 300, height: 0,
});

const els = new Map();
globalThis.document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl()); return els.get(id); },
  createElement: t => makeEl(t),
  readyState: 'complete', addEventListener() {}, title: '',
};
globalThis.window = { devicePixelRatio: 1, addEventListener() {} };
globalThis.location = { protocol: 'http:', search: '', href: 'http://x/' };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.setInterval = () => 0;

// --- 假服务端：按路径返回固定应答，并记录 chat 请求体 ---
const chatCalls = [];
const Resp = (status, body) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => body,
  blob: async () => ({ size: 0 }),
});
globalThis.fetch = (u, o) => {
  const url = String(u);
  if (url.includes('/api/v1/assistant/chat')) {
    chatCalls.push(JSON.parse(o.body));
    const text = JSON.parse(o.body).text || '';
    return Promise.resolve(Resp(200, text === '取消'
      ? { ok: true, intent: { action: 'cancel', device_mac: null, params: {}, reason: '取消当前任务' },
          answer: '已撤销采集指令 req_abc，不会再有结果播报。', command: null, error_code: null,
          context: { timing: { ttff_ms: 12 } } }
      : { ok: true, intent: { action: 'query', device_mac: null, params: {}, reason: 'ok' },
          answer: '最近数据来自测试设备。', command: null, error_code: null,
          context: { timing: { ttff_ms: 7 } } }));
  }
  if (url.includes('/api/v1/voice/info'))
    return Promise.resolve(Resp(200, { ok: true, device_audio: false }));
  return Promise.resolve(Resp(404, { ok: false }));
};

const api = new Function(code + '\n;return {applyTiming, isCancelIntent, doCancel, onAsk};')();
const tel = id => document.getElementById(id);
const texts = id => tel(id).children.map(c => c.textContent);
const fail = msg => { console.error('FAIL: ' + msg); process.exit(1); };
const have = (id, frag) => texts(id).some(t => t.includes(frag));

// 1) 意图判别
if (api.isCancelIntent({ intent: { action: 'cancel' } }) !== true) fail('cancel 意图没被识别');
if (api.isCancelIntent({ intent: { action: 'query' } }) !== false) fail('query 被误判成取消');
console.log('OK: isCancelIntent 只认 cancel');

// 2) 计时渲染：有 timing 才输出，没有不输出
tel('ask_out').children.length = 0;
api.applyTiming({ context: { timing: { ttff_ms: 42 } } });
if (!have('ask_out', '⏱ 首答 42ms')) fail('applyTiming 没渲染 "⏱ 首答 42ms"');
api.applyTiming({ context: { timing: null } });
if (texts('ask_out').filter(t => t.includes('⏱')).length !== 1)
  fail('无 timing 时多输出了计时行');
console.log('OK: applyTiming 只在有 timing 时输出');

// 3) 文字停止按钮路径：停播报 → 请求"取消" → 渲染取消确认与首答计时
tel('ask_out').children.length = 0;
chatCalls.length = 0;
api.doCancel();
await new Promise(r => setTimeout(r, 20));   // 等 askText 的 .then 落地
const lines = texts('ask_out');
if (!lines.some(t => t.includes('已停止播报'))) fail('doCancel 未输出"已停止播报"');
if (!lines.some(t => t.includes('已撤销采集指令'))) fail('doCancel 未渲染服务端取消确认');
if (!lines.some(t => t.includes('⏱ 首答 12ms'))) fail('doCancel 未渲染首答计时');
if (chatCalls.length !== 1 || chatCalls[0].text !== '取消') fail('doCancel 未发"取消"给服务端');
if (!/^[0-9a-f-]{36}$/.test(chatCalls[0].context_id || ''))
  fail('chat 请求没带合法的 context_id，收到: ' + JSON.stringify(chatCalls[0]));
console.log('OK: 停止按钮路径 — 停播报/发取消/渲染确认与计时');

// 4) 文字入口同链路：普通查询也带 context_id，取消走同一处
chatCalls.length = 0;
tel('ask_in').value = '查看数据';
api.onAsk();
await new Promise(r => setTimeout(r, 20));
if (chatCalls.length !== 1 || chatCalls[0].text !== '查看数据') fail('onAsk 未发查询请求');
if (!/^[0-9a-f-]{36}$/.test(chatCalls[0].context_id || '')) fail('onAsk 没带 context_id');
tel('ask_in').value = '取消';
api.onAsk();
await new Promise(r => setTimeout(r, 20));
if (chatCalls.length !== 2 || chatCalls[1].text !== '取消') fail('onAsk 取消分支没走 askText');
console.log('OK: 文字入口与语音入口共用同一条 chat 链路（均带 context_id）');

console.log('OK: 前端语音检查通过（取消/计时/意图/共用链路）');