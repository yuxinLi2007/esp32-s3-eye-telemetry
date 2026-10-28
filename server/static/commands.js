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
      // 第3周：字符串参数（白名单 enum，服务端 ops 清单里带 choices）。
      // 取值照旧不另抄一份——下拉框的选项就是从 choices 渲染出来的。
      if (p.kind === 'str') {
        const v = input.value;
        if (!(p.choices || []).includes(v)) {
          note(p.name + ' 只能是 ' + (p.choices || []).join('/') + '，当前是 "' + v + '"', true);
          return null;
        }
        out[p.name] = v;
        continue;
      }
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
        // 这条限制不是服务端拍脑袋定的：采集期间板子不上传连续流，全靠 12 秒环形缓冲扛。
        note('n × interval_ms = ' + dur + ' ms，超过上限 ' + maxCaptureMs +
             ' ms（再长连续流缓冲就会溢出丢样）', true);
        return null;
      }
      $('cmd_dur').textContent = '预计 ' + (dur / 1000).toFixed(1) + ' s';
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
                     'mic_verdict', 'collect_enabled', 'decision', 'event_id',
                     'led_pin_note']) {
      if (r[k] !== undefined && r[k] !== null) pick.push(k + '=' + r[k]);
    }
    return pick.length ? pick.join('　') : JSON.stringify(r).slice(0, 120);
  }

  function renderOps() {
    const box = $('cmd_ops');
    box.innerHTML = '';
    if (!ops) { box.appendChild(el('span', 'note', '正在读取指令清单 …')); return; }
    for (const spec of ops) {
      if (spec.params && spec.params.length) {
        for (const p of spec.params) {
          const lab = el('label', null, p.name);
          let inp;
          if (p.kind === 'str') {
            // 第3周：白名单字符串渲染成下拉框（notify 的 decision=ack/cancel）
            inp = el('select');
            inp.id = 'cmd_p_' + p.name;
            for (const ch of (p.choices || [])) {
              const o = el('option', null, ch);
              o.value = ch;
              inp.appendChild(o);
            }
            inp.value = p.default;
          } else {
            inp = el('input');
            inp.type = 'number';
            inp.id = 'cmd_p_' + p.name;
            inp.value = p.default;
            inp.min = p.lo; inp.max = p.hi;
          }
          inp.disabled = !!busyOp;
          box.appendChild(lab); box.appendChild(inp);
        }
        if (spec.op === 'capture') {
          box.appendChild(el('span', 'note', 'n≤' + spec.params[0].hi +
            '，interval ' + spec.params[1].lo + '~' + spec.params[1].hi + ' ms，n×interval≤' +
            maxCaptureMs + ' ms'));
        } else if (spec.op === 'notify') {
          box.appendChild(el('span', 'note',
            'decision：设备播放的反馈图案（ack=两下慢闪 / cancel=六下快闪）；' +
            'event_id=0 表示手动测试，正常回应请走上面的按键事件面板'));
        }
        box.appendChild(el('span', 'note', ''));
      }
      const b = el('button', 'btn op', spec.op);
      b.title = spec.desc;
      b.disabled = !!busyOp;
      b.textContent = busyOp === spec.op ? spec.op + ' …' : spec.op;
      b.onclick = () => send(spec.op);
      box.appendChild(b);
    }
    const dur = el('span', 'note', '');
    dur.id = 'cmd_dur';
    box.appendChild(dur);
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
        tdN.title = '设备自报与服务端实收不一致：有样本在上传途中丢了';
        tdN.textContent += ' ⚠';
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
    add('超时口径', 'ttl ' + fmtAge(c.ttl_ms) + ' 内未被领取 → expired；领取后静默 ' +
        fmtAge(c.timeout_ms) + ' → 重排队或 timeout（锚点是最后一次有动静的时刻，进度心跳可续命）');
    add('样本数', '服务端实收 ' + (c.n_samples === null ? '—' : c.n_samples) +
        '　设备自报 ' + (c.n_samples_device === null ? '—' : c.n_samples_device) +
        (c.sample_count_mismatch ? '　⚠ 不一致，有样本在上传途中丢了' : ''));
    add('备注', c.note);
    if (c.error_code) add('错误码', c.error_code + (c.error_message ? '　' + c.error_message : ''));
    wrap.appendChild(kv);

    if (c.result) {
      wrap.appendChild(el('h3', null, '结果体'));
      const pre = el('pre', 'json', JSON.stringify(c.result, null, 2));
      wrap.appendChild(pre);
    }

    if (samples) {
      wrap.appendChild(el('h3', null, '这条指令采到的样本（' + samples.count + ' 个）'));
      const n2 = el('div', 'note',
        '这些样本不会出现在上方的连续流图表与样本表里（服务端默认按 request_id 把它们分流，' +
        '当前窗口共排除 ' + samples.excluded_command_samples + ' 个指令样本）。' +
        '两条流各自独立计 seq，混在一起会算出假缺口。');
      wrap.appendChild(n2);
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
          $('cmd_mac_hint').textContent = '（已按最近一批数据自动填入）';
        }
      }
    } catch (e) { /* 填不上就让用户自己填，不值得报错 */ }

    await refresh(true);
    setInterval(() => refresh(false), POLL_MS);
  }

  // 第4周：自然语言助手下发 capture 之后，希望指令面板立刻刷新一次。
  // 把 refresh 挂到共享 DASH 上，而不是让 assistant.js 去猜这里的状态——
  // 面板状态仍然只有这一份实现，跨脚本只共享入口。
  D.refreshCommands = () => refresh(true);

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();