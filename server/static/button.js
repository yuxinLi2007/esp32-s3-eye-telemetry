// 第3周：按键事件面板 —— 板上按键 -> 这里实时出现 -> 点"回应/取消" ->
// 指令通道下发 notify -> 设备播放物理反馈，指令状态回显在同一行。
//
// 口径延续前两个面板：
//   - 状态不自己记：事件状态、notify 指令状态全部来自轮询，
//     另一个标签页回应了，这里 2 秒内就能看到。
//   - 失败必须可见：设备没来领（expired）、领了没结果（timeout）、
//     固件太旧（failed/unsupported_op）都在行上有自己的样子，
//     并且给出"重发"按钮——重发就是一次新的 respond，走同一条路。
//   - 幂等：同一次点击的所有网络重试共用一个 client_token，
//     双击/断网重发不会让设备闪两遍灯。
(function () {
  'use strict';
  const D = window.DASH;
  if (!D) { console.error('button.js 需要 index.html 暴露 window.DASH'); return; }
  const { $, api, el, fmtTime, fmtAge } = D;

  const POLL_MS = 2000;          // 与指令面板同节奏："实时显示"的口径全站一致
  const STATE_CLS = { received:'b-pending', acked:'b-done', cancelled:'b-cancelled' };
  const STATE_TEXT = { received:'待回应', acked:'已回应', cancelled:'已取消' };

  let events = [];
  let stats = null;
  let inflight = false;
  let busyId = null;             // 正在提交回应的事件 id（按钮防抖第一层）
  let intent = null;             // { key, token } 同一次点击的重试复用同一个 token

  function randToken() {
    return 'web-' + Date.now().toString(36) + '-' +
           Math.random().toString(36).slice(2, 10);
  }

  function note(msg, bad) {
    const n = $('btn_note');
    n.textContent = msg || '';
    n.style.color = bad ? 'var(--crit)' : 'var(--dim)';
  }

  async function ctlFetch(path, opts) {
    // 与 commands.js 同一套 401 流程、同一个存储键：令牌只需要输入一次。
    const doIt = () => fetch(api(path), Object.assign({}, opts, {
      headers: Object.assign({ 'Content-Type': 'application/json' },
                             D.ctlToken ? { 'X-Control-Token': D.ctlToken } : {},
                             (opts && opts.headers) || {}),
    }));
    let r = await doIt();
    if (r.status === 401) {
      const t = window.prompt('服务端设置了 CONTROL_TOKEN，回应按键事件需要它：');
      if (!t) throw new Error('未提供 CONTROL_TOKEN');
      D.setCtlToken(t);
      r = await doIt();
    }
    return r;
  }

  async function readError(r) {
    let j = null;
    try { j = await r.json(); } catch (e) { /* 用状态码说话 */ }
    if (j && j.error) return j.error.code + '：' + j.error.message;
    if (j && j.detail) return typeof j.detail === 'string' ? j.detail
                         : (j.detail.code ? j.detail.code + '：' + j.detail.message
                                          : JSON.stringify(j.detail));
    return 'HTTP ' + r.status;
  }

  // ------------------------------------------------------------ 回应
  async function respond(ev, decision) {
    if (busyId !== null) return;              // 防抖第一层：一次只允许一个回应在飞
    const verb = decision === 'ack' ? '回应' : '取消';
    if (!window.confirm('对按键事件 #' + ev.id + ' 作出「' + verb + '」？\n' +
                        '设备将在下一次轮询（最坏 ' + (POLL_MS / 1000) +
                        ' 秒）后播放对应的 LED 反馈。')) return;
    // 幂等键：一次点击一个 token。key 变了（用户点了另一次）才换新 token。
    const key = ev.id + '|' + decision + '|' + (ev.respond_count || 0);
    if (!intent || intent.key !== key) intent = { key, token: randToken() };

    busyId = ev.id;
    render();
    note('正在下发 ' + verb + ' …');
    try {
      const r = await ctlFetch('/api/v1/button/events/' + ev.id + '/respond', {
        method: 'POST',
        body: JSON.stringify({ decision, client_token: intent.token }),
      });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      if (j.deduped) {
        note('重复提交已被服务端去重，仍是同一条指令 ' + j.command.request_id, false);
      } else {
        note('已' + verb + '：指令 ' + j.command.request_id + '　' +
             j.command.state_text + '　设备最坏 ' + (POLL_MS / 1000) + ' s 后领取', false);
        intent = null;
      }
      await refresh(true);
    } catch (e) {
      note(verb + '失败：' + (e.message || e) + '（未产生指令，可重试；重试用同一个幂等键）', true);
    } finally {
      busyId = null;
      render();
    }
  }

  // ------------------------------------------------------------ 渲染
  function trustBadge(ev) {
    // 与第1周同一套规则：设备时间只有在 NTP 新鲜时才可参考
    const t = D.trustOf ? D.trustOf(ev.ntp_synced, ev.ntp_sync_age_s) : '';
    if (!t) return null;
    return el('span', 'badge b-' + t, D.TRUST_TEXT[t] || t);
  }

  function cmdCell(ev) {
    // 事件行不存指令状态的副本：command 是轮询时服务端现查回来的
    const c = ev.command;
    if (!ev.request_id) return el('span', 'note', '—');
    const box = el('span');
    box.appendChild(el('code', null, ev.request_id));
    if (c) {
      box.appendChild(document.createTextNode(' '));
      box.appendChild(el('span', 'badge b-' + c.state, c.state_text));
      if (c.error_code) {
        box.appendChild(el('span', 'note', ' [' + c.error_code + ']'));
      }
    } else {
      box.appendChild(el('span', 'note', '（指令查询中…）'));
    }
    return box;
  }

  function needsResend(ev) {
    // 已回应但设备侧没成（expired/timeout/failed/cancelled）：给出重发入口。
    // pending/claimed/running/done 都不该重发——在飞的指令重发只会排队更长。
    const c = ev.command;
    return ev.request_id && c && c.is_terminal && c.state !== 'done';
  }

  function render() {
    // 统计
    const s = stats;
    $('btn_stats').textContent = s
      ? ('共 ' + s.total + ' 次按键　待回应 ' + s.received +
         '　已回应 ' + s.acked + '　已取消 ' + s.cancelled)
      : '';
    $('btn_stats').style.color = (s && s.received > 0) ? 'var(--warn)' : 'var(--dim)';

    const tb = $('btn_tbody');
    tb.innerHTML = '';
    if (!events.length) {
      const tr = el('tr');
      const td = el('td', 'l', '还没有按键事件。按下板上 BOOT 键（GPIO0），' +
                              'LED 会立刻单闪——那是本地反馈，不等网络；' +
                              '事件本身最坏 2 秒后出现在这里。');
      td.colSpan = 7;
      tr.appendChild(td);
      tb.appendChild(tr);
      return;
    }
    for (const ev of events) {
      const tr = el('tr');
      // 服务端接收时刻（权威时间）+ 板端按下时刻（参考，带可信度标）
      const tdT = el('td', 'l', fmtTime(ev.t_server_ms) + '　');
      tdT.appendChild(el('span', 'note', fmtAge(ev.age_ms) + '前'));
      tr.appendChild(tdT);
      tr.appendChild(el('td', 'l', '#' + ev.id + '　seq ' + ev.press_seq));
      tr.appendChild(el('td', 'l', ev.device_mac + ' @ ' + ev.boot_id));
      const tdDev = el('td', 'l', ev.t_device_ntp_ms === null ? '—'
                       : fmtTime(ev.t_device_ntp_ms));
      const badge = trustBadge(ev);
      if (badge) { tdDev.appendChild(document.createTextNode(' ')); tdDev.appendChild(badge); }
      tr.appendChild(tdDev);
      const tdS = el('td', 'l');
      tdS.appendChild(el('span', 'badge ' + (STATE_CLS[ev.state] || ''),
                         STATE_TEXT[ev.state] || ev.state));
      if (ev.queue_dropped > 0) {
        // 板端丢弃过的按键数：失败必须可见，哪怕后来网络恢复了
        tdS.appendChild(el('span', 'note', ' 板上曾丢弃 ' + ev.queue_dropped + ' 次'));
      }
      tr.appendChild(tdS);
      tr.appendChild(cmdCell(ev));

      const tdOp = el('td', 'l');
      const busy = busyId === ev.id;
      if (ev.state === 'received' || needsResend(ev)) {
        const label = ev.state === 'received' ? '' : '重发：';
        if (label) tdOp.appendChild(el('span', 'note', label));
        const bAck = el('button', 'btn go', '回应');
        const bCan = el('button', 'btn danger', '取消');
        bAck.disabled = bCan.disabled = busy;
        bAck.title = '下发 notify(decision=ack)：设备播放两下慢闪';
        bCan.title = '下发 notify(decision=cancel)：设备播放六下快闪';
        bAck.onclick = () => respond(ev, 'ack');
        bCan.onclick = () => respond(ev, 'cancel');
        tdOp.appendChild(bAck);
        tdOp.appendChild(document.createTextNode(' '));
        tdOp.appendChild(bCan);
      } else if (ev.command && ev.command.state === 'done') {
        tdOp.appendChild(el('span', 'note', '设备已播放反馈'));
      } else {
        tdOp.appendChild(el('span', 'note', ev.request_id ? '指令在飞，等结果…' : '—'));
      }
      tr.appendChild(tdOp);
      tb.appendChild(tr);
    }
  }

  // ------------------------------------------------------------ 轮询
  async function refresh(force) {
    if (inflight && !force) return;
    inflight = true;
    try {
      const r = await fetch(api('/api/v1/button/events?limit=30'), { cache: 'no-store' });
      if (!r.ok) throw new Error(await readError(r));
      const j = await r.json();
      events = j.events || [];
      stats = j.stats || null;
      render();
    } catch (e) {
      note('按键面板刷新失败：' + (e.message || e), true);
    } finally {
      inflight = false;
    }
  }

  function init() {
    $('btn_refresh').onclick = () => refresh(true);
    refresh(true);
    setInterval(() => refresh(false), POLL_MS);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
