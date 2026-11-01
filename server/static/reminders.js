/* 第12周：提醒偏好与纠错闭环（reminders.js）
 *
 * 独立小模块：只读轮询 /api/v1/reminders/prefs|report|feedback|events，
 * 渲染"本轮提醒偏好（on/off/snooze + 配置版本）"、"前后对照"与反馈/触发日志。
 * - 全部新 DOM id（remind_*），不动既有元素；自带 5s 轮询，不改既有轮询间隔；
 * - 读操作匿名；"关闭/稍后/恢复默认"是写状态，走 CONTROL_TOKEN（401 时提示输入，
 *   与 index.html 采集开关同一套口令）；DASH 不存在时退化为只读面板；
 * - ui_check 的 DOM 桩不加载本文件（只加载内联脚本与 commands.js），无回归。
 */
(() => {
  const PANEL = document.getElementById('remind_panel');
  if (!PANEL) return;

  const DASH = window.DASH || null;
  const ctlHeaders = () =>
    (DASH ? DASH.ctlHeaders() : { 'Content-Type': 'application/json' });

  const VALUE_LABEL = {
    on: '按默认规则提醒', off: '已关闭（本轮不提醒）', snooze: '稍后提醒',
  };
  const VERB_LABEL = {
    confirm: '确认', correct: '纠正', ignore: '忽略', later: '稍后',
    off: '关闭', restore_default: '恢复默认',
  };

  async function postPref(mac, action) {
    if (!DASH) return;
    const body = JSON.stringify({ action,
      ...(mac ? { device_mac: mac } : {}) });
    let r = await fetch('/api/v1/reminders/prefs', {
      method: 'POST', headers: ctlHeaders(), body,
    });
    if (r.status === 401 && DASH) {
      const t = window.prompt('服务端设置了 CONTROL_TOKEN，修改提醒偏好需要它：');
      if (!t) throw new Error('未提供 CONTROL_TOKEN');
      DASH.setCtlToken(t);
      r = await fetch('/api/v1/reminders/prefs', {
        method: 'POST', headers: ctlHeaders(), body,
      });
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    refresh();
  }

  function chip(cls, text) {
    const s = document.createElement('span');
    s.className = 'src-chip ' + cls;
    s.textContent = text;
    return s;
  }

  function actBtn(label, mac, action) {
    const b = document.createElement('button');
    b.className = 'btn mini';
    b.textContent = label;
    b.addEventListener('click', () => {
      b.disabled = true;
      postPref(mac, action)
        .catch(e => { b.disabled = false; PANEL.textContent = '操作失败：' + (e && e.message || e); });
    });
    return b;
  }

  async function refresh() {
    try {
      const [dr, pr, rr, fr, er] = await Promise.all([
        fetch('/api/v1/devices', { cache: 'no-store' }),
        fetch('/api/v1/reminders/prefs', { cache: 'no-store' }),
        fetch('/api/v1/reminders/report', { cache: 'no-store' }),
        fetch('/api/v1/reminders/feedback?limit=8', { cache: 'no-store' }),
        fetch('/api/v1/reminders/events?limit=6', { cache: 'no-store' }),
      ]);
      if (!dr.ok || !pr.ok || !rr.ok) throw new Error('reminders ' + dr.status);
      const devs = (await dr.json()).devices || [];
      const prefs = await pr.json();
      const report = (await rr.json()).report;
      const fb = (await fr.json()).feedback || [];
      const ev = (await er.json()).events || [];

      PANEL.innerHTML = '';

      // ---- 偏好卡片：每个已知设备一行，外加"全局默认"一行（用户可见、可撤回） ----
      const scopes = [null].concat(devs.slice(0, 3).map(d => d.device_mac));
      for (const mac of scopes) {
        const card = document.createElement('div');
        card.className = 'src-card';
        card.id = 'remind_card_' + ((mac || 'GLOBAL').replace(/[^A-Z0-9]/g, ''));
        const macLine = document.createElement('div');
        macLine.className = 'src-mac';
        macLine.textContent = mac ? (mac + ' 的提醒偏好') : '全局默认提醒偏好';
        card.appendChild(macLine);

        let p = null;
        try {
          const r2 = await fetch('/api/v1/reminders/prefs' +
            (mac ? '?device_mac=' + encodeURIComponent(mac) : ''), { cache: 'no-store' });
          if (r2.ok) p = (await r2.json()).channels.vision_ask;
        } catch (e) { /* 单卡失败不影响其它卡 */ }
        const v = (p && p.value) || 'on';
        const state = VALUE_LABEL[v] || v;
        const chipEl = chip(v, '提醒：' + state +
          (p && p.config_version != null ? ' · 配置版本 v' + p.config_version : ''));
        chipEl.title = p && p.note ? p.note : '';
        card.appendChild(chipEl);
        card.appendChild(actBtn('关闭提醒', mac || null, 'off'));
        card.appendChild(actBtn('稍后提醒', mac || null, 'later'));
        card.appendChild(actBtn('恢复默认', mac || null, 'restore'));
        PANEL.appendChild(card);
      }

      // ---- 前后对照（实践2：修改前/后重复提醒次数与任务结果） ----
      const cmp = document.createElement('div');
      cmp.className = 'ctx-block';
      cmp.id = 'remind_report_block';
      const cmpTitle = document.createElement('div');
      cmpTitle.className = 'src-mac';
      cmpTitle.textContent = '提醒前后对照（规则版本 ' +
        (report.rule_version || '—') + ' · 配置版本 v' + (report.config_version || 0) + '）';
      cmp.appendChild(cmpTitle);
      const line = document.createElement('div');
      line.className = 'src-answer';
      line.id = 'remind_report_answer';
      line.textContent =
        ('偏好变更前提醒 ' + (report.fired_before_pref_change || 0) + ' 次 / ' +
         '变更后 ' + (report.fired_after_pref_change || 0) + ' 次；' +
         '同一事件重放未再触发 ' + (report.replays_not_refired || 0) + ' 次；' +
         '询问终局 open ' + ((report.asks && report.asks.open) || 0) +
         ' / confirmed ' + ((report.asks && report.asks.confirmed) || 0) +
         ' / ignored ' + ((report.asks && report.asks.ignored) || 0) + '。');
      cmp.appendChild(line);
      const fbLine = document.createElement('div');
      fbLine.className = 'src-answer';
      fbLine.id = 'remind_feedback_line';
      const fbText = Object.entries(report.feedback || {})
        .map(([k, n]) => (VERB_LABEL[k] || k) + ' ' + n + ' 次').join('、');
      fbLine.textContent = '用户反馈：' + (fbText || '暂无');
      cmp.appendChild(fbLine);
      PANEL.appendChild(cmp);

      // ---- 最近反馈与触发日志（供当堂验证：重放不重复触发） ----
      const log = document.createElement('div');
      log.className = 'ctx-block';
      log.id = 'remind_log_block';
      const logTitle = document.createElement('div');
      logTitle.className = 'src-mac';
      logTitle.textContent = '最近反馈与触发/去重日志';
      log.appendChild(logTitle);
      if (fb.length) {
        for (const r of fb.slice(0, 5)) {
          const item = document.createElement('div');
          item.className = 'src-answer';
          item.textContent = (VERB_LABEL[r.verb] || r.verb) + ' · 设备 ' +
            (r.device_mac || '—') + ' · 规则 ' + (r.rule_version || '—') +
            ' · 配置 v' + (r.config_version == null ? '—' : r.config_version);
          item.id = 'remind_fb_' + r.id;
          log.appendChild(item);
        }
      } else {
        log.appendChild(document.createElement('div'))
          .className = 'src-answer';
        log.lastChild.textContent = '还没有任何反馈记录。';
      }
      if (ev.length) {
        for (const e of ev.slice(0, 5)) {
          const item = document.createElement('div');
          item.className = 'src-answer';
          item.textContent = (e.event_key || '') + ' · fire_count=' + e.fire_count +
            ' · 重放 ' + ((e.last_seen_ms || 0) > (e.last_fired_ms || 0) && e.fire_count > 0
              ? '已见（未重复触发）' : '—');
          item.id = 'remind_ev_' + e.id;
          log.appendChild(item);
        }
      }
      PANEL.appendChild(log);
    } catch (e) {
      PANEL.textContent = '提醒偏好加载失败：' + (e && e.message || e);
    }
  }

  const btn = document.getElementById('remind_refresh');
  if (btn) btn.addEventListener('click', refresh);
  refresh();
  setInterval(refresh, 5000);   // 第12周新增面板自己的轮询，不动既有间隔
})();