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

// 轮询服务端采集开关的间隔。取 2 秒是因为"按了停止"到"板子真停"的延迟
// 就是这个值，太长会让按钮显得没反应。代价是每 2 秒多一次 HTTP 请求，
// 在局域网里可以忽略，而且它阻塞的时间也计入采样间隔的抖动。
#define CONTROL_POLL_MS     2000

// 第2周：轮询"有没有远程指令要领"的间隔。
// 与 CONTROL_POLL_MS 取同一个值：网页上点一下到板子开始动作，最坏就是 2 秒，
// 两个通道的响应手感一致，用户不必记"哪个按钮快哪个按钮慢"。
// 代价是每 2 秒多一次 HTTP 请求；局域网里可以忽略。
// 注意：指令执行期间不会轮询（一次只领一条，执行完再领），
// 所以这个值只影响"下发到开始执行"的排队时延，不影响执行本身。
#define COMMAND_POLL_MS     2000

// ---- 第3周：按键与物理反馈 ----
// PIN_BUTTON 用 GPIO0（BOOT 键）：这是板上唯一一个有实体按键、且引脚号
// 确定的 GPIO（低有效，按下接地）。佩戴场景不需要额外接按钮。
#define PIN_BUTTON         0
#define BUTTON_DEBOUNCE_MS 30    // 机械键抖动实测 5~20ms，取 30ms 留余量
#define BUTTON_QUEUE       8     // 待传按键队列：断网期间按下的键先存在板上
#define BUTTON_RETRY_MS    5000  // 上传失败后的重试节流
#define BUTTON_RETRY_MAX   10    // 单条事件的重试上限，放弃后计入 queue_dropped

// 【未实测，上板第一件事就是验证它】LED 引脚与有效电平。
// ESP32-S3-EYE 的公开资料没有给出一致的板载 LED 引脚；GPIO21 在相机/LCD/SD
// 的已知引脚表之外，大概率空闲，但没有实测依据。若上板发现 21 号没反应：
//   1) 用万用表/试灯确认板载 LED 实际接在哪个 GPIO；
//   2) 或外接一只 LED（串 330Ω）到任意空闲 GPIO，改这两个宏即可。
// 其余逻辑不依赖具体引脚：按键、上报、指令通道全部与 LED 无关。
#define PIN_LED            21
#define LED_ON_LEVEL       HIGH  // 若 LED 常亮不灭，说明是低有效，改成 LOW
// 上面两个宏在真机上验证过之后把它改成 1。notify 指令的回执会带上这个标志
// （led_pin_note=pin_unverified），于是"界面显示成功但灯其实没亮"这种事
// 在数据里就有痕迹，不会只在佩戴者嘴里。
#define PIN_LED_VERIFIED   0

#define FW_VERSION          "0.3.0"

// 必须明显小于服务端 db.NTP_FRESH_THRESHOLD_S(300 秒)。
// 原值 30 分钟远大于 300 秒，实测导致同步年龄一路上涨、3000 个样本全部被判成
// ntp_stale——可信度标签变成常量，等于没有信息。取 4 分钟留出重试与回调延迟的余量。
// 若同步真的失败，年龄会如实继续增长并越过阈值，那时报"陈旧/过期"才是对的。
#define NTP_RESYNC_MS       (4UL * 60UL * 1000UL)
