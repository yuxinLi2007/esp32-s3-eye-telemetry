# 第 12 周：让用户纠错影响后续提醒 —— 主动提醒与纠错闭环记录

> 生成：`python tools/demo_week12.py`（对 127.0.0.1:8001 的现场运行，40 项检查，
> 结果：40 项检查，0 项失败）。
> 原始输出为控制台编码，此处已转 UTF-8 存档。当堂验证口径：
> ① 同一事件重放不重复触发提醒；② 用户关闭后不继续打扰；③ 能查看并清除本轮偏好。

## 一、有限反馈规则（显式、可枚举）

| 动词 | 语义 | 入口 | 落库 |
|---|---|---|---|
| confirm 确认 | 同意提醒提出的动作（"要重拍吗" → 立即下发重拍指令） | `POST /snapshots/{id}/ask` decision=confirm | feedback + asks=confirmed |
| correct 纠正 | 提醒依据判断有误（人工核对画面可用） | `POST /snapshots/{id}/corrections` | feedback + asks=superseded（询问作废） |
| ignore 忽略 | 这条不要了（进冷却期） | 语音"忽略" / `ask` decision=ignore | feedback + asks=ignored |
| later 稍后 | 稍后窗口（默认 10 分钟）内不主动打扰 | 语音"稍后再说" / `POST reminders/prefs` action=later | feedback + reminder_prefs=snooze |
| restore_default 恢复默认 | 清除本轮偏好，回到按默认规则提醒（配置版本回退） | 语音"恢复默认提醒" / `POST reminders/prefs` action=restore | feedback + reminder_pref_log（from→to） |

## 二、当堂验证三例（实现处）

| 验证点 | 实现 | 演示/测试断言 |
|---|---|---|
| 1. 同一事件重放不重复触发提醒 | 事件去重：`reminder_events` 的 `fire_count` 只在真正新建提醒时 +1；重放（同 frame 重新 analyze）只更新 `last_seen_ms`，且 `REMIND_DEDUP_MS` 窗口内 gate 返回 `dedup_replay` | A2 / B3 / B4：`replays_not_refired=1`，`fire_count` 恒为 1 |
| 2. 用户关闭后不继续打扰 | 偏好 `off` → gate 返回 `pref_off`，analyze 不再开新 ask（之前未答的询问保留） | A3 / A4：新帧 `ask_note=pref_off`，旧询问仍在 |
| 3. 能查看并清除本轮偏好 | `GET /api/v1/reminders/prefs` 匿名可查（on/off/snooze + 配置版本）；语音"恢复默认提醒"或 `restore` 清除并写回退记录 | A7 / B1 / B2：偏好回到 on，`v2 → v3` 回退记录留档 |

## 三、配置变化与版本回退记录（本轮）

| 版本 | 动作 | 偏好 | 说明 |
|---|---|---|---|
| v1 | 语音"这次不用提醒" → off | off | 关闭本轮提醒（feedback: off） |
| v2 | 语音"稍后再说" → later | snooze（until=now+10min） | 稍后窗口（feedback: later） |
| v3 | 语音"恢复默认提醒" → restore | on（默认） | 回退 v2→v3，pref_log 记 from=snooze → to=on（feedback: restore_default） |

说明：**保存反馈 ≠ 在线训练或自动学习成功**。反馈只调整上述显式规则
（开/关/稍后/恢复），不改模型、不引入任何个性化学习；`feedback` /
`reminder_pref_log` 仅作受控交互调整的记录与前后对照，并向后续课程输出
结构化反馈样例。

## 四、前后对照（实践2：重复提醒次数与任务结果）

- 偏好变更前提醒触发：**1 次**（A1）；变更后：**3 次**（A8 / A10 / A11 恢复后才触发）。
- 同一事件重放未再触发：**1 次**（A2 force 重跑同一帧）。
- 询问终局：`confirmed=1 / ignored=1 / superseded=1 / open=1`（4 条总询问）。
- 反馈汇总：确认 1、纠正 1、忽略 1、稍后 1、关闭 1、恢复默认 1（6 种动词齐全）。

## 五、运行输出（demo_week12.py）

```
PASS  服务端可达 /health
PASS  设备登记批次上报  batch_id=1

=== A1 默认开启：遮挡帧 → 主动提醒（要重拍吗）===
PASS  A1 询问打开  round ask#1
PASS  A1 事件 fire_count=1  round_id=1

=== A2 同一事件重放：不重复触发提醒 ===
PASS  A2 重放仍返回同一条询问
PASS  A2 fire_count 仍为 1（不重复触发）  last_seen=1793547969290 last_fired=1793547969276

=== A3 语音「这次不用提醒」→ 偏好 off ===
PASS  A3 意图 remind_off
PASS  A3 答复带配置版本  好的，已记住：本轮起不再主动提醒（配置版本 v1）。想恢复就说“恢复默认提醒”。
PASS  A3 偏好已关闭
PASS  A3 反馈 off 已记录

=== A4 用户关闭后：新帧不再打扰（前/后对照）===
PASS  A4 不发起新询问
PASS  A4 旧询问仍在（未答），新帧不新增
PASS  A4 前后对照：变更前 1 次 / 变更后 0 次  before=1 after=0

=== A5 语音「稍后再说」→ 稍后窗口 ===
PASS  A5 意图 remind_later
PASS  A5 偏好 snooze + 版本 v2

=== A6 稍后窗口内：新帧拦截 ===
PASS  A6 被稍后窗口拦住

=== A7 语音「恢复默认提醒」→ 配置版本回退 ===
PASS  A7 意图 remind_restore + 回退记录
PASS  A7 偏好回到 on（默认）
PASS  A7 反馈 restore_default 已记录

=== A8 恢复默认后：新帧又能提醒（fire #2）===
PASS  A8 询问重新打开
PASS  A8 事件 fire_count=1（第 2 个事件）

=== A9 纠正（人工核对：画面可用）→ 询问作废 + feedback correct ===
PASS  A9 纠正成功
PASS  A9 询问作废（superseded）
PASS  A9 反馈 correct 已记录

=== A10 确认重拍 → 重拍指令 + feedback confirm ===
PASS  A10 确认成功且下发重拍指令
PASS  A10 反馈 confirm 已记录
PASS  A10 重拍指令可被设备领取  req_0mvy05ibd_aecc75|image||30000

=== A11 语音「忽略」→ 关闭询问 + 冷却（拒绝后不反复打扰）===
PASS  A11 新提示已打开
PASS  A11 意图 ask_ignore + 冷却提示
PASS  A11 反馈 ignore 已记录

=== A12 冷却窗口内：新帧不再询问 ===
PASS  A12 被冷却拦住

=== B1 查看本轮偏好（用户可见、可撤回）===
PASS  B1 显示偏好与配置版本
PASS  B1 反馈可查（有限动词集合）  verbs=confirm,correct,ignore,later,off,restore_default

=== B2 语音「提醒设置」→ remind_review 只读 ===
PASS  B2 意图 remind_review
PASS  B2 答复带规则/配置版本与反馈记录  提醒偏好（规则版本 week12-r1，配置版本 v3）：按默认规则提醒（开）。 反馈记录：confirm 1 次、correct 1 次、

=== B3 提醒触发与去重日志（重放不重复触发）===
PASS  B3 触发事件都只 fire 1 次（重放只更新 last_seen）  fired_events=4

=== B4 最终前后对照（实践2：重复提醒次数 + 任务结果 + 反馈汇总）===
PASS  B4 变更前 1 次 / 变更后 3 次触发  before=1 after=3
PASS  B4 重放未重复触发 ≥1  replays=1
PASS  B4 任务结果统计完整  asks={'total': 4, 'confirmed': 1, 'ignored': 1, 'open': 1, 'superseded': 1}
PASS  B4 反馈汇总 = 6 种动词  fb=['confirm', 'correct', 'ignore', 'later', 'off', 'restore_default']

结果：40 项检查，0 项失败
```
