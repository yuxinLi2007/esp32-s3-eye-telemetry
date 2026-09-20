#pragma once

#include <Arduino.h>

#include "sensors.h"

// HTTP 上行统一出口（第2周新增）。
//
// 第1周里 ingest 与 control/state 各写了一遍 HTTPClient 样板；第2周又要加
// "领取指令"和"回执"两个端点。再抄两遍就有四份超时设置和四份令牌头，
// 改一处忘三处是迟早的事，所以这里收成一个函数：超时、令牌、URL 只有一份。
//
// 本模块不持有任何设备状态。状态只有一个来源（main.cpp 的采样器），
// 复制一份到这里必然会漂移——因此每次调用都由调用方在"调用那一刻"填好
// DeviceId / BatchMeta 传进来。

#define UPLINK_NO_WIFI      (-1)  // 未连上 WiFi，根本没发出去
#define UPLINK_BEGIN_FAILED (-2)  // URL 非法/套接字建不起来

struct DeviceId {
  const char *mac;        // 大写带冒号，与服务端 MAC_RE 一致
  const char *boot_id;    // 本次启动的随机标识
  const char *fw_version;
};

// 一批样本的溯源事实。request_id 为 nullptr 表示"连续流样本"，
// 非空表示这批样本是执行某条远程指令时采的。
struct BatchMeta {
  bool ntp_synced;
  uint32_t ntp_age_s;
  bool has_device_time;
  uint64_t t_device_ntp_ms;
  uint32_t dropped_since_last;
  const char *request_id;
};

// 底层：POST 一段 body，返回 HTTP 状态码；负值见上面两个 UPLINK_* 常量。
// resp 非空时把响应体带出来（调用方自己决定怎么解析/怎么报错）。
int uplink_post(const char *path, const String &body, const char *content_type,
                String *resp = nullptr, uint32_t timeout_ms = 5000);

// 把字符串转义成可以安全放进 JSON 引号里的形式。
// 板端不引 JSON 库，但错误消息里出现一个引号就能把整条回执变成非法 JSON，
// 于是"设备报了错"会变成"服务端解析失败"——错误被换成了另一个错误，更难查。
String json_escape(const char *s);

String uplink_ingest_json(const DeviceId &id, const BatchMeta &meta,
                          const Sample *samples, uint16_t n);

// 上传一批样本。返回 true 仅当服务端明确以 201 收下。
// batch_id_out 非空时回填服务端给的 batch_id（解析不到则为 -1，但不算失败：
// 201 已经是服务端的确认，batch_id 只是给指令回执做交叉引用用的）。
bool uplink_ingest(const DeviceId &id, const BatchMeta &meta,
                   const Sample *samples, uint16_t n, long *batch_id_out = nullptr);