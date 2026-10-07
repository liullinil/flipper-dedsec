#include "rf_record.h"
#include "rf_capture.h"

#include <stdio.h>
#include <string.h>

static const char rf_record_suffix[] = "],\"upload_state\":\"pending\"}\n";

uint32_t rf_record_utc_unix(uint32_t rtc_local_unix, int16_t tz_offset_minutes) {
    int64_t utc = (int64_t)rtc_local_unix - (int64_t)tz_offset_minutes * 60;
    if(utc < 0) utc = 0;
    if(utc > (int64_t)UINT32_MAX) utc = UINT32_MAX;
    return (uint32_t)utc;
}

/* Days since 1970-01-01 -> civil date (proleptic Gregorian, H. Hinnant). */
static void rf_civil_from_days(uint32_t days, uint32_t* year, uint32_t* month, uint32_t* day) {
    uint32_t z = days + 719468U;
    uint32_t era = z / 146097U;
    uint32_t doe = z - era * 146097U;
    uint32_t yoe = (doe - doe / 1460U + doe / 36524U - doe / 146096U) / 365U;
    uint32_t doy = doe - (365U * yoe + yoe / 4U - yoe / 100U);
    uint32_t mp = (5U * doy + 2U) / 153U;
    *day = doy - (153U * mp + 2U) / 5U + 1U;
    *month = mp < 10U ? mp + 3U : mp - 9U;
    *year = yoe + era * 400U + (*month <= 2U ? 1U : 0U);
}

void rf_record_format_utc(uint32_t unix_utc, char* out, size_t size) {
    uint32_t year, month, day;
    uint32_t seconds = unix_utc % 86400U;
    rf_civil_from_days(unix_utc / 86400U, &year, &month, &day);
    snprintf(
        out,
        size,
        "%04lu-%02lu-%02luT%02lu:%02lu:%02luZ",
        (unsigned long)year,
        (unsigned long)month,
        (unsigned long)day,
        (unsigned long)(seconds / 3600U),
        (unsigned long)(seconds / 60U % 60U),
        (unsigned long)(seconds % 60U));
}

static size_t rf_u32_text(uint32_t value, char* out) {
    char reversed[10];
    size_t n = 0;
    do {
        reversed[n++] = (char)('0' + value % 10U);
        value /= 10U;
    } while(value);
    for(size_t i = 0; i < n; i++) out[i] = reversed[n - 1 - i];
    return n;
}

size_t rf_record_subghz(
    char* out,
    size_t capacity,
    const RfSubGhzRecord* record,
    uint32_t* timings_written) {
    if(timings_written) *timings_written = 0;
    if(!out || !record || capacity < sizeof(rf_record_suffix)) return 0;
    const RfRecordCommon* c = &record->common;
    uint32_t utc = rf_record_utc_unix(c->rtc_local_unix, c->tz_offset_minutes);
    char when[24];
    rf_record_format_utc(utc, when, sizeof(when));
    char profile[24] = "";
    if(record->follow) {
        snprintf(
            profile, sizeof(profile), "local-%08lx", (unsigned long)record->follow_fingerprint);
    }
    float similarity = record->follow_similarity;
    if(!(similarity >= 0.0f)) similarity = 0.0f;
    if(similarity > 1.0f) similarity = 1.0f;
    int head = snprintf(
        out,
        capacity,
        "{\"schema_version\":1,\"event_id\":\"%s\",\"device_uuid\":\"%s\",\"session_id\":\"%s\","
        "\"sequence_number\":%lu,\"captured_at_utc\":\"%s\",\"captured_at_unix\":%lu,"
        "\"timezone_offset_minutes\":%d,\"rtc_local_unix\":%lu,\"monotonic_ms\":%lu,"
        "\"source_type\":\"subghz\",\"mode\":\"%s\",\"frequency_hz\":%lu,\"modulation\":\"OOK\","
        "\"bandwidth_hz\":0,\"duration_us\":%lu,\"repeat_count\":1,\"battery_pct\":%lu,"
        "\"fingerprint_id\":\"local-%08lx\",\"family_id\":null,\"classification\":\"unknown\","
        "\"classification_confidence\":0.0,\"follow_profile_id\":\"%s\",\"follow_similarity\":%.3f,"
        "\"rssi_min_dbm\":%.1f,\"rssi_avg_dbm\":%.1f,\"rssi_max_dbm\":%.1f,\"pulse_count\":%lu,"
        "\"last_duration_us\":%lu,\"pulse_timings_us\":[",
        c->event_id,
        c->device_id,
        c->session_id,
        (unsigned long)c->sequence,
        when,
        (unsigned long)utc,
        (int)c->tz_offset_minutes,
        (unsigned long)c->rtc_local_unix,
        (unsigned long)c->monotonic_ms,
        record->mode,
        (unsigned long)record->frequency_hz,
        (unsigned long)record->duration_us,
        (unsigned long)c->battery_pct,
        (unsigned long)record->fingerprint,
        profile,
        (double)similarity,
        (double)record->rssi_min_dbm,
        (double)record->rssi_avg_dbm,
        (double)record->rssi_max_dbm,
        (unsigned long)record->pulse_count,
        (unsigned long)record->last_duration_us);
    const size_t suffix_length = sizeof(rf_record_suffix) - 1U;
    if(head <= 0 || (size_t)head + suffix_length >= capacity) return 0;
    size_t used = (size_t)head;
    uint32_t written = 0;
    for(uint32_t i = 0; record->timings && i < record->timing_count; i++) {
        char text[12];
        size_t n = 0;
        if(written) text[n++] = ',';
        n += rf_u32_text(rf_timing_duration(record->timings[i]), text + n);
        /* Keep room for the closing suffix and the terminator. */
        if(used + n + suffix_length >= capacity) break;
        memcpy(out + used, text, n);
        used += n;
        written++;
    }
    memcpy(out + used, rf_record_suffix, suffix_length);
    used += suffix_length;
    out[used] = '\0';
    if(timings_written) *timings_written = written;
    return used;
}

size_t rf_record_nfc(char* out, size_t capacity, const RfNfcRecord* record) {
    if(!out || !record || !capacity) return 0;
    const RfRecordCommon* c = &record->common;
    uint32_t utc = rf_record_utc_unix(c->rtc_local_unix, c->tz_offset_minutes);
    char when[24];
    rf_record_format_utc(utc, when, sizeof(when));
    uint64_t duration_us = (uint64_t)record->duration_ms * 1000U;
    if(duration_us > UINT32_MAX) duration_us = UINT32_MAX;
    int length = snprintf(
        out,
        capacity,
        "{\"schema_version\":1,\"event_id\":\"%s\",\"device_uuid\":\"%s\",\"session_id\":\"%s\","
        "\"sequence_number\":%lu,\"captured_at_utc\":\"%s\",\"captured_at_unix\":%lu,"
        "\"timezone_offset_minutes\":%d,\"rtc_local_unix\":%lu,\"monotonic_ms\":%lu,"
        "\"source_type\":\"nfc\",\"mode\":\"NFC\",\"frequency_hz\":13560000,\"modulation\":\"NFC\","
        "\"bandwidth_hz\":0,\"duration_us\":%lu,\"repeat_count\":1,\"battery_pct\":%lu,"
        "\"fingerprint_id\":\"local-nfc-field\",\"family_id\":null,\"classification\":\"unknown\","
        "\"classification_confidence\":0.0,\"nfc_technology\":\"external-field\","
        "\"nfc_protocol\":\"carrier-presence\",\"nfc_identifier\":\"\","
        "\"nfc_field_duration_ms\":%lu,\"nfc_field_count\":%lu,\"nfc_confidence\":0.50,"
        "\"upload_state\":\"pending\"}\n",
        c->event_id,
        c->device_id,
        c->session_id,
        (unsigned long)c->sequence,
        when,
        (unsigned long)utc,
        (int)c->tz_offset_minutes,
        (unsigned long)c->rtc_local_unix,
        (unsigned long)c->monotonic_ms,
        (unsigned long)duration_us,
        (unsigned long)c->battery_pct,
        (unsigned long)record->duration_ms,
        (unsigned long)record->field_count);
    if(length <= 0 || (size_t)length >= capacity) return 0;
    return (size_t)length;
}

void rf_shape_compute(RfShape* shape, const uint32_t* timings, uint32_t count) {
    memset(shape, 0, sizeof(*shape));
    for(uint32_t i = 0; timings && i < count; i++) {
        uint32_t scaled = rf_timing_duration(timings[i]) / 32U;
        uint32_t bin = 0;
        while(scaled > 1U && bin < RF_SHAPE_BINS - 1U) {
            scaled >>= 1;
            bin++;
        }
        if(shape->bins[bin] < UINT16_MAX) shape->bins[bin]++;
    }
    shape->count = count;
}

static uint32_t rf_fnv(uint32_t hash, uint32_t value) {
    for(uint8_t i = 0; i < 4; i++) {
        hash ^= (uint8_t)(value >> (i * 8U));
        hash *= 16777619U;
    }
    return hash;
}

/* Coarse structure key: carrier + which duration bands dominate.  Repeated
 * frames of one remote land in the same key although their payload bits and
 * exact pulse count differ; the desktop does the authoritative grouping. */
uint32_t rf_shape_fingerprint(const RfShape* shape, uint32_t frequency_hz) {
    uint32_t total = 0;
    for(uint32_t i = 0; i < RF_SHAPE_BINS; i++) total += shape->bins[i];
    uint32_t mask = 0, dominant = 0xFFU;
    uint16_t best = 0;
    for(uint32_t i = 0; i < RF_SHAPE_BINS; i++) {
        if(total && (uint32_t)shape->bins[i] * 100U >= total * 15U) mask |= 1UL << i;
        if(shape->bins[i] > best) {
            best = shape->bins[i];
            dominant = i;
        }
    }
    uint32_t hash = 2166136261U;
    hash = rf_fnv(hash, frequency_hz);
    hash = rf_fnv(hash, mask);
    hash = rf_fnv(hash, dominant);
    return hash;
}

float rf_shape_similarity(const RfShape* a, const RfShape* b) {
    uint32_t ta = 0, tb = 0;
    for(uint32_t i = 0; i < RF_SHAPE_BINS; i++) {
        ta += a->bins[i];
        tb += b->bins[i];
    }
    if(!ta || !tb) return ta == tb ? 1.0f : 0.0f;
    float overlap = 0.0f;
    for(uint32_t i = 0; i < RF_SHAPE_BINS; i++) {
        float pa = (float)a->bins[i] / (float)ta;
        float pb = (float)b->bins[i] / (float)tb;
        overlap += pa < pb ? pa : pb;
    }
    float ratio = ta < tb ? (float)ta / (float)tb : (float)tb / (float)ta;
    float score = 0.8f * overlap + 0.2f * ratio;
    if(score < 0.0f) score = 0.0f;
    if(score > 1.0f) score = 1.0f;
    return score;
}
