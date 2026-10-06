#pragma once
#include <furi.h>
#include <storage/storage.h>

typedef struct {
    uint32_t dwell_ms;
    int16_t rssi_threshold_dbm;
    uint16_t capture_ms;
    uint16_t silence_us;
    uint8_t band_profile;
} RfHunterSettings;

void rf_settings_defaults(RfHunterSettings* settings);
bool rf_settings_load(Storage* storage, RfHunterSettings* settings);
bool rf_settings_save(Storage* storage, const RfHunterSettings* settings);

