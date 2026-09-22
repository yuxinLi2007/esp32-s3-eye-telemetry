// 第4周：自然语言助手前端。
//
// 口径：
//   - 意图、调用的接口、引擎、错误码全部**摆到界面上**，不让用户猜"它到底做了什么"。
//     助手最危险的失败是"看起来在聊天，实际却下了指令"，所以写操作必须显式标红。
//   - 数据不自己格式化时间：把服务端返回的 t_server_recv_ms 原样显示，
//     保证"查看上次"看到的就是旧时间戳，前端绝不悄悄用当前时间顶替。
//   - 错误分类只看 error.code，不解析中文——前端和测试用的是同一套稳定契约。
(function () {
  'use strict';
  const D = window.DASH;
  if (!D) { console.error('assistant.js 需要 index.html 暴露 window.DASH'); return; }
  const { $, api, el, ctlHeaders, setCtlToken } = D;

  const EXAMPLES = [
    '帮我看看上次的数据',
    '重新采集一次',
    '查看最近 50 条记录',
    '重新采集 40 次，间隔 50 毫秒',
    '帮我弄一下',
    '查一下 AA:BB:CC:DD:EE:FF 的数据',
    '帮我停止采集',
  ];

  const LEVEL_CLS = { info: 'bad', warn: 'bad', error: 'err' };

  let info = null;
  let inflight = false;
  let lastToken = null;   // 同一条问题重试复用同一个 client_token，服务端幂等

  function randToken() {
    return 'nl-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  async function nlFetch(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, ctlHeaders(),
                                 opts.headers || {});
    const r = await fetch(api(path), opts);
    if (r.status === 401) {
      const t = window.prompt('服务端要求 X-Control-Token（自然语言助手能下发采集指令）：');
      if (t) { setCtlToken(t); opts.headers = Object.assign({}, opts.headers, ctlHeaders()); return fetch(api(path), opts); }
    }
    return r;
  }

  function renderExamples() {
    const box = $('nl_examples');
    box.innerHTML = '';
    if (!info) return;
    for (const q of EXAMPLES) {
      const b = el('button', 'btn', q);
      b.type = 'button';
      b.addEventListener('click', () => { $('nl_text').value = q; ask(); });
      box.appendChild(b);
    }
    const llm = info.llm || {};
    const src = info.allowlist_source === 'DEVICE_ALLOWLIST' ? '配置白名单' : '服务端见过的设备';
    $('nl_engine_note').textContent =
      '能力：查看历史（只读）/ 请求新采集（写，会下发指令）。'
      + ' 模型：' + (llm.enabled ? ('已启用 ' + llm.model) : '未启用，走规则引擎（本地可用）')
      + '。设备白名单来源：' + src
      + (info.allowed_devices && info.allowed_devices.length
         ? '（' + info.allowed_devices.join('、') + '）' : '（暂无，先让板子上传一次）')
      + '。';
  }

  function chip(text, cls) {
    const s = el('span', 'nl-chip' + (cls ? ' ' + cls : ''), text);
    return s;
  }

  function renderResult(j) {
    const out = $('nl_out');
    out.innerHTML = '';

    const verdict = j.ok ? 'ok' : (j.error ? (LEVEL_CLS[j.error.level] || 'bad') : 'bad');
    const box = el('div', 'nl-answer ' + verdict);
    box.appendChild(el('div', null, j.answer || '(无回复)'));
    out.appendChild(box);

    const meta = el('div', 'nl-meta');
    const intent = j.intent || '—';
    const isWrite = intent === 'request_capture';
    meta.appendChild(chip('意图 ' + intent, isWrite ? 'write' : 'read'));
    if (j.action) {
      meta.appendChild(chip((j.action.method || '') + ' ' + (j.action.endpoint || ''),
                            isWrite ? 'write' : 'read'));
      if (j.action.op) meta.appendChild(chip('op=' + j.action.op, 'write'));
      if (j.action.state) meta.appendChild(chip('状态 ' + j.action.state));
      if (j.action.request_id) meta.appendChild(chip(j.action.request_id));
    }
    if (j.error) meta.appendChild(chip('错误码 ' + j.error.code, 'err'));
    if (j.trace) {
      meta.appendChild(chip('引擎 ' + ((j.trace.engine_chain || []).join('→') || j.engine)));
      // 模型通道状态：只有 degraded 是真故障（画红）；
      // not_configured / disabled / off 都是"按设计走规则"，中性色，不吓唬人。
      const lst = j.trace.llm_status;
      if (lst === 'used') {
        meta.appendChild(chip('模型 ' + (j.trace.llm_model || '') + ' 已应答', 'read'));
      } else if (lst === 'not_configured') {
        meta.appendChild(chip('模型：未配置 key，按默认走规则（非故障）'));
      } else if (lst === 'disabled') {
        meta.appendChild(chip('模型通道被配置关闭，走规则'));
      } else if (lst === 'off') {
        meta.appendChild(chip('模型通道未调用（指定 rules）'));
      } else if (lst === 'degraded' || j.trace.llm_error) {
        meta.appendChild(chip('模型降级：' + j.trace.llm_error, 'err'));
      }
      meta.appendChild(chip('用时 ' + j.trace.elapsed_ms + ' ms'));
    }
    meta.appendChild(chip('confidence ' + j.confidence));
    out.appendChild(meta);

    if (isWrite && j.action && j.action.request_id) {
      const n = el('div', 'note',
        '本次是写操作：已在服务端生成采集指令 ' + j.action.request_id
        + '，可在下方「远程采集指令」面板里看到它的完整状态流转。');
      out.appendChild(n);
    }

    if (j.data && j.data.readings && j.data.readings.length) {
      out.appendChild(renderTable(j.data));
    } else if (j.data && j.data.count === 0) {
      out.appendChild(el('div', 'note', '命中了 0 条样本——服务端如实说明，而不是编一条。'));
    }

    const det = el('details');
    det.appendChild(el('summary', null, '原始返回（结构化契约）'));
    const pre = el('div', 'nl-json');
    pre.textContent = JSON.stringify(j, null, 2);
    det.appendChild(pre);
    out.appendChild(det);
  }

  function renderTable(data) {
    const t = el('table', 'nl-table');
    const head = el('tr');
    for (const h of ['seq', 't_server_recv_ms（服务端，权威）', 't_device_ms（设备）',
                     '|a| (g)', 'SPL (dB)', '可信度', 'request_id']) {
      head.appendChild(el('th', null, h));
    }
    t.appendChild(head);
    // 只列最近 30 条，避免把页面撑爆；条数由数据本身决定，不截断真相。
    const rows = data.readings.slice(-30);
    for (const r of rows) {
      const tr = el('tr');
      tr.appendChild(el('td', null, String(r.seq)));
      tr.appendChild(el('td', null, String(r.t_server_recv_ms)
        + (r.t_server_recv_utc ? '  ' + r.t_server_recv_utc : '')));
      tr.appendChild(el('td', null, String(r.t_device_ms)));
      tr.appendChild(el('td', null, r.mag_g === null ? '—' : r.mag_g.toFixed(3)));
      tr.appendChild(el('td', null, r.spl_db === null ? '—' : String(r.spl_db)));
      tr.appendChild(el('td', null, String(r.t_trust)));
      tr.appendChild(el('td', null, r.request_id || '（连续流）'));
      t.appendChild(tr);
    }
    const wrap = el('div', 'tablewrap');
    wrap.style.marginTop = '10px';
    wrap.appendChild(t);
    const note = el('div', 'note',
      '共 ' + data.count + ' 条，表格显示最近 ' + rows.length + ' 条。'
      + (data.adjustments && data.adjustments.length
         ? '　参数调整：' + data.adjustments.join('；') : ''));
    const box = el('div');
    box.appendChild(wrap);
    box.appendChild(note);
    return box;
  }

  async function ask() {
    if (inflight) return;
    const text = $('nl_text').value.trim();
    if (!text) return;
    const mac = $('nl_mac').value.trim() || null;
    const engine = $('nl_engine').value;
    const waitS = Number($('nl_wait').value || 0);
    const key = text + '|' + mac + '|' + engine + '|' + waitS;
    if (!lastToken || lastToken.key !== key) lastToken = { key: key, token: randToken() };

    inflight = true;
    $('nl_ask').disabled = true;
    const out = $('nl_out');
    out.innerHTML = '';
    out.appendChild(el('div', 'nl-answer', '正在翻译意图并调用接口 …'
      + (engine === 'rules' ? '（规则引擎，不联网）' : '')));
    try {
      const r = await nlFetch('/api/v1/assistant/ask', {
        method: 'POST',
        body: JSON.stringify({ text: text, device_mac: mac, engine: engine,
                               wait_ms: Math.round(waitS * 1000),
                               client_token: lastToken.token }),
      });
      const j = await r.json();
      renderResult(j);
      if (j.intent === 'request_capture' && D.refreshCommands) D.refreshCommands();
    } catch (e) {
      out.innerHTML = '';
      const box = el('div', 'nl-answer err',
        '调用助手接口失败（不是助手拒绝，是根本没连上）：' + String(e.message || e));
      out.appendChild(box);
    } finally {
      inflight = false;
      $('nl_ask').disabled = false;
    }
  }

  async function loadInfo() {
    try {
      const r = await fetch(api('/api/v1/assistant/info'));
      if (!r.ok) return;
      info = await r.json();
      renderExamples();
    } catch (e) { /* 服务端没起时由主面板的报警条统一报，不在这里重复 */ }
  }

  $('nl_ask').addEventListener('click', ask);
  $('nl_text').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); ask(); }
  });
  $('nl_engine').addEventListener('change', () => {
    const v = $('nl_engine').value;
    if (v === 'llm' && info && info.llm && !info.llm.configured) {
      $('nl_engine_note').textContent =
        '未配置 OPENAI_API_KEY：选「大模型」也会自动降级到规则引擎，不会失败。'
        + ' 要启用模型，请在 server/.env 里设置 OPENAI_API_KEY（可选 OPENAI_MODEL / OPENAI_BASE_URL）。';
    } else {
      renderExamples();
    }
  });

  loadInfo();
})();
