#!/usr/bin/env node
/**
 * 打开"本周（含重做）"的 PR，并回写 state/week-progress.json 的 inProgress.pr。
 *
 * 幂等与去重（护栏：不重复开同一周的 PR）：
 *   - 目标分支上已有一个开放 PR → 打印它并退出，绝不创建第二条；
 *   - 已有一周同标题（week-N: …）的开放 PR 挂在别的分支上 → 报错退出。
 *
 * PR 标题：week-N: <摘要>（--summary 优先；否则取 prompts/week-N.md 标题里的主题；
 *           再否则取分支首个提交的摘要）。
 * PR 正文：本周改动（diff stat + 提交列表）、UI 精简点、ui_check 结果、
 *          redo 时给出"替换原第 N 周实现"的说明。
 *
 * 用法：
 *   node scripts/open-pr.mjs [--week N] [--branch B] [--redo]
 *                            [--summary S] [--ui-check-log FILE]
 *                            [--replace-note TEXT] [--dry-run]
 *
 * 环境：需要 git 与 gh CLI。默认在本分支（week-N / week-N-redo）上操作。
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATE_PATH = 'state/week-progress.json';

function run(cmd, args, opts = {}) {
  try {
    return execFileSync(cmd, args, {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'], ...opts,
    }).toString().trim();
  } catch (e) {
    const msg = (e.stderr || '').toString().trim();
    throw new Error(`命令失败 [${cmd} ${args.join(' ')}]: ${msg || e.message || e.status}`);
  }
}
const git = (args, opts) => run('git', args, opts);
const gh = (args, opts) => run('gh', args, opts);

function repoSlug() {
  if (process.env.GITHUB_REPOSITORY) return process.env.GITHUB_REPOSITORY;
  const url = git(['remote', 'get-url', 'origin']);
  const m = /github\.com[:/]([^/\s]+)\/([^/\s]+?)(\.git)?\s*$/.exec(url);
  if (!m) throw new Error('无法从 git remote 识别 GitHub 仓库: ' + url);
  return `${m[1]}/${m[2]}`;
}
function prForBranch(branch) {
  const out = gh(['pr', 'list', '--repo', repoSlug(), '--head', branch,
    '--state', 'all', '--json', 'number,state,title,url', '--jq', '.[0]']);
  if (!out || out === 'null') return null;
  return JSON.parse(out);
}
function readFileSafe(f) { try { return fs.readFileSync(f, 'utf8'); } catch { return null; } }
function argvOf(names) {
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    if (names.includes(argv[i]) && argv[i + 1]) return argv[i + 1];
  }
  return null;
}
function flagOn(names) { return process.argv.slice(2).some(a => names.includes(a)); }

// ---------------------------------------------------------------- 参数
const args = {
  week: argvOf(['--week']) ? parseInt(argvOf(['--week']), 10) : null,
  branch: argvOf(['--branch']),
  summary: argvOf(['--summary']),
  uiLog: argvOf(['--ui-check-log']),
  replaceNote: argvOf(['--replace-note']),
  redo: flagOn(['--redo']),
  dryRun: flagOn(['--dry-run']),
};

// 读工作树/远端的最新状态（与 next-week.mjs 同一口径，优先本周分支）
function readActiveState() {
  git(['fetch', 'origin', 'main']);
  const local = path.join(ROOT, STATE_PATH);
  let state = null;
  try { state = JSON.parse(readFileSafe(local)); } catch { state = null; }
  if (!state || !(state.inProgress && Number.isInteger(state.inProgress.week))) {
    const raw = (() => {
      try { return execFileSync('git', ['show', `origin/main:${STATE_PATH}`],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).toString(); } catch { return null; }
    })();
    if (raw) { try { state = JSON.parse(raw); } catch {} }
  }
  if (!state) throw new Error(`缺少 ${STATE_PATH}`);
  return state;
}

// ---------------------------------------------------------------- 摘要
function deriveSummary(week, branch) {
  const via = p => String(p).slice(0, 120);
  if (args.summary) return via(args.summary);
  const tpl = readFileSafe(path.join(ROOT, 'prompts', `week-${week}.md`));
  if (tpl) {
    const m = /^#\s*第\s*\d+\s*周[^:：]*[:：]\s*(.+)$/m.exec(tpl);
    if (m) return via(m[1].trim());
  }
  const log = git(['log', '--oneline', '--no-merges', 'origin/main..HEAD']);
  const first = (log || '').split('\n').find(Boolean);
  if (first) {
    const s = first.replace(/^[a-z]+(\([^)]*\))?:\s*/i, '')
                  .replace(/^第\s*\d+\s*周[^:：]*[:：]?\s*/i, '').trim();
    if (s) return via(s);
  }
  return `第 ${week} 周任务`;
}

// ---------------------------------------------------------------- 正文
function buildBody({ week, isRedo, branch, summary, uiLog, replaceNote }) {
  const L = [];
  const stat = git(['diff', '--stat', 'origin/main...HEAD']);
  L.push('## 本周改动');
  L.push('```');
  L.push(stat || '（分支相对 main 无文件差异）');
  L.push('```');
  const commits = (git(['log', '--oneline', '--no-merges', 'origin/main..HEAD']) || '')
    .split('\n').filter(Boolean);
  if (commits.length) {
    L.push('\n提交：');
    for (const c of commits) L.push(`- ${c}`);
  }

  L.push('\n## UI 精简点');
  const uiNote = readFileSafe(path.join(ROOT, '.artifacts', `week-${week}-ui.md`));
  L.push((uiNote && uiNote.trim())
    || '- 单文件页面（index.html 自包含样式与主脚本，无构建、无 CDN；commands.js 因 ui_check 按文件读取而保留拆分）。\n'
       + '- 页面只保留：报警条（仅异常时出现）、实时曲线（缺口为红色竖带）、三项指标卡（最后接收时间 / 采样率 / NTP 可信等级）、采集开关、指令输入区。\n'
       + '- 删除解释文字、派生字段、调试信息、版本号、Git 链接；底色白/极浅灰、单强调色红、无边框/阴影/分隔线、圆角 ≤6px、不用渐变与 emoji、窄屏不横向滚动。');

  L.push('\n## ui_check 结果');
  if (uiLog) {
    const log = (readFileSafe(uiLog) || '').trim().split('\n').slice(-40).join('\n');
    L.push('```');
    L.push(log || '（日志为空）');
    L.push('```');
  } else {
    L.push('（未提供 ui_check 日志文件——自动化流程中开 PR 前必须拿到通过日志）');
  }

  if (isRedo) {
    L.push('\n## 替换说明（重做）');
    L.push(replaceNote || `本 PR 为第 ${week} 周的重做实现（分支 \`${branch}\`，从 main 新建），`
      + `替换此前已推送的第 ${week} 周代码；原分支/PR 不回退 main，不并入本分支。`);
  }

  L.push('\n---\n> 由 scripts/open-pr.mjs 生成：单周粒度、状态驱动流水线的开 PR 环节。');
  return L.join('\n');
}

// ---------------------------------------------------------------- 主流程
function main() {
  const state = readActiveState();

  const curBranch = git(['rev-parse', '--abbrev-ref', 'HEAD']);
  const branch = args.branch || (state.inProgress && state.inProgress.branch) || curBranch;
  const week = args.week || (state.inProgress && state.inProgress.week)
             || (() => { const m = /^week-(\d+)/.exec(branch); return m ? parseInt(m[1], 10) : null; })();
  const isRedo = args.redo ?? (state.inProgress && !!state.inProgress.redo)
             ?? /-redo$/.test(branch);
  if (!week) throw new Error('无法确定周次：传 --week 或确认 state.inProgress');

  // 1) 本分支已有的 PR
  const existing = prForBranch(branch);
  if (existing && existing.state === 'OPEN') {
    console.log(JSON.stringify({
      ok: true, existing: true, pr: existing.number, title: existing.title, url: existing.url,
    }, null, 2));
    return;
  }

  // 2) 同周次去重：别的分支上不能同时有"第 N 周"的开放 PR
  const opens = JSON.parse(gh(['pr', 'list', '--repo', repoSlug(), '--state', 'open',
    '--json', 'number,title,headRefName,headRepositoryOwner', '--jq', '.']));
  const sameWeek = opens.filter(p => p.title.startsWith(`week-${week}:`) || p.title.startsWith(`week-${week}：`));
  if (sameWeek.length) {
    throw new Error(`第 ${week} 周已有开放 PR：#${sameWeek[0].number} ${sameWeek[0].title}`
      + `（分支 ${sameWeek[0].headRefName}）。不重复开同一周的 PR。`);
  }

  if (branch !== curBranch) {
    throw new Error(`当前分支 ${curBranch} 不是目标分支 ${branch}，请先切换（git checkout ${branch}）`);
  }
  const ahead = parseInt(git(['rev-list', '--count', 'origin/main..HEAD']), 10);
  if (!ahead) throw new Error('分支相对 main 没有新提交，不开空 PR');

  const summary = deriveSummary(week, branch);
  const title = `week-${week}: ${summary}`;
  fs.mkdirSync(path.join(ROOT, '.artifacts'), { recursive: true });
  const bodyFile = path.join(ROOT, '.artifacts', `pr-week-${week}.md`);
  fs.writeFileSync(bodyFile, buildBody({ week, isRedo, branch, summary, uiLog: args.uiLog, replaceNote: args.replaceNote }));

  if (args.dryRun) {
    console.log(JSON.stringify({ dryRun: true, title, bodyFile, stateInProgress: { week, redo: isRedo, branch } }, null, 2));
    return;
  }

  const created = JSON.parse(gh(['pr', 'create', '--repo', repoSlug(),
    '--base', 'main', '--head', branch, '--title', title,
    '--body-file', bodyFile, '--json', 'number,title,url,state']));

  // 3) 回写 inProgress.pr
  const next = { ...state, inProgress: { week, redo: isRedo, branch, pr: created.number }, updatedAt: new Date().toISOString() };
  fs.writeFileSync(path.join(ROOT, STATE_PATH), JSON.stringify(next, null, 2) + '\n');
  git(['add', STATE_PATH]);
  git(['commit', '-m', `chore(state): 第 ${week} 周 PR #${created.number} 已开，等合并`]);
  git(['push', 'origin', `${branch}:refs/heads/${branch}`]);

  console.log(JSON.stringify({
    ok: true, existing: false, pr: created.number, title: created.title, url: created.url,
    stateInProgress: next.inProgress,
  }, null, 2));
}

main();