/* 第10周：多源独立状态页（sources.js）
 *
 * 独立小模块：只读轮询 /api/v1/devices + /api/v1/devices/{mac}/sources，
 * 渲染"未尝试/失败/成功/陈旧"四态 chip 与数据年龄（按观测时刻计算）。
 * - 全部新 DOM id（src_*），不动既有元素；自带 5s 轮询，不改既有轮询间隔；
 * - 不依赖 window.DASH（与 commands.js 相互独立），ui_check 的 DOM 桩不加载本文件。
 */
(() => {
  const PANEL = document.getElementById('src_panel');
  if (!PANEL) return;

  const SRC_LABEL = { gnss: 'GNSS 定位', imu: '惯性', net: '网络' };
  const STATUS_LABEL = {
    not_attempted: '未尝试', failed: '失败', success: '正常', stale: '陈旧',
  };

  function ageText(ms) {
    if (ms == null) return '—';
    const sec = Math.max(0, Math.floor(ms / 1000));
    if (sec < 60) return sec + ' 秒前';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
    if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
    return Math.floor(sec / 86400) + ' 天前';
  }

  async function refresh() {
    try {
      const r = await fetch('/api/v1/devices', { cache: 'no-store' });
      if (!r.ok) throw new Error('devices ' + r.status);
      const j = await r.json();
      const devs = j.devices || [];
      PANEL.innerHTML = '';
      if (!devs.length) {
        PANEL.textContent = '暂无设备上报（有批次或来源事件后这里显示各源状态）。';
        return;
      }
      for (const d of devs.slice(0, 5)) {
        const card = document.createElement('div');
        card.className = 'src-card';
        card.id = 'src_card_' + d.device_mac.replace(/[^A-Z0-9]/g, '');
        const macLine = document.createElement('div');
        macLine.className = 'src-mac';
        macLine.textContent = d.device_mac;
        card.appendChild(macLine);
        PANEL.appendChild(card);

        const rr = await fetch('/api/v1/devices/' + encodeURIComponent(d.device_mac) +
                               '/sources', { cache: 'no-store' });
        if (!rr.ok) continue;
        const sj = await rr.json();
        for (const [src, s] of Object.entries(sj.source_states || {})) {
          const chip = document.createElement('span');
          chip.className = 'src-chip ' + s.status;
          chip.id = 'src_' + src + '_' + d.device_mac.replace(/[^A-Z0-9]/g, '');
          chip.textContent = (SRC_LABEL[src] || src) + '：' +
            (STATUS_LABEL[s.status] || s.status) + '（' + ageText(s.age_ms) + '）';
          chip.title = s.detail || '';
          card.appendChild(chip);
        }
        const note = document.createElement('div');
        note.className = 'src-answer';
        note.id = 'src_answer_' + d.device_mac.replace(/[^A-Z0-9]/g, '');
        note.textContent = sj.answer || '';
        card.appendChild(note);

        // 第11周：三层上下文（原始观测/派生状态/交互决策）—— 已知事实＋待确认信息
        const cr = await fetch('/api/v1/context?device_mac=' +
          encodeURIComponent(d.device_mac), { cache: 'no-store' });
        if (cr.ok) {
          const cj = await cr.json();
          const ctxBlock = document.createElement('div');
          ctxBlock.className = 'ctx-block';
          ctxBlock.id = 'ctx_block_' + d.device_mac.replace(/[^A-Z0-9]/g, '');
          const ctxTitle = document.createElement('div');
          ctxTitle.className = 'src-mac';
          ctxTitle.textContent = '上下文解释（第11周）· 规则版本 ' + (cj.rule_version || '—');
          ctxBlock.appendChild(ctxTitle);
          const ctxAnswer = document.createElement('div');
          ctxAnswer.className = 'src-answer';
          ctxAnswer.id = 'ctx_answer_' + d.device_mac.replace(/[^A-Z0-9]/g, '');
          ctxAnswer.textContent = cj.answer || '';
          ctxBlock.appendChild(ctxAnswer);
          card.appendChild(ctxBlock);
        }
      }
    } catch (e) {
      PANEL.textContent = '多源状态加载失败：' + (e && e.message || e);
    }
  }

  const btn = document.getElementById('src_refresh');
  if (btn) btn.addEventListener('click', refresh);
  refresh();
  setInterval(refresh, 5000);   // 第10周新增面板自己的轮询，不动既有间隔
})();