#pragma once
#include <furi.h>
#include <storage/storage.h>

typedef struct {
    uint32_t dwell_ms;
    int16_t rssi_threshold_dbm;
    uint16_t capture_ms;
    uint16_t silence_us;
    uint8_t band_profile;
    /* The RTC exposes calendar fields.  This is the signed offset used to
       turn those fields into an absolute UTC epoch in event records. */
    int16_t timezone_offset_minutes;
    /* Retention is deliberately explicit: a full card must never silently
       overwrite pending evidence. */
    uint8_t retention_policy;
    /* User feedback is enabled by default; this field replaced the old
       reserved byte in settings version 3. */
    uint8_t feedback_enabled;
    uint32_t min_free_bytes;
    uint32_t version;
} RfHunterSettings;

typedef enum {
    /* Keep pending records until an ACK.  ACKed raw records are reclaimed. */
    RfRetentionCompactAfterAck = 0,
    /* Keep full ACKed captures on the device in addition to receipts. */
    RfRetentionKeepCapture = 1,
    /* Stop accepting new events when the free-space guard is reached. */
    RfRetentionStopWhenFull = 2,
} RfRetentionPolicy;

#define RF_SETTINGS_VERSION 3U
#define RF_SETTINGS_DEFAULT_MIN_FREE_BYTES (32U * 1024U)

void rf_settings_defaults(RfHunterSettings* settings);
bool rf_settings_load(Storage* storage, RfHunterSettings* settings);
bool rf_settings_save(Storage* storage, const RfHunterSettings* settings);
