#include "rf_settings.h"

#include <stddef.h>
#include <string.h>

#define RF_SETTINGS_PATH APP_DATA_PATH("rf_signal_hunter/settings.bin")

void rf_settings_defaults(RfHunterSettings* settings) {
    memset(settings, 0, sizeof(*settings));
    settings->dwell_ms = 250;
    settings->rssi_threshold_dbm = -75;
    settings->capture_ms = 1000;
    settings->silence_us = 8000;
    settings->band_profile = 0;
    settings->timezone_offset_minutes = 0;
    settings->retention_policy = RfRetentionCompactAfterAck;
    settings->min_free_bytes = RF_SETTINGS_DEFAULT_MIN_FREE_BYTES;
    settings->version = RF_SETTINGS_VERSION;
}

bool rf_settings_load(Storage* storage, RfHunterSettings* settings) {
    if(!storage || !settings) return false;
    rf_settings_defaults(settings);
    /* If reset interrupted a replacement, restore the last synced settings. */
    FileInfo info;
    if(storage_common_stat(storage, RF_SETTINGS_PATH, &info) != FSE_OK) {
        storage_common_rename(storage, RF_SETTINGS_PATH ".bak", RF_SETTINGS_PATH);
    } else {
        storage_common_remove(storage, RF_SETTINGS_PATH ".bak");
    }
    storage_common_remove(storage, RF_SETTINGS_PATH ".part");
    /* Read into a bounded buffer so an interrupted write or a pre-v2 settings
       file cannot partially overwrite the defaults.  The first five fields
       are layout-compatible with the original settings.bin. */
    uint8_t raw[sizeof(RfHunterSettings)] = {0};
    File* file = storage_file_alloc(storage);
    size_t n = 0;
    bool ok = false;
    if(storage_file_open(file, RF_SETTINGS_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        n = storage_file_read(file, raw, sizeof(raw));
        ok = n >= offsetof(RfHunterSettings, timezone_offset_minutes);
    }
    if(storage_file_is_open(file)) storage_file_close(file);
    storage_file_free(file);

    if(ok) {
        /* Restore the legacy prefix first.  New fields are restored only when
           a complete, versioned record is present. */
        memcpy(settings, raw, offsetof(RfHunterSettings, timezone_offset_minutes));
        if(n == sizeof(RfHunterSettings)) {
            RfHunterSettings candidate;
            memcpy(&candidate, raw, sizeof(candidate));
            if(candidate.version == RF_SETTINGS_VERSION) {
                settings->timezone_offset_minutes = candidate.timezone_offset_minutes;
                settings->retention_policy = candidate.retention_policy;
                settings->reserved = candidate.reserved;
                settings->min_free_bytes = candidate.min_free_bytes;
                settings->version = candidate.version;
            }
        }
    }

    if(settings->dwell_ms < 50 || settings->dwell_ms > 10000 ||
       settings->capture_ms < 200 || settings->capture_ms > 2000 ||
       settings->silence_us < 1000 || settings->silence_us > 30000 ||
       settings->timezone_offset_minutes < -14 * 60 ||
       settings->timezone_offset_minutes > 14 * 60 ||
       settings->retention_policy > RfRetentionStopWhenFull ||
       settings->min_free_bytes < 4096 || settings->min_free_bytes > (4U * 1024U * 1024U)) {
        rf_settings_defaults(settings);
        ok = false;
    }
    return ok;
}

bool rf_settings_save(Storage* storage, const RfHunterSettings* settings) {
    if(!storage || !settings) return false;
    RfHunterSettings value = *settings;
    value.version = RF_SETTINGS_VERSION;
    if(value.dwell_ms < 50 || value.dwell_ms > 10000 ||
       value.capture_ms < 200 || value.capture_ms > 2000 ||
       value.silence_us < 1000 || value.silence_us > 30000 ||
       value.timezone_offset_minutes < -14 * 60 ||
       value.timezone_offset_minutes > 14 * 60 ||
       value.retention_policy > RfRetentionStopWhenFull ||
       value.min_free_bytes < 4096 || value.min_free_bytes > (4U * 1024U * 1024U)) {
        return false;
    }
    storage_common_mkdir(storage, APP_DATA_PATH("rf_signal_hunter"));
    File* file = storage_file_alloc(storage);
    const char* temporary = RF_SETTINGS_PATH ".part";
    bool ok = storage_file_open(file, temporary, FSAM_WRITE, FSOM_CREATE_ALWAYS) &&
              storage_file_write(file, &value, sizeof(value)) == sizeof(value);
    if(ok) ok = storage_file_sync(file);
    if(storage_file_is_open(file)) storage_file_close(file);
    storage_file_free(file);
    if(!ok) {
        storage_common_remove(storage, temporary);
        return false;
    }
    FileInfo info;
    bool had_previous = storage_common_stat(storage, RF_SETTINGS_PATH, &info) == FSE_OK;
    storage_common_remove(storage, RF_SETTINGS_PATH ".bak");
    if(had_previous && storage_common_rename(storage, RF_SETTINGS_PATH, RF_SETTINGS_PATH ".bak") != FSE_OK) {
        storage_common_remove(storage, temporary);
        return false;
    }
    ok = storage_common_rename(storage, temporary, RF_SETTINGS_PATH) == FSE_OK;
    if(ok) storage_common_remove(storage, RF_SETTINGS_PATH ".bak");
    else {
        storage_common_remove(storage, temporary);
        if(had_previous) storage_common_rename(storage, RF_SETTINGS_PATH ".bak", RF_SETTINGS_PATH);
    }
    return ok;
}
