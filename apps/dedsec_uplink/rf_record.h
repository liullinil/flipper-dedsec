#pragma once

/* RF event records (schema_version 1, unchanged from the standalone RF
 * Signal Hunter) and the light local signal shape used for family counting
 * and Follow matching.  Pure functions: no HAL, no storage. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RF_SHAPE_BINS 12U

typedef struct {
    const char* event_id;
    const char* device_id;
    const char* session_id;
    uint32_t sequence;
    uint32_t rtc_local_unix; /* RTC calendar fields read as an epoch */
    int16_t tz_offset_minutes; /* RTC local time minus UTC */
    uint32_t monotonic_ms; /* since the engine session started */
    uint32_t battery_pct;
} RfRecordCommon;

typedef struct {
    RfRecordCommon common;
    const char* mode; /* "SCOUT", "CAPTURE", "FOLLOW" */
    uint32_t frequency_hz;
    uint32_t duration_us;
    uint32_t fingerprint;
    bool follow; /* writes follow_profile_id */
    uint32_t follow_fingerprint;
    float follow_similarity; /* 0..1 */
    float rssi_min_dbm;
    float rssi_avg_dbm;
    float rssi_max_dbm;
    uint32_t pulse_count; /* timings observed in this event */
    uint32_t last_duration_us;
    const uint32_t* timings; /* packed rf_capture timings, level bit ignored */
    uint32_t timing_count;
} RfSubGhzRecord;

typedef struct {
    RfRecordCommon common;
    uint32_t duration_ms;
    uint32_t field_count; /* field detections in this session */
} RfNfcRecord;

typedef struct {
    uint16_t bins[RF_SHAPE_BINS]; /* log2 duration histogram from 32 us */
    uint32_t count;
} RfShape;

/* UTC epoch of an RTC calendar epoch (never negative). */
uint32_t rf_record_utc_unix(uint32_t rtc_local_unix, int16_t tz_offset_minutes);

/* "YYYY-MM-DDTHH:MM:SSZ" (needs 21 bytes). */
void rf_record_format_utc(uint32_t unix_utc, char* out, size_t size);

/* Build one record ending in "}\n".  Timings that do not fit are left out
 * (pulse_count still counts them).  Returns the length, 0 if the record
 * cannot be built.  *timings_written may be NULL. */
size_t rf_record_subghz(
    char* out,
    size_t capacity,
    const RfSubGhzRecord* record,
    uint32_t* timings_written);
size_t rf_record_nfc(char* out, size_t capacity, const RfNfcRecord* record);

void rf_shape_compute(RfShape* shape, const uint32_t* timings, uint32_t count);
uint32_t rf_shape_fingerprint(const RfShape* shape, uint32_t frequency_hz);
float rf_shape_similarity(const RfShape* a, const RfShape* b); /* 0..1 */
