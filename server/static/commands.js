// 第2周：Web 远程采集指令面板。
//
// 三条口径延续第1周：
//   - 参数范围不另抄一份：按钮和输入框的上下限全部来自 GET /api/v1/commands/ops，
//     服务端改了范围，界面跟着变，不会出现"前端能填、后端拒绝"的错位。
//   - 失败必须可见：下发失败、被幂等去重、设备报错、超时、样本数对不上，
//     每一种都在界面上有它自己的样子，绝不静默。
//   - 状态不自己记：按钮可用性、进度、耗时全部由轮询回来的数据决定。
//     自己记一份状态的话，另一个标签页下发的指令在这里就看不见了。
(function () {
  'use strict';
  const D = window.DASH;
  if (!D) { console.error('commands.js 需要 index.html 暴露 window.DASH'); return; }
  const { $, api, el, fmtTime, fmtAge, setAlarm } = D;

  const POLL_MS = 2000;          // 与服务端惰性结算的节奏对齐（status 每 2 秒被轮询一次）
  const MAC_RE = /^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/;

  let ops = null;                // /commands/ops 的原始清单
  let maxLive = 8;
  let maxCaptureMs = 10000;
  let cmds = [];                 // 最近若干条指令
  let stats = null;
  let busyOp = null;             // 正在下发的 op：按钮防抖的第一层
  let intent = null;             // { key, token } 同一次点击的重试复用同一个 token
  let selected = null;           // 展开详情的 request_id
  let detail = null;             // 选中指令的完整数据（含 events）
  let samples = null;            // 选中指令采到的样本
  let inflight = false;
  let lastError = null;

  // ------------------------------------------------------------ 小工具
  const STATE_CLS = { pending:'b-pending', claimed:'b-claimed', running:'b-running',
                      done:'b-done', failed:'b-failed', expired:'b-expired',
                      timeout:'b-timeout', cancelled:'b-cancelled' };

  function randToken() {
    // 幂等键：一次用户意图一个。用时间+随机数，不依赖 crypto（file:// 下可能没有）
    return 'web-' + Date.now().toString(36) + '-' +
           Math.random().toString(36).slice(2, 10);
  }

  function note(msg, bad) {
    const n = $('cmd_note');
    n.textContent = msg || '';
    n.style.color = bad ? 'var(--crit)' : 'var(--dim)';
  }

  async function ctlFetch(path, opts) {
    // 401 时现问一次令牌再记住——和第1周的采集开关按钮同一套逻辑、同一个存储键，
    // 用户只需要输入一次。网页里不可能预置密钥：HTML 本身是要发出去的。
    const doIt = () => fetch(api(path), Object.assign({}, opts, {
      headers: Object.assign({ 'Content-Type': 'application/json' },
                             D.ctlToken ? { 'X-Control-Token': D.ctlToken } : {},
                             (opts && opts.headers) || {}),
    }));
    let r = await doIt();
    if (r.status === 401) {
      const t = window.prompt('服务端设置了 CONTROL_TOKEN，下发远程指令需要它：');
      if (!t) throw new Error('未提供 CONTROL_TOKEN');
      D.setCtlToken(t);
      r = await doIt();
    }
    return r;
  }

  async function readError(r) {
    let j = null;
    try { j = await r.json(); } catch (e) { /* 非 JSON 响应，下面就用状态码说话 */ }
    if (j && j.error) return j.error.code + '：' + j.error.message;
    if (j && j.detail) return typeof j.detail === 'string' ? j.detail
                         : (j.detail.code ? j.detail.code + '：' + j.detail.message
                                          : JSON.stringify(j.detail));
    return 'HTTP ' + r.status;
  }

  // ------------------------------------------------------------ 下发
  function opSpec(op) {
    return ops ? ops.find(o => o.op === op) : null;
  }

  function collectParams(op) {
    const spec = opSpec(op);
    const out = {};
    for (const p of (spec ? spec.params : [])) {
      const input = $('cmd_p_' + p.name);
      const raw = input.value.trim();
      const v = parseInt(raw, 10);
      if (!/^-?\d+$/.test(raw)) { note(p.name + ' 必须是整数，当前是 "' + raw + '"', true); return null; }
      if (p.lo !== null && v < p.lo) { note(p.name + ' 不能小于 ' + p.lo, true); return null; }
      if (p.hi !== null && v > p.hi) { note(p.name + ' 不能大于 ' + p.hi, true); return null; }
      out[p.name] = v;
      input.classList.remove('bad');
    }
    if (op === 'capture' && out.n !== undefined && out.interval_ms !== undefined) {
      const dur = out.n * out.interval_ms;
      if (dur > maxCaptureMs) {
        note('n × interval_ms = ' + dur + ' ms，超过上限 ' + maxCaptureMs + ' ms', true);
        return null;
      }
    }
    return out;
  }

  async function send(op) {
    if (busyOp) return;                       // 防抖第一层：一次只允许一个下发在飞
    const mac = $('cmd_mac').value.trim().toUpperCase();
    if (!MAC_RE.test(mac)) {
      note('目标设备 MAC 非法：应形如 AA:BB:CC:DD:EE:FF（当前 "' + mac + '"）', true);
      return;
    }
    const params = collectParams(op);
    if (params === null) return;
    const noteTxt = $('cmd_note_text').value.trim().slice(0, 200) || null;

    // 防抖第二层：同一次点击的所有重试带同一个 client_token，服务端按
    // (device_mac, op, client_token) 唯一约束去重。按钮禁用挡不住刷新页面、
    // 多标签页和直接 curl，服务端那道才是真的。
    const key = mac + '|' + op + '|' + JSON.stringify(params);
    if (!intent || intent.key !== key) intent = { key, token: randToken() };

    busyOp = op;
    renderOps();
    note('正在下发 ' + op + ' …');
    try {
      const r = await ctlFetch('/api/v1/commands', {
        method: 'POST',
        body: JSON.stringify({ device_mac: mac, op, params,
                               client_token: intent.token, note: noteTxt }),
      });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      const c = j.command;
      selected = c.request_id; detail = null; samples = null;
      if (j.deduped) {
        // 200 + deduped 表示"这次点击没有产生新指令"。这不是错误，但必须说出来：
        // 用户以为发了两条、实际只有一条，界面若沉默就会让人怀疑按钮坏了。
        note('重复提交已被服务端去重，仍是同一条指令 ' + c.request_id +
             '（状态 ' + c.state_text + '）', false);
      } else {
        note('已下发 ' + c.request_id + '　' + c.state_text +
             '　设备最坏 ' + (POLL_MS / 1000) + ' s 后领取', false);
        intent = null;                        // 意图已完成，下一次点击是新意图
      }
      await refresh(true);
    } catch (e) {
      lastError = String(e && e.message || e);
      note('下发失败：' + lastError + '（指令未产生，可重试；重试会用同一个幂等键）', true);
    } finally {
      busyOp = null;
      renderOps();
    }
  }

  async function cancel(rid) {
    if (!window.confirm('撤销 ' + rid + ' ？\n只有"等待设备领取"的指令能撤销；已被领取的不能抽走。')) return;
    try {
      const r = await ctlFetch('/api/v1/commands/' + rid + '/cancel', { method: 'POST' });
      if (!r.ok) throw new Error(await readError(r));
      note('已撤销 ' + rid, false);
      await refresh(true);
    } catch (e) {
      note('撤销失败：' + (e.message || e), true);
    }
  }

  // ------------------------------------------------------------ 第8周：视觉识别与反馈
  // 标签 → 人话（界面显示用；"分数未校准"的口径在 renderInfer 里固定出现）
  function labText(l) {
    return { usable: '可用', occluded: '遮挡', blurred: '模糊',
             unknown: '未知' }[l] || l;
  }

  async function analyzeSnap(sid, task) {
    const ib = $('snap_infer_box');
    if (!ib) return;
    ib.textContent = '正在分析（未配置真实模型时按"回放"处理，会在结果里标注）…';
    try {
      const r = await ctlFetch('/api/v1/snapshots/' + sid + '/analyze', {
        method: 'POST',
        body: JSON.stringify({ task: task || 'usability' }),
      });
      if (!r.ok) throw new Error(await readError(r));
      renderInfer(ib, (await r.json()).inference);
      checkAsk(sid);                    // 分析结果若触发主动询问，立即亮横幅
    } catch (e) {
      ib.textContent = '识别失败：' + String(e && e.message || e);
    }
  }

  function renderInfer(ib, inf) {
    ib.innerHTML = '';
    if (!inf) { ib.textContent = '没有推理结果。'; return; }
    const kv = el('div', 'kv');
    const add = (k, v) => { kv.appendChild(el('div', 'k', k));
                            kv.appendChild(el('div', 'v', v === null || v === undefined ? '—' : String(v))); };
    if (inf.status === 'error') {
      add('识别状态', '失败（' + inf.error_code + '）');
      add('错误信息', inf.error_message);
      ib.appendChild(kv);
      return;
    }
    add('识别状态', inf.status === 'rejected' ? '拒识（模型无法判断）' : '有结果');
    add('标签', labText(inf.label) + '（' + inf.label + '）');
    add('分数', inf.score === null ? '—'
        : inf.score.toFixed(2) + '　未校准（模型置信度，不是现实正确率）');
    add('模型版本', inf.model_version);
    add('推理来源', inf.source === 'model'
        ? '真实模型推理' : '回放标注（非真实推理，仅界面演示）');
    add('分析时刻', fmtTime(inf.t_server_ms) + '（服务端时间）');
    ib.appendChild(kv);
    if (inf.score !== null && inf.score < 0.45) {
      ib.appendChild(el('div', 'note', '分数偏低，结果不太确定：可点下方"重拍一张"补拍。'));
    }
  }

  async function correctSnap(sid, value) {
    try {
      const r = await ctlFetch('/api/v1/snapshots/' + sid + '/corrections', {
        method: 'POST',
        body: JSON.stringify({ kind: 'label', value, note: 'ui 人工纠正' }),
      });
      if (!r.ok) throw new Error(await readError(r));
      const ib = $('snap_infer_box');
      if (ib && ib.childNodes.length) {
        ib.appendChild(el('div', 'note',
          '已记录纠正：该画面现在按你的判断显示为 "'
          + labText(value) + '（' + value + '）"。'));
      }
      await refresh(true);
    } catch (e) {
      note('纠正失败：' + String(e && e.message || e), true);
    }
  }

  async function recaptureSnap(sid) {
    try {
      const r = await ctlFetch('/api/v1/snapshots/' + sid + '/recapture', {
        method: 'POST', body: '{}',
      });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      note('已重拍：' + j.command.request_id + '　' + j.command.state_text, false);
      await refresh(true);
    } catch (e) {
      note('重拍失败：' + String(e && e.message || e), true);
    }
  }

  // ------------------------------------------------------------ 第9周：主动询问
  async function checkAsk(sid) {
    // 该画面有未表态的"要重拍吗"询问 → 亮出横幅（新 id snap_ask_box）
    try {
      const r = await fetch(api('/api/v1/asks?pending=1'), { cache: 'no-store' });
      if (!r.ok) return;
      const j = await r.json();
      const a = (j.asks || []).find(x => x.snapshot_id === sid && x.state === 'open');
      const box = $('snap_ask_box');
      if (box) box.style.display = a ? '' : 'none';
    } catch (e) { /* 横幅是增强，不因它报错 */ }
  }

  async function decideAsk(sid, decision) {
    // confirm → 服务端立即下发重拍指令（同设备新 request_id）；ignore → 冷却期不打扰
    try {
      const r = await ctlFetch('/api/v1/snapshots/' + sid + '/ask', {
        method: 'POST',
        body: JSON.stringify({ decision }),
      });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      if (decision === 'confirm' && j.recapture) {
        note('已确认重拍：' + j.recapture.request_id + '　' +
             (j.recapture.state_text || ''), false);
      } else {
        note('已忽略：本轮不再打扰（冷却期内同类画面不重复询问）', false);
      }
      const box = $('snap_ask_box');
      if (box) box.style.display = 'none';
      await refresh(true);
    } catch (e) {
      note('询问处理失败：' + String(e && e.message || e), true);
    }
  }

  async function showDetail(rid) {
    selected = (selected === rid) ? null : rid;
    detail = null; samples = null;
    if (selected) {
      try {
        const r = await fetch(api('/api/v1/commands/' + selected), { cache: 'no-store' });
        if (!r.ok) throw new Error(await readError(r));
        detail = (await r.json()).command;
        if (detail.n_samples > 0) await loadSamples(selected);
      } catch (e) {
        note('取详情失败：' + (e.message || e), true);
      }
    }
    renderDetail();
  }

  async function loadSamples(rid) {
    const r = await fetch(api('/api/v1/readings?request_id=' + encodeURIComponent(rid) +
                              '&limit=500'), { cache: 'no-store' });
    if (!r.ok) throw new Error(await readError(r));
    samples = await r.json();
  }
  // ------------------------------------------------------------ 渲染
  function paramSummary(c) {
    const p = c.params || {};
    const ks = Object.keys(p);
    if (!ks.length) return c.op;
    return c.op + '　' + ks.map(k => k + '=' + p[k]).join(' ');
  }

  function resultSummary(c) {
    if (c.op === 'image' && c.state === 'done') {
      const r = c.result || {};
      return r.captured_at_ms != null
        ? '已获取画面 ' + fmtTime(r.captured_at_ms) + '（'
          + (r.source === 'replay' ? '回放' : '实拍') + '）'
        : '图像完成（无图像回执）';
    }
    if (c.state === 'failed' || c.state === 'timeout') {
      const code = c.error_code || (c.state === 'timeout' ? 'timeout' : '');
      const msg = c.error_message || '';
      return (code ? '[' + code + '] ' : '') + msg;
    }
    if (c.state === 'expired') return 'ttl ' + c.ttl_ms + ' ms 内没有设备来领（离线/MAC 写错/固件太旧）';
    if (c.state === 'cancelled') return '用户撤销';
    const r = c.result;
    if (!r) return c.state === 'done' ? '成功（无结果体）' : '—';
    // 结果体字段随 op 不同，这里挑最有信息量的几个显示，完整内容在详情里。
    const pick = [];
    for (const k of ['n_acked', 'n_sampled', 'spl_avg_db', 'mag_avg', 'uptime_s',
                     'rssi_dbm', 'free_heap', 'ring_count', 'accel_verdict',
                     'mic_verdict', 'collect_enabled']) {
      if (r[k] !== undefined && r[k] !== null) pick.push(k + '=' + r[k]);
    }
    return pick.length ? pick.join('　') : JSON.stringify(r).slice(0, 120);
  }

  function renderOps() {
    const box = $('cmd_ops');
    box.innerHTML = '';
    if (!ops) { box.appendChild(el('span', 'note', '正在读取指令清单 …')); return; }
    for (const spec of ops) {
      if (spec.op === 'capture') {
        for (const p of spec.params) {
          const lab = el('label', null, p.name);
          const inp = el('input');
          inp.type = 'number';
          inp.id = 'cmd_p_' + p.name;
          inp.value = p.default;
          inp.min = p.lo; inp.max = p.hi;
          inp.disabled = !!busyOp;
          box.appendChild(lab); box.appendChild(inp);
        }
      }
      const b = el('button', 'btn op', spec.op);
      b.title = spec.desc;
      b.disabled = !!busyOp;
      b.textContent = busyOp === spec.op ? spec.op + ' …' : spec.op;
      b.onclick = () => send(spec.op);
      box.appendChild(b);
    }
  }

  function renderStats() {
    const s = stats;
    if (!s) { $('cmd_stats').textContent = ''; return; }
    const live = s.pending + s.claimed + s.running;
    $('cmd_stats').textContent =
      '在飞 ' + live + '/' + maxLive + '（等待 ' + s.pending + '、已领 ' + s.claimed +
      '、执行中 ' + s.running + '）　终态：成功 ' + s.done + '、设备报错 ' + s.failed +
      '、超时 ' + s.timeout + '、超期未领 ' + s.expired + '、撤销 ' + s.cancelled;
    $('cmd_stats').style.color = live >= maxLive ? 'var(--warn)' : 'var(--dim)';
  }

  function renderTable() {
    const tb = $('cmd_tbody');
    tb.innerHTML = '';
    if (!cmds.length) {
      const tr = el('tr'); const td = el('td', 'l', '还没有下发过任何指令');
      td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr);
      return;
    }
    for (const c of cmds) {
      const tr = el('tr', c.request_id === selected ? 'peer' : '');
      const pct = c.progress === null || c.progress === undefined ? 0 : c.progress;
      const live = !c.is_terminal;

      const tdId = el('td', 'l');
      const a = el('a', null, c.request_id);
      a.href = '#'; a.style.color = 'inherit';
      a.onclick = ev => { ev.preventDefault(); showDetail(c.request_id); };
      tdId.appendChild(a);
      tdId.appendChild(el('div', 'note', fmtTime(c.t_created_ms)));
      tr.appendChild(tdId);

      tr.appendChild(el('td', 'l', paramSummary(c)));

      const tdS = el('td', 'l');
      tdS.appendChild(el('span', 'badge ' + (STATE_CLS[c.state] || ''), c.state_text || c.state));
      if (live && c.remaining_ms !== null) {
        tdS.appendChild(el('div', 'note', '剩余 ' + fmtAge(c.remaining_ms)));
      }
      if (c.requeues) tdS.appendChild(el('div', 'note', '重排队 ' + c.requeues + ' 次'));
      tr.appendChild(tdS);

      const tdP = el('td', 'l');
      const bar = el('div', 'bar');
      const fill = el('i');
      fill.style.width = (c.is_terminal && c.state === 'done' ? 100 : pct) + '%';
      if (c.state === 'failed' || c.state === 'timeout') fill.style.background = 'var(--crit)';
      bar.appendChild(fill);
      tdP.appendChild(bar);
      tdP.appendChild(el('div', 'note', live ? (pct + '%') : (c.progress === null ? '—' : pct + '%')));
      tr.appendChild(tdP);

      const tdT = el('td');
      tdT.textContent = fmtAge(c.elapsed_ms);
      if (c.queue_ms !== null) tdT.title = '排队 ' + fmtAge(c.queue_ms) +
        (c.exec_ms !== null ? '，执行 ' + fmtAge(c.exec_ms) : '');
      tr.appendChild(tdT);

      // 样本数并排显示"服务端实收 / 设备自报"。对不上就是上传掉了，
      // 只显示一个数的话，state=done 会把丢数据说成一切正常。
      const tdN = el('td');
      const srv = c.n_samples === null ? '—' : c.n_samples;
      const dev = c.n_samples_device === null ? '—' : c.n_samples_device;
      tdN.textContent = srv + ' / ' + dev;
      if (c.sample_count_mismatch) {
        tdN.classList.add('mismatch');
        tdN.title = '设备自报与服务端实收不一致，有样本在上传途中丢了';
      }
      tr.appendChild(tdN);

      const tdR = el('td', 'l', resultSummary(c));
      tdR.style.maxWidth = '420px';
      tdR.style.whiteSpace = 'normal';
      if (c.state === 'failed' || c.state === 'timeout') tdR.style.color = 'var(--crit)';
      tr.appendChild(tdR);

      const tdA = el('td', 'l');
      const b1 = el('button', 'btn', c.request_id === selected ? '收起' : '详情');
      b1.onclick = () => showDetail(c.request_id);
      tdA.appendChild(b1);
      if (c.state === 'pending') {
        const b2 = el('button', 'btn danger', '撤销');
        b2.style.marginLeft = '6px';
        b2.onclick = () => cancel(c.request_id);
        tdA.appendChild(b2);
      }
      tr.appendChild(tdA);
      tb.appendChild(tr);
    }
  }

  function renderDetail() {
    const box = $('cmd_detail');
    box.innerHTML = '';
    if (!selected) return;
    const c = detail;
    const wrap = el('div', 'cmd-detail');
    if (!c) { wrap.appendChild(el('div', 'note', '正在取 ' + selected + ' 的详情 …'));
              box.appendChild(wrap); return; }

    const kv = el('div', 'kv');
    const add = (k, v) => { kv.appendChild(el('div', 'k', k));
                            kv.appendChild(el('div', 'v', v === null || v === undefined ? '—' : String(v))); };
    add('request_id', c.request_id);
    add('状态', (c.state_text || c.state) + (c.is_terminal ? '（终态，不会再变）' : ''));
    add('目标设备', c.device_mac);
    add('指令与参数', paramSummary(c));
    add('下发时刻', fmtTime(c.t_created_ms) + '（服务端时间，权威）');
    add('领取时刻', c.t_claimed_ms ? fmtTime(c.t_claimed_ms) + '（排队 ' + fmtAge(c.queue_ms) + '）' : '未被领取');
    add('执行者', c.claimed_boot_id ? 'boot_id=' + c.claimed_boot_id + '　fw=' + (c.claimed_fw || '—') : '—');
    add('结束时刻', c.t_finished_ms ? fmtTime(c.t_finished_ms) + '（执行 ' + fmtAge(c.exec_ms) + '）' : '—');
    add('尝试次数', c.attempts + ' / 上限 ' + c.max_attempts + '（重排队 ' + c.requeues + ' 次）');
    add('样本数', '服务端实收 ' + (c.n_samples === null ? '—' : c.n_samples) +
        '　设备自报 ' + (c.n_samples_device === null ? '—' : c.n_samples_device) +
        (c.sample_count_mismatch ? '　不一致，有样本在上传途中丢了' : ''));
    add('备注', c.note);
    if (c.error_code) add('错误码', c.error_code + (c.error_message ? '　' + c.error_message : ''));
    wrap.appendChild(kv);

    if (c.result) {
      wrap.appendChild(el('h3', null, '结果体'));
      const pre = el('pre', 'json', JSON.stringify(c.result, null, 2));
      wrap.appendChild(pre);
    }

    // 第7周：按需环境快照 —— 图像本体 + 采集时刻（服务端接收为权威，设备声称作参考）。
    if (c.op === 'image' && c.result && c.result.image_id != null) {
      const r = c.result;
      wrap.appendChild(el('h3', null, '环境快照（本次 request_id=' + c.request_id + '）'));
      const img = el('img', 'snap');
      img.src = api('/api/v1/snapshots/' + r.image_id + '/image?ts=' + r.captured_at_ms);
      img.alt = '环境快照 ' + c.request_id;
      img.style.maxWidth = '320px'; img.style.maxHeight = '240px';
      img.style.border = '1px solid var(--line)'; img.style.display = 'block';
      wrap.appendChild(img);
      const kv2 = el('div', 'kv');
      const add2 = (k, v) => { kv2.appendChild(el('div', 'k', k));
                                kv2.appendChild(el('div', 'v', v === null || v === undefined ? '—' : String(v))); };
      add2('采集时刻', fmtTime(r.captured_at_ms) + '（服务端接收，权威）'
           + (r.t_captured_ms ? '；设备声称 ' + fmtTime(r.t_captured_ms) : ''));
      add2('图像来源', r.source === 'replay'
           ? '回放（获授权历史图像，非本次实拍）' : '实拍（camera）');
      add2('图像摘要', (r.n_bytes != null ? r.n_bytes + ' 字节 · ' : '')
           + 'sha256 ' + String(r.sha256 || '').slice(0, 16));
      wrap.appendChild(kv2);

      // 第8周：视觉识别 —— "看到了什么 + 还有什么不能确定"的可理解反馈。
      // 标签/分数/拒识/模型版本/来源结构化展示；纠正按钮是纠错入口，
      // 重拍按钮复用 image 指令通道补拍一张（新 request_id，旧图绝不冒充）。
      const h3 = el('h3', null, '视觉识别（第8周：标签+分数，未校准）');
      wrap.appendChild(h3);
      const box2 = el('div', 'snap-analyze');
      const btnRun = el('button', 'btn', '识别画面（可用性）');
      btnRun.id = 'snap_analyze_btn';
      btnRun.onclick = () => analyzeSnap(r.image_id, 'usability');
      box2.appendChild(btnRun);
      const inferBox = el('div', 'snap-infer');
      inferBox.id = 'snap_infer_box';
      inferBox.textContent = '尚未分析。点击"识别画面"：未配置真实模型时按回放处理并标注。';
      box2.appendChild(inferBox);
      const corrRow = el('div', 'snap-corr');
      for (const lab of ['usable', 'occluded', 'blurred', 'unknown']) {
        const b = el('button', 'mini', '纠正为' + labText(lab));
        b.onclick = () => correctSnap(r.image_id, lab);
        corrRow.appendChild(b);
      }
      const recap = el('button', 'mini', '重拍一张');
      recap.id = 'snap_recapture_btn';
      recap.onclick = () => recaptureSnap(r.image_id);
      corrRow.appendChild(recap);
      box2.appendChild(corrRow);
      wrap.appendChild(box2);

      // 第9周：主动询问 —— 画面不可用时问"要重拍吗"，确认/忽略 + 冷却。
      // 横幅在渲染后轮询一次 GET /asks（可在任意时机打开过的询问里出现）。
      const askBox = el('div', 'snap-ask');
      askBox.id = 'snap_ask_box';
      askBox.style.display = 'none';
      askBox.appendChild(el('span', null, '画面可能不可用，要重拍一张吗？'));
      const askYes = el('button', 'mini', '重拍（确认）');
      askYes.id = 'snap_ask_yes';
      askYes.onclick = () => decideAsk(r.image_id, 'confirm');
      const askNo = el('button', 'mini', '不用（忽略）');
      askNo.id = 'snap_ask_no';
      askNo.onclick = () => decideAsk(r.image_id, 'ignore');
      askBox.appendChild(askYes);
      askBox.appendChild(askNo);
      wrap.appendChild(askBox);
      checkAsk(r.image_id);          // 异步：该画面有未表态询问就亮横幅
    }

    if (samples) {
      wrap.appendChild(el('h3', null, '这条指令采到的样本（' + samples.count + ' 个）'));
      const tw = el('div', 'tablewrap');
      const t = el('table');
      t.innerHTML = '<thead><tr><th class="l">seq</th><th>设备时间</th><th>ax</th>' +
                    '<th>ay</th><th>az</th><th>|a|</th><th>SPL</th>' +
                    '<th class="l">可信度</th></tr></thead>';
      const tb2 = el('tbody');
      for (const r of (samples.readings || []).slice(0, 200)) {
        const mag = (r.ax === null || r.ay === null || r.az === null)
          ? null : Math.hypot(r.ax, r.ay, r.az);
        const tr = el('tr');
        for (const v of [r.seq, fmtTime(r.t_device_ms),
                         r.ax === null ? 'null' : r.ax.toFixed(3),
                         r.ay === null ? 'null' : r.ay.toFixed(3),
                         r.az === null ? 'null' : r.az.toFixed(3),
                         mag === null ? 'null' : mag.toFixed(3),
                         r.spl_db === null ? 'null' : r.spl_db.toFixed(1)]) {
          tr.appendChild(el('td', '', String(v)));
        }
        const trust = D.trustOf ? D.trustOf(r.ntp_synced, r.ntp_sync_age_s) : '';
        const td = el('td', 'l');
        if (trust) td.appendChild(el('span', 'badge b-' + trust, D.TRUST_TEXT[trust] || trust));
        tr.appendChild(td);
        tb2.appendChild(tr);
      }
      t.appendChild(tb2);
      tw.appendChild(t);
      wrap.appendChild(tw);
    }

    wrap.appendChild(el('h3', null, '事件时间线（每一次状态迁移都留痕）'));
    const ul = el('ul', 'tl');
    for (const e of (c.events || [])) {
      const li = el('li');
      const same = e.from_state === e.to_state;
      li.innerHTML = '<b>' + fmtTime(e.t_server_ms) + '</b>　' +
        (same ? '注记（状态未变）' : (e.from_state + ' → ' + e.to_state)) +
        '　<span>' + (e.actor || '') + '</span>';
      if (e.detail) li.appendChild(el('div', null, JSON.stringify(e.detail)));
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
    box.appendChild(wrap);
  }

  // ------------------------------------------------------------ 轮询
  async function refresh(force) {
    if (inflight && !force) return;
    inflight = true;
    try {
      const r = await fetch(api('/api/v1/commands?limit=30'), { cache: 'no-store' });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      cmds = j.commands || [];
      stats = j.stats || null;
      renderStats();
      renderTable();
      // 选中的那条只要还在飞，就跟着一起刷新：进度条不动等于界面在撒谎。
      if (selected) {
        const c = cmds.find(x => x.request_id === selected);
        if (c && !c.is_terminal) {
          const r2 = await fetch(api('/api/v1/commands/' + selected), { cache: 'no-store' });
          if (r2.ok) { detail = (await r2.json()).command; renderDetail(); }
        } else if (c && detail && detail.state !== c.state) {
          const r2 = await fetch(api('/api/v1/commands/' + selected), { cache: 'no-store' });
          if (r2.ok) {
            detail = (await r2.json()).command;
            if (detail.n_samples > 0 && !samples) await loadSamples(selected);
            renderDetail();
          }
        }
      }
      lastError = null;
    } catch (e) {
      lastError = String(e && e.message || e);
      note('指令面板刷新失败：' + lastError, true);
    } finally {
      inflight = false;
    }
  }

  async function init() {
    $('cmd_refresh').onclick = () => refresh(true);
    try {
      const r = await fetch(api('/api/v1/commands/ops'), { cache: 'no-store' });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      ops = j.ops; maxLive = j.max_live_per_device; maxCaptureMs = j.max_capture_duration_ms;
    } catch (e) {
      note('读不到指令清单：' + (e.message || e) + '（服务端是否在跑？）', true);
      ops = [];
    }
    renderOps();

    // MAC 默认填最近一批数据的设备：绝大多数情况下就一台板子，
    // 让用户手抄一串十六进制是纯粹的错误来源（抄错一个字符 => 指令 expired）。
    try {
      const sr = await fetch(api('/api/v1/status'), { cache: 'no-store' });
      if (sr.ok) {
        const st = await sr.json();
        if (st.last_batch && st.last_batch.device_mac && !$('cmd_mac').value) {
          $('cmd_mac').value = st.last_batch.device_mac;
        }
      }
    } catch (e) { /* 填不上就让用户自己填，不值得报错 */ }

    await refresh(true);
    setInterval(() => refresh(false), POLL_MS);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();