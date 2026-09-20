#pragma once

#include <Arduino.h>

// 第2周：Web 远程采集指令的设备端。
//
// 与第1周的分工：main.cpp 只管"连续采集 + 上传"，本模块只管
// "领取一条指令 -> 真去读传感器 -> 把结果如实回报"。
// 两边都不复制对方的状态：本模块要用到设备身份与时间事实时，
// 通过 command_begin() 注册的回调在"用的那一刻"向 main 取。
//
// 刻意不做的事：
//   - 不缓存指令队列。领一条执行一条，执行完再领下一条。
//     板子掉线重启后，服务端会按超时把指令重新入队或判超时，
//     板上再存一份队列就有两个事实来源，谁说了算不清楚。
//   - 不在板上解析 JSON。领取应答是纯文本 `request_id|op|k=v;k=v|timeout_ms`，
//     少一个库、少几百字节 RAM、少一个可能失败的分支。
//     能这么干的前提是服务端已把 op / 参数名 / 参数值都按白名单校验过，
//     分隔符 | ; = 不可能出现在任何字段里。

// 指令执行期间要如实回报的"设备此刻的样子"。全部是取值，不是快照：
// 由 main.cpp 在回调里现场填，避免两份状态漂移。
struct DeviceSnapshot {
  const char *mac;             // 指向 main 里的常驻缓冲，本模块不复制
  const char *boot_id;
  const char *fw_version;
  bool ntp_synced;
  uint32_t ntp_age_s;
  bool has_device_time;
  uint64_t t_device_ntp_ms;
  uint32_t dropped_since_last;  // 连续流本轮未上报的丢弃数
  uint32_t stream_seq;          // 连续流下一个 seq（仅用于 ping 回报）
  uint16_t ring_count;          // 环形缓冲水位
  uint16_t ring_capacity;
  bool collect_enabled;         // 连续采集开关当前状态
};

typedef void (*SnapshotFn)(DeviceSnapshot *out);

// 指令执行（尤其是 capture 的等待间隔）期间回调它，让连续采集与上传照常进行。
// 不这么做的话，一次 10 秒的指令采集会把连续流整整堵住 10 秒，
// 界面上就会出现一条红色"上传中断"竖带——把用户自己点的按钮显示成设备故障。
typedef void (*IdleFn)();

void command_begin(SnapshotFn snap, IdleFn idle);

// 在 loop() 里调用；内部按 COMMAND_POLL_MS 节流。要求调用前 WiFi 已连接。
void command_poll();

// 正在执行指令时为 true。仅用于串口日志与自检，不参与任何控制决策。
bool command_executing();