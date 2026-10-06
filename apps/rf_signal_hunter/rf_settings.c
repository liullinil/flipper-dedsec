#include "rf_settings.h"

#define RF_SETTINGS_PATH APP_DATA_PATH("rf_signal_hunter/settings.bin")

void rf_settings_defaults(RfHunterSettings* settings) {
    settings->dwell_ms = 250;
    settings->rssi_threshold_dbm = -75;
    settings->capture_ms = 1000;
    settings->silence_us = 8000;
    settings->band_profile = 0;
}

bool rf_settings_load(Storage* storage, RfHunterSettings* settings) {
    rf_settings_defaults(settings);
    File* file = storage_file_alloc(storage);
    bool ok = storage_file_open(file, RF_SETTINGS_PATH, FSAM_READ, FSOM_OPEN_EXISTING) &&
              storage_file_read(file, settings, sizeof(*settings)) == sizeof(*settings);
    if(storage_file_is_open(file)) storage_file_close(file);
    storage_file_free(file);
    if(settings->dwell_ms < 50 || settings->dwell_ms > 10000 || settings->capture_ms < 200 || settings->capture_ms > 2000 || settings->silence_us < 1000 || settings->silence_us > 30000) {
        rf_settings_defaults(settings);
        ok = false;
    }
    return ok;
}

bool rf_settings_save(Storage* storage, const RfHunterSettings* settings) {
    File* file = storage_file_alloc(storage);
    bool ok = storage_file_open(file, RF_SETTINGS_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS) &&
              storage_file_write(file, settings, sizeof(*settings)) == sizeof(*settings);
    if(ok) storage_file_sync(file);
    if(storage_file_is_open(file)) storage_file_close(file);
    storage_file_free(file);
    return ok;
}

