#pragma once

// 所有硬件常量均由板上实测确定，不是抄数据手册。验证过程见 git 提交记录。
#define PIN_I2C_SDA      4
#define PIN_I2C_SCL      5
#define PIN_I2S_BCLK     41   // 标准 I2S，非 PDM
#define PIN_I2S_WS       42
#define PIN_I2S_DIN      2

#define ACCEL_I2C_ADDR   0x12
#define ACCEL_REG_DATA   0x01 // X/Y/Z 各一对 LSB/MSB，共 6 字节

// 麦克风输出的是"32 位帧内的 24 位数据"，必须按 32 位读再右移，
// 按 16 位读会帧错位，得到对声音无反应的小幅乱跳。
#define I2S_FRAMES       256
#define MIC_SAMPLE_RATE  16000

// 灵敏度 -26 dBFS @ 94 dB SPL ⇒ dBFS = SPL - 120，反推 SPL = dBFS + 120。
// 实测印证：静室底噪约 -70 dBFS ⇒ 50 dB SPL；拍手约 -38 dBFS ⇒ 82 dB SPL。
#define MIC_DBFS_TO_SPL  120.0f

// 量程标定：用"静止时合矢量 |a| = 1g"实测得出，不是抄数据手册。
// 初值 8192 时实测 |a|=1.610（1000 样本内 1.575~1.656，证明板子确为静止），
// 故 8192 × 1.610 ≈ 13189。修正后需复核 |a| 是否回到 1.00。
//
// 局限：单姿态标定无法把"量程误差"和"零偏误差"分开。若修正后不同姿态下
// |a| 仍明显偏离 1，需要多姿态拟合才能同时解出零偏。当前值只保证本姿态正确。
#define ACCEL_LSB_PER_G  13189.0f

#define SAMPLE_INTERVAL_MS  50    // 20 Hz
#define UPLOAD_INTERVAL_MS  2000
#define RING_CAPACITY       240   // 12 秒缓冲
#define MAX_BATCH           100   // 单次上传样本上限

#define FW_VERSION          "0.1.0"

// 必须明显小于服务端 db.NTP_FRESH_THRESHOLD_S(300 秒)。
// 原值 30 分钟远大于 300 秒，实测导致同步年龄一路上涨、3000 个样本全部被判成
// ntp_stale——可信度标签变成常量，等于没有信息。取 4 分钟留出重试与回调延迟的余量。
// 若同步真的失败，年龄会如实继续增长并越过阈值，那时报"陈旧/过期"才是对的。
#define NTP_RESYNC_MS       (4UL * 60UL * 1000UL)
