// 第5周：语音链路前端 —— 录音 / 识别 / 任务执行 / 播放 四态可见。
//
// 口径：
//   - 四个状态各自有 badge，失败态有独立文案；文案全部来自 /api/v1/voice/info
//     的错误码表，前端不写死中文，和服务端测试断言的是同一份契约。
//   - 识别文本只是"一句用户文本"：任务执行直接调第4周 /assistant/ask，
//     与文字入口同一个端点、同一个信封。"对照"按钮就是把同一句话再走一次
//     文字入口，把两边结果并排摆出来——相同才叫复用，不同就是bug。
//   - 语音链路任何失败都不碰文字入口：nl_text 的 disabled 永远是 false，
//     ui_check 里有断言守着。
//   - 播放发生在浏览器里，服务端只能记"浏览器回告了什么"：播完/播失败都
//     POST /voice/events/{id}/played，引擎与位置如实回告（server/browser）。
//   - 没有麦克风能力（无 getUserMedia / 无 MediaRecorder）不等于功能坏掉：
//     录音按钮禁用并说明原因，"上传授权录音"入口始终可用（课程备用路径）。
(function () {
  'use strict';
  const D = window.DASH;
  if (!D) { console.error('voice.js 需要 index.html 暴露 window.DASH'); return; }
  const { $, api, el, ctlHeaders, setCtlToken } = D;

  const LEVEL_CLS = { warn: 'a-warn', crit: 'a-crit' };
  const STATE_TEXT = {
    idle: '待命', recording: '录音中', recognizing: '识别中',
    executing: '任务执行中', playing: '播放中', done: '完成',
  };

  let info = null;            // /api/v1/voice/info 的事实
  let ERR = {};               // code -> {level, message}
  let recorder = null;        // 当前 MediaRecorder；null = 没在录
  let starting = false;       // 防抖：getUserMedia 还在飞时忽略连点
  let chunks = [];
  let recStartedAt = 0;
  let timeline = [];          // [{name, ms}]
  let lastEventId = null;
  let lastVoiceResult = null; // {intent, ok, answer} 供对照

  function note(msg, level) {
    const box = $('vc_err');
    box.innerHTML = '';
    if (!msg) return;
    const d = el('div', LEVEL_CLS[level] || 'a-warn', msg);
    d.style.padding = '6px 8px';
    d.style.marginTop = '6px';
    box.appendChild(d);
  }

  function setState(s) {
    const b = $('vc_state');
    b.textContent = STATE_TEXT[s] || s;
    b.className = 'badge ' + (s === 'idle' || s === 'done' ? 'b-done'
      : s === 'recording' ? 'b-running' : 'b-claimed');
    $('vc_record').disabled = (s !== 'idle' && s !== 'done' && s !== 'recording')
      || !micCapable();
    if (s === 'idle' || s === 'done') $('vc_record').textContent = '开始录音';
    if (s === 'recording') $('vc_record').textContent = '结束录音';
  }

  function micCapable() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
              && window.MediaRecorder);
  }

  function pushStage(name, ms) {
    timeline.push({ name: name, ms: ms == null ? null : Math.round(ms) });
    const box = $('vc_timeline');
    box.innerHTML = '';
    timeline.forEach(function (t) {
      box.appendChild(el('span', 'badge b-running',
        t.name + (t.ms == null ? '' : ' ' + t.ms + 'ms')));
      box.appendChild(document.createTextNode(' '));
    });
  }

  // FormData（multipart 上传）绝不能带 Content-Type：ctlHeaders() 里那份
  // application/json 会顶掉 fetch 自动生成的 multipart boundary，服务端会
  // 解析不到 file 字段报 422（ui_check 真抓到过这个 bug）。只带令牌。
  function vHeaders(opts) {
    const h = Object.assign({}, ctlHeaders(), (opts && opts.headers) || {});
    if (opts && typeof FormData !== 'undefined' && opts.body instanceof FormData)
      delete h['Content-Type'];
    return h;
  }

  async function vFetch(path, opts) {
    opts = opts || {};
    opts.headers = vHeaders(opts);
    const r = await fetch(api(path), opts);
    if (r.status === 401) {
      const t = window.prompt('服务端设置了 CONTROL_TOKEN，语音链路（能触发采集）需要它：');
      if (!t) throw new Error('未提供 CONTROL_TOKEN');
      setCtlToken(t);
      opts.headers = vHeaders(opts);
      return fetch(api(path), opts);
    }
    return r;
  }

  function showErr(e, stage) {
    const msg = (ERR[e.code] && ERR[e.code].message) || e.message || e.code;
    note('[' + (stage || 'voice') + '] ' + msg + '（错误码 ' + e.code + '）',
         (ERR[e.code] && ERR[e.code].level) || 'crit');
  }

  // ------------------------------------------------------------- 录音
  async function startRec() {
    if (starting || recorder) return;      // 防抖：连点只生效一次
    starting = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recorder = new MediaRecorder(stream);   // 容器/码率由浏览器决定，如实展示
      chunks = [];
      recorder.ondataavailable = function (ev) { if (ev.data && ev.data.size) chunks.push(ev.data); };
      recorder.onstop = function () { onStopped(); };
      recStartedAt = Date.now();
      recorder.start();
      timeline = [];
      note('', null);
      setState('recording');
      pushStage('录音', null);
    } catch (e) {
      note('麦克风不可用：' + (e && e.name ? e.name : e)
           + '。可改用下方"上传授权录音"走同一条识别链路。', 'warn');
    } finally {
      starting = false;
    }
  }

  function stopRec() {
    if (!recorder) return;
    const r = recorder;
    recorder = null;
    if (r.state !== 'inactive') r.stop();
    r.stream.getTracks().forEach(function (t) { t.stop(); });
  }

  async function onStopped() {
    const dur = Date.now() - recStartedAt;
    const blob = new Blob(chunks, { type: (recorderMime() || 'audio/webm') });
    await runChain(blob, $('vc_source').value, dur);
  }

  function recorderMime() {
    return (window.MediaRecorder && MediaRecorder.isTypeSupported
            && MediaRecorder.isTypeSupported('audio/webm;codecs=opus'))
      ? 'audio/webm;codecs=opus' : '';
  }

  // ------------------------------------------------------------- 主链路
  async function runChain(blob, source, clientMs) {
    setState('recognizing');
    const t0 = Date.now();
    const fd = new FormData();
    fd.append('file', blob, 'voice.' + ((blob.type.split('/')[1] || 'webm').split(';')[0]));
    fd.append('audio_source', source);
    if (clientMs != null) fd.append('client_duration_ms', String(clientMs));
    let tr, trStatus;
    try {
      const r = await vFetch('/api/v1/voice/transcribe', { method: 'POST', body: fd });
      trStatus = r.status;
      tr = await r.json();
    } catch (e) {
      setState('idle');
      note('识别请求本身失败（网络？）：' + e, 'crit');
      return;
    }
    pushStage('识别', tr.latency_ms != null ? tr.latency_ms : Date.now() - t0);
    if (!tr.ok) {
      lastEventId = tr.event_id;
      if (tr.error && tr.error.code) {
        showErr(tr.error, '识别');
      } else {
        // 响应不在契约内（缺 error 字段）也是必须可见的失败，不能静默吞掉
        note('[识别] 服务端响应异常（HTTP ' + trStatus + '，缺 error 字段）：'
             + JSON.stringify(tr).slice(0, 200), 'crit');
      }
      setState('idle');
      refreshEvents();
      return;
    }
    lastEventId = tr.event_id;
    $('vc_transcript').textContent = '识别原文：' + tr.text
      + '（引擎 ' + tr.engine + '，位置 ' + tr.run_location.recognition + '）';

    setState('executing');
    const t1 = Date.now();
    let env;
    try {
      const r2 = await vFetch('/api/v1/assistant/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: tr.text }),
      });
      env = await r2.json();
    } catch (e) {
      setState('idle');
      note('任务执行请求失败（网络？）：' + e, 'crit');
      return;
    }
    pushStage('执行', Date.now() - t1);
    lastVoiceResult = { intent: env.intent, ok: env.ok, answer: env.answer };
    const data = env.data || {};
    $('vc_answer').textContent = '回答：' + env.answer
      + '（意图 ' + env.intent + '，引擎 ' + env.engine + '）';
    try {
      await vFetch('/api/v1/voice/events/' + lastEventId + '/bind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          intent: env.intent, assistant_ok: !!env.ok,
          request_id: data.request_id || null,
        }),
      });
    } catch (e) { /* 回告失败不影响主链路，事件行里缺 intent 而已 */ }

    await speak(env.answer || '');
    refreshEvents();
  }

  // ------------------------------------------------------------- 播放
  async function speak(text) {
    setState('playing');
    pushStage('播放', null);
    let r;
    try {
      r = await vFetch('/api/v1/voice/speak?text=' + encodeURIComponent(text));
    } catch (e) {
      finishPlay(false, 'browser_speechsynthesis', 'browser');
      note('合成请求失败：' + e, 'crit');
      setState('done');
      return;
    }
    const ct = (r.headers.get('content-type') || '');
    if (r.ok && ct.indexOf('audio') === 0) {
      const engine = r.headers.get('X-Voice-Engine') || 'openai_tts';
      try {
        const blob = await r.blob();
        const au = new Audio(URL.createObjectURL(blob));
        au.onended = function () { finishPlay(true, engine, 'server'); setState('done'); };
        au.onerror = function () { finishPlay(false, engine, 'server'); setState('done'); };
        await au.play();
      } catch (e) {
        finishPlay(false, engine, 'server');
        setState('done');
      }
      return;
    }
    // 503：服务端合成不可用 -> 浏览器合成兜底（降级，不是失败）
    let body = {};
    try { body = await r.json(); } catch (e) {}
    if (body.error) showErr(body.error, '合成');
    if (window.speechSynthesis) {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      u.onend = function () { finishPlay(true, 'browser_speechsynthesis', 'browser'); setState('done'); };
      u.onerror = function () { finishPlay(false, 'browser_speechsynthesis', 'browser'); setState('done'); };
      speechSynthesis.speak(u);
    } else {
      finishPlay(false, 'browser_speechsynthesis', 'browser');
      note('浏览器也不支持语音合成，本次只有文字回答。', 'warn');
      setState('done');
    }
  }

  function finishPlay(ok, engine, location) {
    if (lastEventId == null) return;
    vFetch('/api/v1/voice/events/' + lastEventId + '/played', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ played: !!ok, tts_engine: engine, tts_location: location }),
    }).catch(function () {});
  }

  // ------------------------------------------------------------- 对照
  async function compare() {
    const text = (lastVoiceResult && lastTranscript()) || '';
    if (!text) { note('先完成一次语音链路，才能做同句对照。', 'warn'); return; }
    const r = await vFetch('/api/v1/assistant/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text }),
    });
    const env = await r.json();
    const box = $('vc_compare_out');
    box.innerHTML = '';
    const same = env.intent === lastVoiceResult.intent
              && !!env.ok === !!lastVoiceResult.ok;
    const tbl = el('table');
    tbl.innerHTML = '<thead><tr><th class="l">入口</th><th class="l">意图</th>'
      + '<th class="l">ok</th><th class="l">回答</th></tr></thead>';
    const tb = el('tbody');
    [['语音', lastVoiceResult], ['文字', env]].forEach(function (row) {
      const tr = el('tr');
      [row[0], row[1].intent, String(!!row[1].ok), row[1].answer]
        .forEach(function (c, i) { tr.appendChild(el(i ? 'td' : 'td', i ? '' : 'l', c)); });
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    box.appendChild(tbl);
    box.appendChild(el('div', same ? 'b-done badge' : 'b-failed badge',
      same ? '两入口结果一致' : '两入口结果不一致（这是bug，请报）'));
  }

  function lastTranscript() {
    const t = $('vc_transcript').textContent || '';
    return t.replace(/^识别原文：/, '').split('（引擎')[0];
  }

  // ------------------------------------------------------------- 事件表
  async function refreshEvents() {
    try {
      const r = await fetch(api('/api/v1/voice/events?limit=8'));
      const j = await r.json();
      const tb = $('vc_tbody');
      tb.innerHTML = '';
      (j.events || []).forEach(function (ev) {
        const src = (info && (info.sources || []).find(function (s) { return s.id === ev.audio_source; }) || {}).label || ev.audio_source;
        const tr = el('tr');
        const playedTxt = ev.played === 1 ? '已播放' : ev.played === 0 ? '播放失败' : '未播放';
        [String(ev.id), D.fmtTime(ev.t_server_ms), src,
         ev.recognition_engine ? ev.recognition_engine + '/' + ev.recognition_location : '-',
         (ev.transcript || '').slice(0, 24) || '-',
         ev.intent || '-', playedTxt,
         ev.error_code ? ev.error_code + (ev.error_stage ? '@' + ev.error_stage : '') : '-',
         (ev.tts_engine || '-') + '/' + (ev.tts_location || '-')]
          .forEach(function (c, i) { tr.appendChild(el('td', i ? '' : 'l', c)); });
        tb.appendChild(tr);
      });
    } catch (e) { /* 事件表是旁路，拉不到不影响主链路 */ }
  }

  // ------------------------------------------------------------- 初始化
  async function init() {
    try {
      const r = await fetch(api('/api/v1/voice/info'));
      info = await r.json();
    } catch (e) {
      note('拉取语音能力自述失败：' + e, 'crit');
      return;
    }
    ERR = {};
    (info.error_codes || []).forEach(function (c) { ERR[c.code] = c; });
    const sel = $('vc_source');
    sel.innerHTML = '';
    (info.sources || []).forEach(function (s) {
      const o = el('option');
      o.value = s.id;
      o.textContent = s.label;
      sel.appendChild(o);
    });
    const rec = info.recognition || {}, tts = info.tts || {};
    $('vc_engine_note').textContent =
      '识别：' + rec.engine + '（' + (rec.configured ? '已配置' : '未配置') + '，跑在 '
      + rec.run_location + '）；合成：' + tts.engine + '（'
      + (tts.configured ? '已配置' : '未配置，降级 ' + tts.fallback_engine + ' 跑在 '
        + tts.fallback_location) + '）。' + (info.note || '');
    if (!rec.configured) {
      note(ERR.voice_not_configured ? ERR.voice_not_configured.message
           : '未配置语音服务', 'warn');
    }
    if (!micCapable()) {
      $('vc_record').disabled = true;
      note('当前环境没有麦克风能力（无 getUserMedia/MediaRecorder）。'
           + '录音按钮禁用；请用"上传授权录音"走同一条识别链路。', 'warn');
    }
    // 用 onclick 而不是 addEventListener：与 button.js 同风格，
    // 也让 tools/ui_check.mjs 能真的"点"到这颗按钮做防抖断言。
    $('vc_record').onclick = function () {
      if (recorder) stopRec(); else startRec();
    };
    $('vc_upload').onchange = function (ev) {
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      runChain(f, 'authorized_recording', null);
      ev.target.value = '';
    };
    $('vc_compare').onclick = compare;
    setState('idle');
    refreshEvents();
    setInterval(refreshEvents, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
