#!/usr/bin/env node
/**
 * 单周粒度、状态驱动的周进度状态机。
 *
 * 对上文 .github/workflows/week-progress.yml 的"决策"步骤：
 *   1. 读 state/week-progress.json，来源优先级：
 *        本周开放 PR 的分支（状态正随该 PR 流动） > origin/main > 工作树。
 *   2. 结清：inProgress 对应 PR 已合并 → 推进 lastCompletedWeek、把该周移出 redo。
 *   3. 决定本周动作：
 *        wait      —— inProgress 已有 PR 且未合并：不新建分支、不推进，等合并。
 *        continue  —— inProgress 存在但尚无 PR：继续本周（分支已存在则复用）。
 *        do        —— 没有进行中的周：取 redo[0]；否则执行 lastCompletedWeek + 1。
 *        done      —— 18 周全部完成。
 *   4. 为 do/continue 准备分支：week-N（redo 用 week-N-redo）。
 *        分支已存在于远端 → 复用（绝不重复开工）；不存在 → 从 origin/main 新建并推送。
 *   5. 新建分支时，把 inProgress 作为独立 commit 提交到该分支并推送——
 *        状态跟着本周 PR 走，PR 合并时并入 main，全程不直接推 main。
 *
 * 用法：
 *   node scripts/next-week.mjs                # 全量：结清 + 决策 + 建分支 + 回写状态
 *   node scripts/next-week.mjs --plan         # 只读：打印决策，不建分支、不改状态
 *   node scripts/next-week.mjs --checkout <branch>   # 切到指定周分支（实现步骤用）
 *
 * 依赖：git 与 gh CLI（GitHub Actions 自带；本地需 gh auth login）。
 * 输出：决策 JSON 打到 stdout；退出码 0 正常，出错 1。
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATE_PATH = 'state/week-progress.json';
const COURSE_MAX_WEEK = 18;

// ---------------------------------------------------------------- 基础工具
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

function refExists(r) {
  try {
    execFileSync('git', ['rev-parse', '--verify', '--quiet', r],
      { stdio: ['ignore', 'ignore', 'ignore'] });
    return true;
  } catch { return false; }
}
function tryShow(ref) {
  try {
    return execFileSync('git', ['show', ref],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).toString();
  } catch { return null; }
}
function readFileSafe(f) {
  try { return fs.readFileSync(f, 'utf8'); } catch { return null; }
}
function iso() { return new Date().toISOString(); }

function repoSlug() {
  if (process.env.GITHUB_REPOSITORY) return process.env.GITHUB_REPOSITORY;
  const url = git(['remote', 'get-url', 'origin']);
  const m = /github\.com[:/]([^/\s]+)\/([^/\s]+?)(\.git)?\s*$/.exec(url);
  if (!m) throw new Error('无法从 git remote 识别 GitHub 仓库: ' + url);
  return `${m[1]}/${m[2]}`;
}
function prView(n) {
  return JSON.parse(gh(['pr', 'view', String(n), '--repo', repoSlug(),
    '--json', 'state,mergedAt,number,title', '--jq', '.']));
}
function prForBranch(branch) {
  const out = gh(['pr', 'list', '--repo', repoSlug(), '--head', branch,
    '--state', 'all', '--json', 'number,state,title', '--jq', '.[0]']);
  if (!out || out === 'null') return null;
  return JSON.parse(out);
}
function openPrs() {
  const out = gh(['pr', 'list', '--repo', repoSlug(), '--state', 'open',
    '--json', 'number,headRefName', '--jq', '.']);
  if (!out || out === 'null') return [];
  return JSON.parse(out);
}
function branchName(week, isRedo) { return isRedo ? `week-${week}-redo` : `week-${week}`; }

// ---------------------------------------------------------------- 状态读写
function readActiveState() {
  git(['fetch', 'origin', 'main']);
  // 拉全部 week 分支：状态可能正挂在某一周的开放 PR 上
  git(['fetch', 'origin', '+refs/heads/week-*:refs/remotes/origin/week-*']);

  for (const p of openPrs()) {
    const raw = tryShow(`origin/${p.headRefName}:${STATE_PATH}`);
    if (!raw) continue;
    try {
      const st = JSON.parse(raw);
      if (st && st.inProgress && Number.isInteger(st.inProgress.week)) {
        return { state: st, source: `PR#${p.number}（${p.headRefName}）` };
      }
    } catch { /* 该分支上的状态解析不了，当作没有 */ }
  }
  const mainRaw = tryShow(`origin/main:${STATE_PATH}`);
  if (mainRaw) {
    try { return { state: JSON.parse(mainRaw), source: 'origin/main' }; } catch { /* 继续兜底 */ }
  }
  const local = path.join(ROOT, STATE_PATH);
  const rawLocal = readFileSafe(local);
  if (rawLocal) return { state: JSON.parse(rawLocal), source: '工作树' };
  throw new Error(`找不到 ${STATE_PATH}（开放 PR 分支 / origin/main / 工作树都没有）`);
}

// ---------------------------------------------------------------- 决策
function decisionFor(s, mergedWeek) {
  const st = s.state;
  const d = {
    source: s.source,
    lastCompletedWeek: st.lastCompletedWeek ?? 0,
    redoList: [...(st.redo || [])],
    mergedPR: mergedWeek ? { week: mergedWeek.week, pr: mergedWeek.pr } : null,
  };
  if (st.inProgress) {
    d.week = st.inProgress.week;
    d.isRedo = !!st.inProgress.redo || (st.redo || []).includes(st.inProgress.week);
    d.pr = st.inProgress.pr ?? null;
    d.branch = st.inProgress.branch || branchName(d.week, d.isRedo);
    d.action = d.pr ? 'wait' : 'continue';
    d.reason = d.pr ? `PR #${d.pr} 未合并，本周不推进` : 'inProgress 无 PR，继续本周';
  } else if ((st.redo || []).length) {
    d.week = st.redo[0];
    d.isRedo = true;
    d.pr = null;
    d.action = 'do';
    d.branch = branchName(d.week, true);
    d.reason = `redo 队首：第 ${d.week} 周（重做）`;
  } else {
    d.week = (st.lastCompletedWeek ?? 0) + 1;
    if (d.week > COURSE_MAX_WEEK) {
      return { ...d, action: 'done', week: null, branch: null,
               reason: `已完成全部 ${COURSE_MAX_WEEK} 周` };
    }
    d.isRedo = false;
    d.pr = null;
    d.action = 'do';
    d.branch = branchName(d.week, false);
    d.reason = `lastCompletedWeek+1：第 ${d.week} 周`;
  }
  return d;
}

// ---------------------------------------------------------------- 分支准备
function prepareBranch(d, planOnly) {
  if (d.action !== 'do' && d.action !== 'continue') return d;
  if (refExists(`origin/${d.branch}`)) {
    git(['fetch', 'origin', d.branch]);
    const p = prForBranch(d.branch);
    if (p && p.state === 'OPEN') {
      return { ...d, action: 'wait', pr: p.number,
               reason: `分支 ${d.branch} 已有开放 PR #${p.number}，不重复开工` };
    }
    return { ...d, created: false, reason: `${d.reason}；复用已有分支 ${d.branch}` };
  }
  if (planOnly) return { ...d, created: true, reason: `${d.reason}；将新建分支 ${d.branch}（从 origin/main）` };
  // 只在本地建 ref，不 checkout——状态文件留在当前工作分支上
  git(['branch', '-f', d.branch, 'origin/main']);
  git(['push', '-u', 'origin', d.branch]);
  return { ...d, created: true, reason: `${d.reason}；已从 origin/main 新建并推送 ${d.branch}` };
}

// 把 state 作为独立 commit 提交到目标分支并推送（commit 后切回原分支）。
function writeStateToBranch(branch, state, msg) {
  const cur = (() => {
    try {
      return git(['symbolic-ref', '--short', '-q', 'HEAD']);
    } catch { return ''; }
  })();
  if (!cur) throw new Error('当前不在任何分支上（detached HEAD），无法安全提交状态');

  const dirty = (git(['status', '--porcelain']) || '').split('\n').filter(Boolean);
  const tracked = dirty.filter(l => !l.startsWith('?? '));
  if (tracked.length) {
    throw new Error(`工作树有未提交的已跟踪改动，拒绝切换分支：\n${tracked.join('\n')}`);
  }
  try {
    const base = refExists(`origin/${branch}`) ? `origin/${branch}` : 'origin/main';
    git(['checkout', '-B', branch, base]);
    fs.mkdirSync(path.dirname(path.join(ROOT, STATE_PATH)), { recursive: true });
    fs.writeFileSync(path.join(ROOT, STATE_PATH), JSON.stringify(state, null, 2) + '\n');
    git(['add', STATE_PATH]);
    git(['commit', '-m', msg]);
  } finally {
    if (refExists(cur)) git(['checkout', '-q', cur]);
  }
  git(['push', 'origin', `${branch}:refs/heads/${branch}`]);
}

// ---------------------------------------------------------------- 主流程
const argv = process.argv.slice(2);
const planOnly = argv.includes('--plan');
const checkoutIdx = argv.indexOf('--checkout');

if (checkoutIdx >= 0) {
  const branch = argv[checkoutIdx + 1];
  if (!branch) { console.error('--checkout 需要分支名'); process.exit(1); }
  git(['fetch', 'origin', branch]);
  git(['checkout', '-B', branch, refExists(`origin/${branch}`) ? `origin/${branch}` : 'origin/main']);
  console.log(JSON.stringify({ action: 'checked-out', branch }, null, 2));
  process.exit(0);
}

const s = readActiveState();
console.error(`[state] 状态来源：${s.source}；lastCompletedWeek=${s.state.lastCompletedWeek}，redo=[${(s.state.redo || []).join(',')}]`);

// 1) 结清已合并的 PR
let mergedWeek = null;
if (s.state.inProgress && s.state.inProgress.pr) {
  try {
    const pr = prView(s.state.inProgress.pr);
    if (pr.state === 'MERGED' || pr.mergedAt) {
      mergedWeek = {
        week: s.state.inProgress.week,
        pr: s.state.inProgress.pr,
        branch: s.state.inProgress.branch,
      };
      s.state.lastCompletedWeek = Math.max(s.state.lastCompletedWeek ?? 0, s.state.inProgress.week);
      s.state.redo = (s.state.redo || []).filter(w => w !== s.state.inProgress.week);
      s.state.inProgress = null;
      s.state.updatedAt = iso();
      console.error(`[state] PR #${mergedWeek.pr} 已合并 → 第 ${mergedWeek.week} 周计入 lastCompletedWeek=${s.state.lastCompletedWeek}，redo=[${s.state.redo.join(',')}]`);
    }
  } catch (e) {
    console.error(`[state] 查询 PR #${s.state.inProgress.pr} 失败：${e.message}（按未合并处理，下一轮重试）`);
  }
}

// 2) 决策
let d = decisionFor(s, mergedWeek);

// 3) 分支准备
if (!planOnly && (d.action === 'do' || d.action === 'continue')) {
  d = prepareBranch(d, false);
  // 4) 新建分支时（含结清后立即开新周），回写 inProgress
  if (d.created) {
    const nextState = {
      ...s.state,
      redo: [...s.state.redo || []],
      lastCompletedWeek: s.state.lastCompletedWeek ?? 0,
      inProgress: { week: d.week, redo: d.isRedo, branch: d.branch, pr: null },
      updatedAt: iso(),
    };
    writeStateToBranch(d.branch, nextState,
      `chore(state): 第 ${d.week} 周${d.isRedo ? '（重做）' : ''} inProgress（待开 PR）`);
    s.state = nextState;
    console.error(`[state] inProgress 已写回 ${d.branch}（第 ${d.week} 周，PR 未开）`);
  }
  if (d.action === 'continue' && !d.created) {
    console.error(`[state] 继续第 ${d.week} 周：分支 ${d.branch} 已存在且无未合并 PR，直接复用`);
  }
} else if (planOnly && (d.action === 'do' || d.action === 'continue')) {
  d = prepareBranch(d, true);
}

console.log(JSON.stringify({
  action: d.action,
  week: d.week,
  isRedo: d.isRedo ?? false,
  branch: d.branch,
  pr: d.pr ?? null,
  reason: d.reason,
  mergedPR: d.mergedPR,
  state: {
    lastCompletedWeek: s.state.lastCompletedWeek ?? 0,
    redo: s.state.redo || [],
    inProgress: s.state.inProgress,
  },
}, null, 2));