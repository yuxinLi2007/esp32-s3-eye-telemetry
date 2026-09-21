#pragma once

// 所有硬件常量均由板上实测确定，不是抄数据手册。验证过程见 git 提交记录。
#define PIN_I2C_SDA      4
#define PIN_I2C_SCL      5
#define PIN_I2S_BCLK     41   // 标准 I2S，非 PDM
#define PIN_I2S_WS       42
#define PIN_I2S_DIN      2

#define ACCEL_I2C_ADDR   0x12
#define ACCEL_REG_DATA   0x01 // X/Y/Z 各一对 LSB/MSB，共 6 字节
// QMA6100P 其余寄存器（与 esp-bsp/components/qma6100p 驱动一致）。
// 上电后芯片默认处于 suspend，数据寄存器会恒返回 0x0000——这就是
// "三轴全是 0、合矢量没反应"的根因：I2C 通了、读也没报错，但读到的全是零。
#define ACCEL_REG_WHO_AM_I  0x00 // 器件 ID
#define ACCEL_REG_ACCEL_CFG 0x0F // 低 4 位量程：0001=±2g 0010=±4g ...
#define ACCEL_REG_PWR_MGMT  0x11 // bit7 = 唤醒（active），默认 0 = suspend
#define ACCEL_REG_NVM_LOAD  0x33 // bit3 = 从 NVM 载入出厂校准
#define ACCEL_FS_2G         0x01 // ±2g，灵敏度 4096 LSB/g
#define ACCEL_WAKE_SETTLE_MS 50  // 唤醒后等待首个有效样本的时间
// 连续读到全零多少次就判定"疑似未唤醒"并如实打印，不再当成合法的 0g 数据。
#define ACCEL_ZERO_STREAK_WARN 20

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

// LED 引脚与有效电平：已与官方 BSP(esp-bsp/bsp/esp32_s3_eye) 核对——
// BSP_LED_1_IO = GPIO_NUM_3，BSP_LED_1_LEVEL = true（高电平点亮）。
// 此前用的 GPIO21 是错的：那是 LCD 的 PCLK（BSP_LCD_PCLK），所以按键时灯不亮。
// 板载 LED 是 GPIO 型单色灯，直接 digitalWrite 即可，不需要 WS2812 驱动。
#define PIN_LED            3
#define LED_ON_LEVEL       HIGH
// 引脚来源为官方 BSP 定义，不再是"猜测的空闲脚"，故标记为已验证。
// notify 指令回执带的 led_pin_note 也会随之不再报 pin_unverified。
#define PIN_LED_VERIFIED   1

#define FW_VERSION          "0.3.1"

// 必须明显小于服务端 db.NTP_FRESH_THRESHOLD_S(300 秒)。
// 原值 30 分钟远大于 300 秒，实测导致同步年龄一路上涨、3000 个样本全部被判成
// ntp_stale——可信度标签变成常量，等于没有信息。取 4 分钟留出重试与回调延迟的余量。
// 若同步真的失败，年龄会如实继续增长并越过阈值，那时报"陈旧/过期"才是对的。
#define NTP_RESYNC_MS       (4UL * 60UL * 1000UL)
