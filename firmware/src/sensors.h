#pragma once

#include <Arduino.h>

struct Sample {
  uint32_t seq;
  uint32_t t_device_ms;
  float ax;
  float ay;
  float az;
  float spl_db;
};

// 麦克风采集窗口的元信息，用于判断 SPL 值是否可信
struct MicStatus {
  bool ok;
  uint32_t frames_read;
};

bool sensors_begin();
bool accel_read(float *ax, float *ay, float *az);
bool mic_read_spl(float *spl_db);
MicStatus mic_last_status();
