#pragma once

#include <Arduino.h>

#include "command.h"   // 复用 SnapshotFn/IdleFn：设备身份与时间事实仍由 main 现场提供

// 第3周：板上按键 -> 本地立即物理反馈 -> 上报服务端 -> Web 回应 -> 播放决定反馈。
//
// 闭环里"本地反馈"排在最前面，而且刻意不等网络：
// 佩戴者按下去的那一瞬间，WiFi 可能正好断了。如果反馈依赖服务端往返，
// 断网时按钮就是死的——用户分不清"设备坏了"还是"网断了"。
// 所以按下 -> LED 立刻闪，是纯本地动作；上报只是把事件送出去，
// 失败了在板上排队重试，排队溢出丢弃的数量随下一次成功上报如实带出去
// （queue_dropped 字段），失败不会静默消失。
//
// 与 command 模块的分工：本模块只管"上行"（按键事件）和"本地反馈的播放"；
// Web 的回应走第2周的指令通道（op=notify），由 command.cpp 领取后回调
// button_play_decision() 播放。本模块不持有任何指令状态。

// 接线：注册"设备此刻的样子"回调（与 command_begin 同一个回调，同一份事实）。
void button_begin(SnapshotFn snap);

// 在 loop() 里调用（不要求 WiFi 已连接：按键与本地反馈必须离线可用）。
// 内部做三件事：LED 图案推进、按键消抖扫描、待传事件的重试上传。
void button_poll();

// 播放"回应/取消"的物理反馈（阻塞到图案放完，期间回调 idle 让连续流照常跑）。
// decision 必须是 "ack" 或 "cancel"，其余值不播放并返回 false——
// 调用方（command.cpp）据此如实上报 bad_param，而不是假装放过了。
bool button_play_decision(const char *decision, IdleFn idle);

// 还有按键事件没传出去时为 true。仅用于串口日志与自检，不参与控制决策。
bool button_upload_pending();
