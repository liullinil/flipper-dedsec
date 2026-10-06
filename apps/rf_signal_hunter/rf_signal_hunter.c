/* RF Signal Hunter: passive Scout/Capture/Follow/NFC modes.
 *
 * This FAP deliberately uses only the public RX Sub-GHz API available in SDK
 * 88.9.  NFC uses the HAL's field-detector only: it observes an external
 * 13.56 MHz carrier and never starts a poller, listener, or carrier output.
 * There are no TX, replay, or emulation calls in this application.
 */
#include <furi.h>
#include <furi_hal_subghz.h>
#include <gui/gui.h>
#include <input/input.h>
#include <notification/notification_messages.h>
#include <storage/storage.h>
#include <furi_hal_rtc.h>
#include <datetime/datetime.h>
#include <furi_hal_random.h>
#include <furi_hal_nfc.h>
#include <furi_hal_bt.h>
#include <bt/bt_service/bt.h>
#include "rf_hunter_ble.h"
#include "rf_store.h"
#include "rf_capture.h"
#include "rf_settings.h"

#define HUNTER_EVENTS_DIR APP_DATA_PATH("rf_signal_hunter")
#define HUNTER_EVENTS_PATH HUNTER_EVENTS_DIR "/events.jsonl"
#define HUNTER_DEVICE_ID_PATH HUNTER_EVENTS_DIR "/device_id"
#define HUNTER_FREQUENCY_HZ 433920000U
#define HUNTER_PULSE_RING_SIZE 16U
static const uint32_t hunter_frequencies[] = {315000000U, 433920000U, 868350000U};


typedef enum {
    HunterModeScout,
    HunterModeCapture,
    HunterModeFollow,
    HunterModeNfc,
    HunterModeCount,
} HunterMode;

typedef struct {
    Gui* gui;
    ViewPort* viewport;
    NotificationApp* notifications;
    Storage* storage;
    File* events_file;
    RfStore* store;
    RfHunterSettings settings;
    volatile uint32_t pulses;
    volatile uint32_t bursts;
    volatile uint32_t last_duration;
    uint32_t notified_bursts;
    uint32_t sequence;
    uint32_t session_start_tick;
    uint32_t follow_duration;
    RfCaptureEngine capture;
    RfCaptureTiming* event_timings;
    uint16_t event_timing_count;
    char session_id[16];
    char device_id[17];
    HunterMode mode;
    bool running;
    bool receiver_active;
    bool nfc_detect_active;
    bool nfc_hal_acquired;
    bool nfc_field_present;
    uint32_t nfc_field_started_tick;
    uint32_t nfc_field_count;
    DateTime nfc_field_started_rtc;
    bool storage_full;
    bool settings_open;
    uint8_t setting_index;
    uint8_t frequency_index;
    uint32_t events_reviewed;
    uint32_t families_seen;
    float rssi_min;
    float rssi_max;
    float rssi_sum;
    uint32_t rssi_samples;
    Bt* bt;
    FuriHalBleProfileBase* ble_profile;
    RfHunterProfileParams ble_params;
    FuriStreamBuffer* ble_rx;
    char ble_line[1024];
    uint16_t ble_line_len;
} Hunter;

static bool hunter_frequency_allowed(const Hunter* hunter, uint8_t index) {
    if(hunter->settings.band_profile == 0) return true;
    if(hunter->settings.band_profile == 1) return index == 1;
    if(hunter->settings.band_profile == 2) return index == 0;
    if(hunter->settings.band_profile == 3) return index == 2;
    return true;
}

static void hunter_hex_random(char* out, size_t chars) {
    uint8_t bytes[8] = {0};
    furi_hal_random_fill_buf(bytes, sizeof(bytes));
    static const char hex[] = "0123456789abcdef";
    for(size_t i = 0; i < chars; i++) out[i] = hex[(bytes[i / 2] >> ((i & 1) ? 0 : 4)) & 0xF];
    out[chars] = '\0';
}

static void hunter_load_identity(Hunter* hunter) {
    File* id_file = storage_file_alloc(hunter->storage);
    bool loaded = false;
    if(storage_file_open(id_file, HUNTER_DEVICE_ID_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        loaded = storage_file_read(id_file, hunter->device_id, sizeof(hunter->device_id) - 1) == sizeof(hunter->device_id) - 1;
        storage_file_close(id_file);
    }
    storage_file_free(id_file);
    if(!loaded) {
        hunter_hex_random(hunter->device_id, 16);
        id_file = storage_file_alloc(hunter->storage);
        if(storage_file_open(id_file, HUNTER_DEVICE_ID_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
            storage_file_write(id_file, hunter->device_id, 16);
            storage_file_sync(id_file);
            storage_file_close(id_file);
        }
        storage_file_free(id_file);
    } else {
        hunter->device_id[16] = '\0';
    }
}

static const char* hunter_mode_name(HunterMode mode) {
    static const char* names[HunterModeCount] = {"SCOUT", "CAPTURE", "FOLLOW", "NFC"};
    return names[mode < HunterModeCount ? mode : HunterModeScout];
}

static bool hunter_event_selected(const Hunter* hunter) {
    if(hunter->mode == HunterModeScout || hunter->mode == HunterModeCapture) return true;
    if(hunter->mode == HunterModeFollow) {
        uint32_t d = hunter->last_duration;
        uint32_t target = hunter->follow_duration;
        return target == 0 || (d > (target > 500 ? target - 500 : 0) && d < target + 500);
    }
    return false;
}

/* The HAL field detector only samples the external carrier detector.  Keep
 * the NFC lock for the lifetime of the detector so no other service can
 * reconfigure the chip while we are reading its status bit.  No poller,
 * listener, field-on, TX, or RX operation is used here. */
static bool hunter_nfc_start(Hunter* hunter) {
    if(hunter->nfc_detect_active) return true;
    if(!hunter->nfc_hal_acquired) {
        if(furi_hal_nfc_acquire() != FuriHalNfcErrorNone) return false;
        hunter->nfc_hal_acquired = true;
        if(furi_hal_nfc_low_power_mode_stop() != FuriHalNfcErrorNone) {
            furi_hal_nfc_release();
            hunter->nfc_hal_acquired = false;
            return false;
        }
    }
    if(furi_hal_nfc_field_detect_start() != FuriHalNfcErrorNone) {
        furi_hal_nfc_low_power_mode_start();
        furi_hal_nfc_release();
        hunter->nfc_hal_acquired = false;
        return false;
    }
    hunter->nfc_detect_active = true;
    hunter->nfc_field_present = false;
    return true;
}

static void hunter_record_nfc(
    Hunter* hunter,
    const DateTime* captured,
    uint32_t start_tick,
    uint32_t duration_ms) {
    if(!captured) return;
    DateTime local = *captured;
    uint32_t rtc_epoch = datetime_datetime_to_timestamp(&local);
    int64_t utc_epoch = (int64_t)rtc_epoch - (int64_t)hunter->settings.timezone_offset_minutes * 60;
    if(utc_epoch < 0) utc_epoch = 0;
    DateTime utc;
    datetime_timestamp_to_datetime((uint32_t)utc_epoch, &utc);
    hunter->sequence++;
    char line[640];
    int written = snprintf(
        line,
        sizeof(line),
        "{\"event_id\":\"rf-%s-%s-%lu\",\"device_id\":\"%s\",\"session_id\":\"%s\",\"sequence_number\":%lu,"
        "\"captured_at_utc\":\"%04u-%02u-%02uT%02u:%02u:%02uZ\",\"captured_at_unix\":%lu,\"timezone_offset_minutes\":%d,\"rtc_local_unix\":%lu,\"monotonic_ms\":%lu,"
        "\"source_type\":\"nfc\",\"mode\":\"NFC\",\"frequency_hz\":13560000,\"modulation\":\"NFC\","
        "\"nfc_technology\":\"external-field\",\"nfc_protocol\":\"carrier-presence\",\"nfc_identifier\":\"\","
        "\"nfc_field_duration_ms\":%lu,\"nfc_field_count\":%lu,\"nfc_confidence\":0.50,\"upload_state\":\"pending\"}\n",
        hunter->device_id,
        hunter->session_id,
        (unsigned long)hunter->sequence,
        hunter->device_id,
        hunter->session_id,
        (unsigned long)hunter->sequence,
        utc.year,
        utc.month,
        utc.day,
        utc.hour,
        utc.minute,
        utc.second,
        (unsigned long)utc_epoch,
        hunter->settings.timezone_offset_minutes,
        (unsigned long)rtc_epoch,
        (unsigned long)(start_tick - hunter->session_start_tick),
        (unsigned long)duration_ms,
        (unsigned long)hunter->nfc_field_count);
    if(written <= 0 || (size_t)written >= sizeof(line)) return;
    char event_id[80];
    snprintf(
        event_id,
        sizeof(event_id),
        "rf-%s-%s-%lu",
        hunter->device_id,
        hunter->session_id,
        (unsigned long)hunter->sequence);
    hunter->storage_full = !rf_store_save(hunter->store, event_id, line, (size_t)written);
    if(hunter->storage_full) {
        notification_message(hunter->notifications, &sequence_set_only_red_255);
        return;
    }
    if(hunter->events_file && storage_file_is_open(hunter->events_file)) {
        storage_file_write(hunter->events_file, line, (size_t)written);
        storage_file_sync(hunter->events_file);
    }
    hunter->bursts++;
    hunter->families_seen++;
    notification_message(hunter->notifications, &sequence_audiovisual_alert);
    notification_message(hunter->notifications, &sequence_set_only_green_255);
}

static void hunter_nfc_finish_event(Hunter* hunter) {
    if(!hunter->nfc_field_present) return;
    uint32_t now = furi_get_tick();
    hunter_record_nfc(
        hunter,
        &hunter->nfc_field_started_rtc,
        hunter->nfc_field_started_tick,
        now - hunter->nfc_field_started_tick);
    hunter->nfc_field_present = false;
}

static void hunter_nfc_stop(Hunter* hunter) {
    if(hunter->nfc_detect_active) {
        /* Finalize a field that is still present when the user leaves NFC
         * mode, preserving its start timestamp and observed duration. */
        hunter_nfc_finish_event(hunter);
        furi_hal_nfc_field_detect_stop();
        hunter->nfc_detect_active = false;
    }
    if(hunter->nfc_hal_acquired) {
        furi_hal_nfc_low_power_mode_start();
        furi_hal_nfc_release();
        hunter->nfc_hal_acquired = false;
    }
}

static void hunter_process_nfc(Hunter* hunter) {
    if(!hunter->nfc_detect_active) return;
    bool present = furi_hal_nfc_field_is_present();
    if(present && !hunter->nfc_field_present) {
        hunter->nfc_field_present = true;
        hunter->nfc_field_started_tick = furi_get_tick();
        furi_hal_rtc_get_datetime(&hunter->nfc_field_started_rtc);
        hunter->nfc_field_count++;
        /* Feedback is intentionally emitted at field-on, before persistence
         * waits for field-off and its final duration. */
        notification_message(hunter->notifications, &sequence_audiovisual_alert);
        notification_message(hunter->notifications, &sequence_set_only_green_255);
    } else if(!present && hunter->nfc_field_present) {
        hunter_nfc_finish_event(hunter);
    }
}

static void hunter_record(Hunter* hunter) {
    /* The per-event store is the durable BLE source of truth.  The legacy
     * JSONL stream is only a convenience export; a failure to open it must
     * never suppress an otherwise valid pending event. */
    if(!hunter->store) return;
    DateTime local;
    furi_hal_rtc_get_datetime(&local);
    /* RTC calendar fields are local time; apply the persisted offset before
       labelling the instant as UTC.  Keep the raw RTC epoch for audit. */
    uint32_t rtc_epoch = datetime_datetime_to_timestamp(&local);
    int64_t utc_epoch = (int64_t)rtc_epoch - (int64_t)hunter->settings.timezone_offset_minutes * 60;
    if(utc_epoch < 0) utc_epoch = 0;
    DateTime now;
    datetime_timestamp_to_datetime((uint32_t)utc_epoch, &now);
    hunter->sequence++;
    char line[640];
    snprintf(
        line,
        sizeof(line),
        "{\"event_id\":\"rf-%s-%s-%lu\",\"device_id\":\"%s\",\"session_id\":\"%s\",\"sequence_number\":%lu,"
        "\"captured_at_utc\":\"%04u-%02u-%02uT%02u:%02u:%02uZ\",\"captured_at_unix\":%lu,\"timezone_offset_minutes\":%d,\"rtc_local_unix\":%lu,\"monotonic_ms\":%lu,"
        "\"source_type\":\"subghz\",\"mode\":\"%s\",\"frequency_hz\":%lu,\"rssi_min_dbm\":%.1f,\"rssi_avg_dbm\":%.1f,\"rssi_max_dbm\":%.1f,"
        "\"pulse_count\":%lu,\"last_duration_us\":%lu,\"pulse_timings_us\":[",
        hunter->device_id,
        hunter->session_id,
        (unsigned long)hunter->sequence,
        hunter->device_id,
        hunter->session_id,
        (unsigned long)hunter->sequence,
        now.year,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
        (unsigned long)utc_epoch,
        hunter->settings.timezone_offset_minutes,
        (unsigned long)rtc_epoch,
        (unsigned long)(furi_get_tick() - hunter->session_start_tick),
        hunter_mode_name(hunter->mode),
        (unsigned long)hunter_frequencies[hunter->frequency_index],
        (double)(hunter->rssi_samples ? hunter->rssi_min : 0.0f),
        (double)(hunter->rssi_samples ? hunter->rssi_sum / hunter->rssi_samples : 0.0f),
        (double)(hunter->rssi_samples ? hunter->rssi_max : 0.0f),
        (unsigned long)hunter->pulses,
        (unsigned long)hunter->last_duration);
    size_t used = strlen(line);
    /* Leave room for the closing array and upload marker.  A capture may
     * contain more timings than fit in the compact journal record; retaining
     * a valid prefix is safer than writing a truncated JSON document that can
     * never be imported or acknowledged. */
    static const char suffix[] = "],\"upload_state\":\"pending\"}\n";
    const size_t suffix_len = sizeof(suffix) - 1;
    uint16_t count = hunter->event_timing_count;
    for(uint16_t i = 0; i < count; i++) {
        char timing[24];
        int written = snprintf(
            timing,
            sizeof(timing),
            "%s%lu",
            i ? "," : "",
            (unsigned long)hunter->event_timings[i].duration_us);
        if(written <= 0 || (size_t)written >= sizeof(timing) || used + (size_t)written + suffix_len >= sizeof(line)) break;
        memcpy(line + used, timing, (size_t)written);
        used += (size_t)written;
    }
    if(used + suffix_len >= sizeof(line)) return;
    memcpy(line + used, suffix, suffix_len);
    used += suffix_len;
    line[used] = 0;
    char event_id[80];
    snprintf(event_id, sizeof(event_id), "rf-%s-%s-%lu", hunter->device_id, hunter->session_id, (unsigned long)hunter->sequence);
    hunter->storage_full = !rf_store_save(hunter->store, event_id, line, used);
    if(hunter->storage_full) {
        notification_message(hunter->notifications, &sequence_set_only_red_255);
        if(hunter->settings.retention_policy == RfRetentionStopWhenFull && hunter->receiver_active) {
            furi_hal_subghz_stop_async_rx();
            furi_hal_subghz_idle();
            hunter->receiver_active = false;
        }
        return;
    }
    if(hunter->events_file && storage_file_is_open(hunter->events_file)) {
        storage_file_write(hunter->events_file, line, used);
        storage_file_sync(hunter->events_file);
    }
}

static void hunter_capture(bool level, uint32_t duration, void* context) {
    Hunter* hunter = context;
    rf_capture_isr(&hunter->capture, level, duration);
}

static void hunter_process_capture(Hunter* hunter) {
    RfCaptureTiming timing;
    while(rf_capture_pop(&hunter->capture, &timing)) {
        hunter->pulses++;
        hunter->last_duration = timing.duration_us;
        if(hunter->event_timing_count < RF_CAPTURE_MAX_TIMINGS) {
            hunter->event_timings[hunter->event_timing_count++] = timing;
        }
        if(timing.duration_us > hunter->settings.silence_us && hunter->event_timing_count > 1) {
            hunter->bursts++;
            float avg_rssi = hunter->rssi_samples ? hunter->rssi_sum / hunter->rssi_samples : -120.0f;
            if(hunter_event_selected(hunter) && avg_rssi >= hunter->settings.rssi_threshold_dbm) {
                notification_message(hunter->notifications, &sequence_audiovisual_alert);
                notification_message(hunter->notifications, &sequence_set_only_green_255);
                hunter_record(hunter);
                hunter->families_seen++;
                if(hunter->mode == HunterModeFollow && hunter->follow_duration == 0) {
                    hunter->follow_duration = hunter->last_duration;
                }
            }
            hunter->event_timing_count = 0;
            hunter->rssi_min = hunter->rssi_max = hunter->rssi_sum = 0.0f;
            hunter->rssi_samples = 0;
        }
    }
}

static void hunter_ble_rx(const uint8_t* data, uint16_t size, void* context) {
    Hunter* hunter = context;
    if(hunter->ble_rx) furi_stream_buffer_send(hunter->ble_rx, data, size, 0);
}

static void hunter_ble_send(Hunter* hunter, const char* text) {
    if(hunter->ble_profile && text) {
        rfhunter_ble_tx(hunter->ble_profile, (const uint8_t*)text, MIN(strlen(text), (size_t)RFHUNTER_TX_MAX));
    }
}

static bool hunter_extract_string(const char* line, const char* key, char* out, size_t out_size) {
    char needle[48];
    snprintf(needle, sizeof(needle), "\"%s\":\"", key);
    const char* p = strstr(line, needle);
    if(!p) return false;
    p += strlen(needle);
    const char* end = strchr(p, '\"');
    if(!end) return false;
    size_t n = MIN((size_t)(end - p), out_size - 1);
    memcpy(out, p, n);
    out[n] = 0;
    return true;
}

static uint32_t hunter_json_number(const char* line, const char* key) {
    char needle[32];
    snprintf(needle, sizeof(needle), "\"%s\":", key);
    const char* p = strstr(line, needle);
    return p ? strtoul(p + strlen(needle), NULL, 10) : 0;
}

static uint32_t hunter_crc32(const char* data, size_t len) {
    uint32_t crc = 0xffffffffu;
    for(size_t i = 0; i < len; i++) {
        crc ^= (uint8_t)data[i];
        for(uint8_t bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ (0xedb88320u & (-(int32_t)(crc & 1)));
    }
    return ~crc;
}

static void hunter_send_json_chunk(
    Hunter* hunter,
    uint32_t rid,
    const char* event_id,
    const char* record,
    size_t length,
    size_t offset) {
    static const char hex[] = "0123456789abcdef";
    size_t count = MIN((size_t)80, length > offset ? length - offset : 0);
    char frame[243];
    size_t used = snprintf(
        frame,
        sizeof(frame),
        "{\"v\":1,\"rid\":%lu,\"op\":\"chunk\",\"event_id\":\"%s\",\"offset\":%lu,\"next\":%lu,\"hex\":\"",
        (unsigned long)rid,
        event_id,
        (unsigned long)offset,
        (unsigned long)(offset + count));
    for(size_t i = 0; i < count && used + 2 < sizeof(frame); i++) {
        uint8_t byte = (uint8_t)record[offset + i];
        frame[used++] = hex[byte >> 4];
        frame[used++] = hex[byte & 0xf];
    }
    if(used + 3 < sizeof(frame)) {
        frame[used++] = '"';
        frame[used++] = '}';
        frame[used++] = '\n';
        frame[used] = 0;
        hunter_ble_send(hunter, frame);
    }
}

static void hunter_ble_handle(Hunter* hunter, const char* line) {
    uint32_t rid = hunter_json_number(line, "rid");
    if(strstr(line, "\"op\":\"hello\"")) {
        char reply[180];
        snprintf(reply, sizeof(reply), "{\"v\":1,\"rid\":%lu,\"op\":\"hello_ack\",\"device_uuid\":\"%s\",\"pending\":%lu}\n", (unsigned long)rid, hunter->device_id, (unsigned long)rf_store_pending_count(hunter->store));
        hunter_ble_send(hunter, reply);
    } else if(strstr(line, "\"op\":\"list\"")) {
        char reply[220];
        File* dir = storage_file_alloc(hunter->storage);
        char name[96];
        FileInfo info;
        uint32_t cursor = hunter_json_number(line, "cursor");
        uint32_t index = 0;
        if(storage_dir_open(dir, RF_STORE_EVENTS_DIR)) {
            while(storage_dir_read(dir, &info, name, sizeof(name))) {
                size_t n = strlen(name);
                if(n <= 5 || strcmp(name + n - 5, ".json")) continue;
                if(index++ < cursor) continue;
                name[n - 5] = 0;
                /* ACK receipts are durable tombstones.  Retention policy may
                 * keep the JSON file for local review, but it must no longer
                 * appear in the pending manifest. */
                if(rf_store_is_acked(hunter->store, name)) continue;
                char record[640]; size_t length = 0;
                if(rf_store_read(hunter->store, name, record, sizeof(record), &length)) {
                    char item[220];
                    snprintf(item, sizeof(item), "{\"v\":1,\"rid\":%lu,\"op\":\"item\",\"event_id\":\"%s\",\"size\":%lu,\"crc32\":%lu,\"next\":%lu}\n", (unsigned long)rid, name, (unsigned long)length, (unsigned long)hunter_crc32(record, length), (unsigned long)index);
                    hunter_ble_send(hunter, item);
                }
            }
            storage_dir_close(dir);
        }
        storage_file_free(dir);
        snprintf(reply, sizeof(reply), "{\"v\":1,\"rid\":%lu,\"op\":\"end\"}\n", (unsigned long)rid);
        hunter_ble_send(hunter, reply);
    } else if(strstr(line, "\"op\":\"read\"")) {
        char id[80];
        if(hunter_extract_string(line, "event_id", id, sizeof(id))) {
            char record[640]; size_t length = 0;
            if(rf_store_read(hunter->store, id, record, sizeof(record), &length)) {
                uint32_t offset = hunter_json_number(line, "offset");
                if(offset < length) {
                    hunter_send_json_chunk(hunter, rid, id, record, length, offset);
                } else {
                    char error[180];
                    snprintf(error, sizeof(error), "{\"v\":1,\"rid\":%lu,\"op\":\"error\",\"error\":\"offset out of range\"}\n", (unsigned long)rid);
                    hunter_ble_send(hunter, error);
                }
            } else {
                char error[180];
                snprintf(error, sizeof(error), "{\"v\":1,\"rid\":%lu,\"op\":\"error\",\"error\":\"event not found\"}\n", (unsigned long)rid);
                hunter_ble_send(hunter, error);
            }
        } else {
            char error[180];
            snprintf(error, sizeof(error), "{\"v\":1,\"rid\":%lu,\"op\":\"error\",\"error\":\"event_id required\"}\n", (unsigned long)rid);
            hunter_ble_send(hunter, error);
        }
    } else if(strstr(line, "\"op\":\"ack\"")) {
        char id[80] = {0};
        char record[640];
        size_t length = 0;
        bool valid = hunter_extract_string(line, "event_id", id, sizeof(id)) &&
                     rf_store_read(hunter->store, id, record, sizeof(record), &length);
        uint32_t expected_size = hunter_json_number(line, "size");
        uint32_t expected_crc = hunter_json_number(line, "crc32");
        valid = valid && expected_size == length && expected_crc == hunter_crc32(record, length);
        if(valid) valid = rf_store_ack(hunter->store, id);
        if(valid) {
            char reply[180];
            snprintf(reply, sizeof(reply), "{\"v\":1,\"rid\":%lu,\"op\":\"acked\",\"event_id\":\"%s\"}\n", (unsigned long)rid, id);
            hunter_ble_send(hunter, reply);
        } else {
            char error[180];
            snprintf(error, sizeof(error), "{\"v\":1,\"rid\":%lu,\"op\":\"error\",\"error\":\"ack validation failed\"}\n", (unsigned long)rid);
            hunter_ble_send(hunter, error);
        }
    }
}

static void hunter_ble_drain(Hunter* hunter) {
    uint8_t data[128];
    size_t got;
    while(hunter->ble_rx && (got = furi_stream_buffer_receive(hunter->ble_rx, data, sizeof(data), 0)) > 0) {
        for(size_t i = 0; i < got; i++) {
            char ch = (char)data[i];
            if(ch == '\n' || ch == '\r') {
                if(hunter->ble_line_len) {
                    hunter->ble_line[hunter->ble_line_len] = 0;
                    hunter_ble_handle(hunter, hunter->ble_line);
                    hunter->ble_line_len = 0;
                }
            } else if(hunter->ble_line_len < sizeof(hunter->ble_line) - 1) {
                hunter->ble_line[hunter->ble_line_len++] = ch;
            }
        }
    }
}

static void hunter_draw(Canvas* canvas, void* context) {
    Hunter* hunter = context;
    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 2, "RF SIGNAL HUNTER");
    canvas_set_font(canvas, FontSecondary);
    char line[40];
    if(hunter->settings_open) {
        canvas_draw_str(canvas, 2, 16, "TIME / STORAGE SETTINGS");
        int16_t offset = hunter->settings.timezone_offset_minutes;
        snprintf(line, sizeof(line), "%c RTC UTC%c%02u:%02u", hunter->setting_index == 0 ? '>' : ' ', offset < 0 ? '-' : '+', (unsigned)(abs(offset) / 60), (unsigned)(abs(offset) % 60));
        canvas_draw_str(canvas, 2, 29, line);
        static const char* policies[] = {"ACK -> compact", "Keep ACK index", "Stop when full"};
        snprintf(line, sizeof(line), "%c %s", hunter->setting_index == 1 ? '>' : ' ', policies[hunter->settings.retention_policy]);
        canvas_draw_str(canvas, 2, 40, line);
        snprintf(line, sizeof(line), "%c Reserve: %lu KB", hunter->setting_index == 2 ? '>' : ' ', (unsigned long)(hunter->settings.min_free_bytes / 1024));
        canvas_draw_str(canvas, 2, 51, line);
        canvas_draw_str(canvas, 2, 62, "Up/Dn row L/R edit Back save");
        return;
    }
    snprintf(line, sizeof(line), "%s  %s", hunter_mode_name(hunter->mode), hunter->running ? "RUN" : "STOP");
    canvas_draw_str(canvas, 2, 16, line);
    if(hunter->mode == HunterModeNfc) {
        canvas_draw_str(canvas, 2, 29, hunter->nfc_detect_active ? "NFC field detector" : "NFC detector stopped");
        canvas_draw_str(canvas, 2, 40, hunter->nfc_field_present ? "External field: present" : "External field: absent");
        snprintf(line, sizeof(line), "Fields: %lu  no poller TX", (unsigned long)hunter->nfc_field_count);
        canvas_draw_str(canvas, 2, 51, line);
    } else {
        snprintf(line, sizeof(line), "Events: %lu  New fam: %lu", (unsigned long)(hunter->bursts - hunter->events_reviewed), (unsigned long)hunter->families_seen);
        canvas_draw_str(canvas, 2, 29, line);
        snprintf(line, sizeof(line), "Pending: %lu  Free: %luM", (unsigned long)rf_store_pending_count(hunter->store), (unsigned long)(rf_store_free_bytes(hunter->store) / (1024 * 1024)));
        canvas_draw_str(canvas, 2, 40, line);
        if(hunter->storage_full) snprintf(line, sizeof(line), "%s - import now", rf_store_error_text(rf_store_last_error(hunter->store)));
        else snprintf(line, sizeof(line), "Last: %lu us RSSI %.0f", (unsigned long)hunter->last_duration, (double)(hunter->rssi_samples ? hunter->rssi_sum / hunter->rssi_samples : 0.0f));
        canvas_draw_str(canvas, 2, 51, line);
    }
    canvas_draw_str(canvas, 2, 62, "L/R mode OK run Up settings");
}

static void hunter_input(InputEvent* event, void* context) {
    Hunter* hunter = context;
    if(event->type != InputTypeShort) return;
    if(hunter->settings_open) {
        if(event->key == InputKeyBack || event->key == InputKeyOk) {
            if(rf_settings_save(hunter->storage, &hunter->settings)) {
                rf_store_configure(hunter->store, hunter->settings.retention_policy, hunter->settings.min_free_bytes);
                hunter->settings_open = false;
            } else {
                notification_message(hunter->notifications, &sequence_set_only_red_255);
            }
        } else if(event->key == InputKeyUp) hunter->setting_index = (hunter->setting_index + 2) % 3;
        else if(event->key == InputKeyDown) hunter->setting_index = (hunter->setting_index + 1) % 3;
        else if(event->key == InputKeyLeft || event->key == InputKeyRight) {
            int delta = event->key == InputKeyRight ? 1 : -1;
            if(hunter->setting_index == 0) {
                int value = hunter->settings.timezone_offset_minutes + delta * 15;
                hunter->settings.timezone_offset_minutes = CLAMP(value, 14 * 60, -14 * 60);
            } else if(hunter->setting_index == 1) {
                hunter->settings.retention_policy = (hunter->settings.retention_policy + delta + 3) % 3;
            } else {
                int value = (int)hunter->settings.min_free_bytes + delta * 16384;
                hunter->settings.min_free_bytes = CLAMP(value, 4 * 1024 * 1024, 16384);
            }
        }
        view_port_update(hunter->viewport);
        return;
    }
    if(event->key == InputKeyUp) {
        hunter->settings_open = true;
        view_port_update(hunter->viewport);
        return;
    }
    if(event->key == InputKeyBack) {
        hunter->running = false;
        view_port_enabled_set(hunter->viewport, false);
    } else if(event->key == InputKeyLeft || event->key == InputKeyRight) {
        int delta = event->key == InputKeyRight ? 1 : -1;
        int next = (int)hunter->mode + delta;
        if(next < 0) next = HunterModeCount - 1;
        if(next >= HunterModeCount) next = 0;
        HunterMode previous_mode = hunter->mode;
        hunter->mode = (HunterMode)next;
        if(hunter->mode == HunterModeNfc) {
            /* Never leave Sub-GHz RX running while taking the NFC HAL lock. */
            if(hunter->receiver_active) {
                furi_hal_subghz_stop_async_rx();
                furi_hal_subghz_idle();
                hunter->receiver_active = false;
            }
            if(!hunter_nfc_start(hunter)) hunter->mode = previous_mode;
        } else if(previous_mode == HunterModeNfc) {
            hunter_nfc_stop(hunter);
        }
        view_port_update(hunter->viewport);
    } else if(event->key == InputKeyOk) {
        if(hunter->mode == HunterModeScout) hunter->events_reviewed = hunter->bursts;
        if(hunter->mode == HunterModeNfc) {
            if(hunter->nfc_detect_active) hunter_nfc_stop(hunter);
            else hunter_nfc_start(hunter);
            view_port_update(hunter->viewport);
            return;
        }
        hunter->receiver_active = !hunter->receiver_active;
        if(hunter->receiver_active) {
            furi_hal_subghz_set_frequency(hunter_frequencies[hunter->frequency_index]);
            furi_hal_subghz_rx();
            furi_hal_subghz_start_async_rx(hunter_capture, hunter);
            if(hunter->mode == HunterModeFollow && hunter->follow_duration == 0) {
                hunter->follow_duration = hunter->last_duration;
            }
        } else {
            furi_hal_subghz_stop_async_rx();
            furi_hal_subghz_idle();
        }
        view_port_update(hunter->viewport);
    }
}

int32_t rf_signal_hunter_app(void* context) {
    UNUSED(context);
    Hunter hunter = {0};
    hunter.gui = furi_record_open(RECORD_GUI);
    hunter.notifications = furi_record_open(RECORD_NOTIFICATION);
    hunter.bt = furi_record_open(RECORD_BT);
    hunter.ble_rx = furi_stream_buffer_alloc(4096, 1);
    hunter.storage = furi_record_open(RECORD_STORAGE);
    hunter.store = rf_store_alloc(hunter.storage);
    rf_settings_load(hunter.storage, &hunter.settings);
    rf_store_configure(hunter.store, hunter.settings.retention_policy, hunter.settings.min_free_bytes);
    hunter.session_start_tick = furi_get_tick();
    hunter.event_timings = malloc(sizeof(RfCaptureTiming) * RF_CAPTURE_MAX_TIMINGS);
    rf_capture_init(&hunter.capture);
    storage_common_mkdir(hunter.storage, HUNTER_EVENTS_DIR);
    hunter_load_identity(&hunter);
    snprintf(hunter.session_id, sizeof(hunter.session_id), "s");
    hunter_hex_random(hunter.session_id + 1, sizeof(hunter.session_id) - 2);
    hunter.events_file = storage_file_alloc(hunter.storage);
    if(!storage_file_open(hunter.events_file, HUNTER_EVENTS_PATH, FSAM_WRITE, FSOM_OPEN_APPEND)) {
        storage_file_free(hunter.events_file);
        hunter.events_file = NULL;
    }
    hunter.viewport = view_port_alloc();
    view_port_draw_callback_set(hunter.viewport, hunter_draw, &hunter);
    view_port_input_callback_set(hunter.viewport, hunter_input, &hunter);
    gui_add_view_port(hunter.gui, hunter.viewport, GuiLayerFullscreen);
    hunter.running = true;
    hunter.mode = HunterModeScout;
    bt_disconnect(hunter.bt);
    furi_delay_ms(200);
    bt_keys_storage_set_storage_path(hunter.bt, APP_DATA_PATH(".rf_hunter.keys"));
    hunter.ble_params.rx_callback = hunter_ble_rx;
    hunter.ble_params.rx_context = &hunter;
    hunter.ble_profile = bt_profile_start(hunter.bt, rfhunter_ble_profile, &hunter.ble_params);
    if(hunter.ble_profile) furi_hal_bt_start_advertising();
    view_port_update(hunter.viewport);

    while(hunter.running) {
        furi_delay_ms(hunter.settings.dwell_ms);
        hunter_process_capture(&hunter);
        hunter_process_nfc(&hunter);
        hunter_ble_drain(&hunter);
        if(hunter.receiver_active) {
            float rssi = furi_hal_subghz_get_rssi();
            if(hunter.rssi_samples == 0 || rssi < hunter.rssi_min) hunter.rssi_min = rssi;
            if(hunter.rssi_samples == 0 || rssi > hunter.rssi_max) hunter.rssi_max = rssi;
            hunter.rssi_sum += rssi;
            hunter.rssi_samples++;
            if(hunter.mode == HunterModeScout) {
                do {
                    hunter.frequency_index = (hunter.frequency_index + 1) % (sizeof(hunter_frequencies) / sizeof(hunter_frequencies[0]));
                } while(!hunter_frequency_allowed(&hunter, hunter.frequency_index));
                furi_hal_subghz_set_frequency(hunter_frequencies[hunter.frequency_index]);
            }
        }
        view_port_update(hunter.viewport);
    }

    if(hunter.receiver_active) {
        furi_hal_subghz_stop_async_rx();
        furi_hal_subghz_idle();
    }
    hunter_nfc_stop(&hunter);
    bt_disconnect(hunter.bt);
    furi_delay_ms(200);
    bt_keys_storage_set_default_path(hunter.bt);
    if(!bt_profile_restore_default(hunter.bt)) FURI_LOG_E("RfHunter", "restore default BLE failed");
    notification_message(hunter.notifications, &sequence_reset_rgb);
    if(hunter.events_file) {
        storage_file_close(hunter.events_file);
        storage_file_free(hunter.events_file);
    }
    if(hunter.store) rf_store_free(hunter.store);
    free(hunter.event_timings);
    if(hunter.storage) furi_record_close(RECORD_STORAGE);
    if(hunter.ble_rx) furi_stream_buffer_free(hunter.ble_rx);
    if(hunter.bt) furi_record_close(RECORD_BT);
    gui_remove_view_port(hunter.gui, hunter.viewport);
    view_port_free(hunter.viewport);
    furi_record_close(RECORD_NOTIFICATION);
    furi_record_close(RECORD_GUI);
    return 0;
}
