#include "sensors.h"

#include <Wire.h>
#include <math.h>

#include "config.h"
#include "driver/i2s.h"

static MicStatus g_mic = {false, 0};
static int32_t g_i2s_buf[I2S_FRAMES];

static bool i2s_begin() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = MIC_SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT; // 单声道麦只驱动左槽，实测右槽恒为 0
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 4;
  cfg.dma_buf_len = I2S_FRAMES;
  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr) != ESP_OK) return false;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_I2S_BCLK;
  pins.ws_io_num = PIN_I2S_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_I2S_DIN;
  if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) {
    i2s_driver_uninstall(I2S_NUM_0);
    return false;
  }
  i2s_zero_dma_buffer(I2S_NUM_0);
  i2s_start(I2S_NUM_0);
  return true;
}

// 读写 QMA6100P 单个寄存器。返回 false 表示 I2C 层面就没通。
static bool accel_reg_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool accel_reg_read(uint8_t reg, uint8_t *val) {
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)ACCEL_I2C_ADDR, (uint8_t)1) != 1) return false;
  *val = Wire.read();
  return true;
}

bool sensors_begin() {
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);

  // 探测加速度计是否在线，避免"读到全 0 还当成有效数据"这种静默失败
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  bool accel_ok = (Wire.endTransmission() == 0);
  Serial.printf("[sensors] 加速度计 0x%02X %s\n", ACCEL_I2C_ADDR,
                accel_ok ? "在线" : "无响应");

  // ---- QMA6100P 上电配置 --------------------------------------------------
  // 2026-09-21 排查：三轴在库里恒为 0.000（nulls=0，不是读失败），合矢量画成
  // 一条没有反应的平线。根因是这颗芯片上电后默认处于 suspend，数据寄存器
  // 会稳定返回 0x0000——I2C 通、读也没报错，于是"没有数据"被伪装成"静止"。
  // 修复=按 esp-bsp 的 qma6100p 驱动顺序唤醒，并显式写定量程。
  if (accel_ok) {
    uint8_t who = 0;
    if (accel_reg_read(ACCEL_REG_WHO_AM_I, &who))
      Serial.printf("[sensors] WHO_AM_I=0x%02X\n", who);

    // 1) 载入 NVM 出厂校准（bit3 自清）。跳过它会让零偏带着出厂残值。
    uint8_t nvm = 0;
    if (accel_reg_read(ACCEL_REG_NVM_LOAD, &nvm))
      accel_reg_write(ACCEL_REG_NVM_LOAD, nvm | 0x08);

    // 2) 唤醒：写 PWR_MGMT 使芯片进入 active。
    //    实测（2026-09-21）：写 0x80 后回读仍是 0x00，位没被接受；
    //    同系列的 QMA7981 例程用的是 0xC0。故按候选值逐个试，以"回读
    //    确认 bit7 置位"为准，而不是写完就假定成功。
    uint8_t pwr = 0;
    accel_reg_read(ACCEL_REG_PWR_MGMT, &pwr);
    const uint8_t wake_cands[] = {0xC0, 0x80, 0x40};
    uint8_t pwr2 = 0;
    for (uint8_t c : wake_cands) {
      accel_reg_write(ACCEL_REG_PWR_MGMT, (uint8_t)((pwr & 0x3F) | c));
      delay(5);
      accel_reg_read(ACCEL_REG_PWR_MGMT, &pwr2);
      Serial.printf("[sensors] 尝试写入 PWR_MGMT=0x%02X -> 回读 0x%02X\n",
                    (uint8_t)((pwr & 0x3F) | c), pwr2);
      if (pwr2 & 0x80) break;   // 已进入 active，不必再试
    }

    // 3) 量程显式写成 ±2g（低 4 位 = 0001）。不写的话量程取决于上电残留，
    //    LSB/g 会变，标定常数就失去意义。
    uint8_t cfg = 0;
    if (!accel_reg_read(ACCEL_REG_ACCEL_CFG, &cfg)) cfg = 0;
    accel_reg_write(ACCEL_REG_ACCEL_CFG, (cfg & 0xF0) | ACCEL_FS_2G);

    delay(ACCEL_WAKE_SETTLE_MS);

    // 回读配置，留一条"到底写进去没有"的证据，而不是靠猜。
    uint8_t cfg2 = 0;
    accel_reg_read(ACCEL_REG_PWR_MGMT, &pwr2);
    accel_reg_read(ACCEL_REG_ACCEL_CFG, &cfg2);
    Serial.printf("[sensors] 唤醒=%s PWR_MGMT=0x%02X ACCEL_CFG=0x%02X "
                  "(期望 bit7=1 / 低4位=%d)\n",
                  (pwr2 & 0x80) ? "OK" : "失败", pwr2, cfg2, ACCEL_FS_2G);
    if (!(pwr2 & 0x80)) {
      Serial.println("[sensors] 警告：加速度计未进入 active，数据可能恒为 0");
      accel_ok = false;
    }

    // 首次读数自检：打印原始计数，便于现场核对 LSB/g 标定。
    float ax = NAN, ay = NAN, az = NAN;
    if (accel_ok && accel_read(&ax, &ay, &az)) {
      Serial.printf("[sensors] 首读 ax=%.3f ay=%.3f az=%.3f |a|=%.3f g "
                    "(计数 %d/%d/%d, LSB/g=%.0f)\n",
                    ax, ay, az, sqrt(ax * ax + ay * ay + az * az),
                    (int)lroundf(ax * ACCEL_LSB_PER_G), (int)lroundf(ay * ACCEL_LSB_PER_G),
                    (int)lroundf(az * ACCEL_LSB_PER_G), ACCEL_LSB_PER_G);
    }
  }

  g_mic.ok = i2s_begin();
  Serial.printf("[sensors] 麦克风 I2S %s\n", g_mic.ok ? "就绪" : "初始化失败");

  if (g_mic.ok) {
    // 丢掉启动瞬态：首帧实测可达满量程的 77%，不丢会污染第一批数据
    for (int i = 0; i < 20; i++) {
      size_t got = 0;
      i2s_read(I2S_NUM_0, g_i2s_buf, sizeof(g_i2s_buf), &got, 200);
    }
  }
  return accel_ok && g_mic.ok;
}

bool accel_read(float *ax, float *ay, float *az) {
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  Wire.write(ACCEL_REG_DATA);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)ACCEL_I2C_ADDR, (uint8_t)6) != 6) return false;

  int16_t raw[3];
  for (int i = 0; i < 3; i++) {
    uint8_t lo = Wire.read();
    uint8_t hi = Wire.read();
    raw[i] = (int16_t)((hi << 8) | lo);
  }

  // 三轴精确为 0 = 芯片没在出数（suspend/掉电/总线被拉死），而不是"姿态恰好
  // 让重力为零"——真实加速度计静止时至少有一轴接近 ±1g，且总有噪声。
  // 按无效数据处理（上层写 NaN、入库为 null），并连续告警，别让恒零冒充数据。
  static uint32_t zero_streak = 0;
  static uint32_t last_warn_n = 0;
  if (raw[0] == 0 && raw[1] == 0 && raw[2] == 0) {
    zero_streak++;
    if (zero_streak == ACCEL_ZERO_STREAK_WARN && zero_streak != last_warn_n) {
      last_warn_n = zero_streak;
      Serial.printf("[sensors] 警告：加速度计连续 %lu 次读到全零，"
                    "疑似未唤醒/未出数，已按无效值(NaN)处理\n",
                    (unsigned long)zero_streak);
    }
    return false;
  }
  zero_streak = 0;

  *ax = raw[0] / ACCEL_LSB_PER_G;
  *ay = raw[1] / ACCEL_LSB_PER_G;
  *az = raw[2] / ACCEL_LSB_PER_G;
  return true;
}

bool mic_read_spl(float *spl_db) {
  g_mic.frames_read = 0;
  if (!g_mic.ok) return false;

  size_t got = 0;
  if (i2s_read(I2S_NUM_0, g_i2s_buf, sizeof(g_i2s_buf), &got, 200) != ESP_OK ||
      got == 0)
    return false;

  int n = got / 4;
  double acc = 0;
  for (int i = 0; i < n; i++) {
    int32_t s = g_i2s_buf[i] >> 8; // 取出真正的 24 位样本
    acc += (double)s * s;
  }
  double rms = sqrt(acc / n);
  g_mic.frames_read = n;

  if (rms <= 0.5) {
    // 全零意味着通道没在出数（时钟或数据线问题），不能报成一个"很小的声音"
    *spl_db = NAN;
    return false;
  }
  *spl_db = 20.0 * log10(rms / 8388608.0) + MIC_DBFS_TO_SPL;
  return true;
}

MicStatus mic_last_status() { return g_mic; }
