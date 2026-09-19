#include "sensors.h"

#include <Wire.h>

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

bool sensors_begin() {
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);

  // 探测加速度计是否在线，避免"读到全 0 还当成有效数据"这种静默失败
  Wire.beginTransmission(ACCEL_I2C_ADDR);
  bool accel_ok = (Wire.endTransmission() == 0);
  Serial.printf("[sensors] 加速度计 0x%02X %s\n", ACCEL_I2C_ADDR,
                accel_ok ? "在线" : "无响应");

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
