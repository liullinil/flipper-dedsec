/* RF Signal Hunter: passive Scout/Capture/Follow/NFC modes.
 *
 * This FAP deliberately uses only the public RX Sub-GHz API available in SDK
 * 88.9.  NFC is exposed as a safe observation screen because the low-level
 * NFC poller API is not stable for external FAPs; no NFC field is enabled.
 * There are no TX, replay, or emulation calls in this application.
 */
#include <furi.h>
#include <furi_hal_subghz.h>
#include <gui/gui.h>
#include <input/input.h>
#include <notification/notification_messages.h>
#include <storage/storage.h>
#include <furi_hal_rtc.h>
#include <furi_hal_random.h>
#include <furi_hal_nfc.h>
#include <furi_hal_bt.h>
#include <bt/bt_service/bt.h>
#include "rf_hunter_ble.h"
#include "rf_store.h"
#include "rf_capture.h"

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
    volatile uint32_t pulses;
    volatile uint32_t bursts;
    volatile uint32_t last_duration;
    uint32_t notified_bursts;
    uint32_t sequence;
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

static void hunter_record(Hunter* hunter) {
    if(!hunter->events_file || !storage_file_is_open(hunter->events_file)) return;
    DateTime now;
    furi_hal_rtc_get_datetime(&now);
    hunter->sequence++;
    char line[448];
    snprintf(
        line,
        sizeof(line),
        "{\"event_id\":\"rf-%s-%s-%lu\",\"device_id\":\"%s\",\"session_id\":\"%s\",\"sequence_number\":%lu,"
        "\"captured_at_utc\":\"%04u-%02u-%02uT%02u:%02u:%02uZ\",\"monotonic_ms\":%lu,"
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
        (unsigned long)furi_get_tick(),
        hunter_mode_name(hunter->mode),
        (unsigned long)hunter_frequencies[hunter->frequency_index],
        (double)(hunter->rssi_samples ? hunter->rssi_min : 0.0f),
        (double)(hunter->rssi_samples ? hunter->rssi_sum / hunter->rssi_samples : 0.0f),
        (double)(hunter->rssi_samples ? hunter->rssi_max : 0.0f),
        (unsigned long)hunter->pulses,
        (unsigned long)hunter->last_duration);
    size_t used = strlen(line);
    uint16_t count = hunter->event_timing_count;
    for(uint16_t i = 0; i < count && used + 24 < sizeof(line); i++) {
        used += snprintf(line + used, sizeof(line) - used, "%s%lu", i ? "," : "", (unsigned long)hunter->event_timings[i].duration_us);
    }
    snprintf(line + used, sizeof(line) - used, "],\"upload_state\":\"pending\"}\n");
    storage_file_write(hunter->events_file, line, strlen(line));
    storage_file_sync(hunter->events_file);
    char event_id[80];
    snprintf(event_id, sizeof(event_id), "rf-%s-%s-%lu", hunter->device_id, hunter->session_id, (unsigned long)hunter->sequence);
    rf_store_save(hunter->store, event_id, line, strlen(line));
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
        if(timing.duration_us > 8000 && hunter->event_timing_count > 1) {
            hunter->bursts++;
            if(hunter_event_selected(hunter)) {
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
        snprintf(reply, sizeof(reply), "{\"v\":1,\"rid\":%lu,\"op\":\"list_ack\"}\n", (unsigned long)rid);
        hunter_ble_send(hunter, reply);
        File* dir = storage_file_alloc(hunter->storage);
        char name[96];
        FileInfo info;
        if(storage_dir_open(dir, RF_STORE_EVENTS_DIR)) {
            while(storage_dir_read(dir, &info, name, sizeof(name))) {
                size_t n = strlen(name);
                if(n <= 5 || strcmp(name + n - 5, ".json")) continue;
                name[n - 5] = 0;
                char record[640]; size_t length = 0;
                if(rf_store_read(hunter->store, name, record, sizeof(record), &length)) {
                    char item[220];
                    snprintf(item, sizeof(item), "{\"v\":1,\"rid\":%lu,\"op\":\"item\",\"event_id\":\"%s\",\"size\":%lu,\"crc32\":%lu,\"next\":%lu}\n", (unsigned long)rid, name, (unsigned long)length, (unsigned long)hunter_crc32(record, length), (unsigned long)1);
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
                hunter_send_json_chunk(hunter, rid, id, record, length, hunter_json_number(line, "offset"));
            }
        }
    } else if(strstr(line, "\"op\":\"ack\"")) {
        char id[80] = {0}; char reply[180];
        if(hunter_extract_string(line, "event_id", id, sizeof(id))) rf_store_ack(hunter->store, id);
        snprintf(reply, sizeof(reply), "{\"v\":1,\"rid\":%lu,\"op\":\"acked\",\"event_id\":\"%s\"}\n", (unsigned long)rid, id);
        hunter_ble_send(hunter, reply);
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
    snprintf(line, sizeof(line), "%s  %s", hunter_mode_name(hunter->mode), hunter->running ? "RUN" : "STOP");
    canvas_draw_str(canvas, 2, 16, line);
    if(hunter->mode == HunterModeNfc) {
        canvas_draw_str(canvas, 2, 29, hunter->nfc_detect_active ? "NFC field detector" : "NFC detector stopped");
        canvas_draw_str(canvas, 2, 40, furi_hal_nfc_field_is_present() ? "External field: present" : "External field: absent");
        canvas_draw_str(canvas, 2, 51, "Presence only; no poller TX");
    } else {
        snprintf(line, sizeof(line), "Events: %lu  New fam: %lu", (unsigned long)(hunter->bursts - hunter->events_reviewed), (unsigned long)hunter->families_seen);
        canvas_draw_str(canvas, 2, 29, line);
        snprintf(line, sizeof(line), "Pulses: %lu", (unsigned long)hunter->pulses);
        canvas_draw_str(canvas, 2, 40, line);
        snprintf(line, sizeof(line), "Last: %lu us RSSI %.0f", (unsigned long)hunter->last_duration, (double)(hunter->rssi_samples ? hunter->rssi_sum / hunter->rssi_samples : 0.0f));
        canvas_draw_str(canvas, 2, 51, line);
    }
    canvas_draw_str(canvas, 2, 62, "L/R mode  OK run  Back exit");
}

static void hunter_input(InputEvent* event, void* context) {
    Hunter* hunter = context;
    if(event->type != InputTypeShort) return;
    if(event->key == InputKeyBack) {
        hunter->running = false;
        view_port_enabled_set(hunter->viewport, false);
    } else if(event->key == InputKeyLeft || event->key == InputKeyRight) {
        int delta = event->key == InputKeyRight ? 1 : -1;
        int next = (int)hunter->mode + delta;
        if(next < 0) next = HunterModeCount - 1;
        if(next >= HunterModeCount) next = 0;
        hunter->mode = (HunterMode)next;
        if(hunter->mode == HunterModeNfc && !hunter->nfc_detect_active) {
            hunter->nfc_detect_active = furi_hal_nfc_field_detect_start() == FuriHalNfcErrorNone;
        } else if(hunter->mode != HunterModeNfc && hunter->nfc_detect_active) {
            furi_hal_nfc_field_detect_stop();
            hunter->nfc_detect_active = false;
        }
        view_port_update(hunter->viewport);
    } else if(event->key == InputKeyOk) {
        if(hunter->mode == HunterModeScout) hunter->events_reviewed = hunter->bursts;
        if(hunter->mode == HunterModeNfc) {
            if(hunter->nfc_detect_active) furi_hal_nfc_field_detect_stop();
            hunter->nfc_detect_active = false;
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
        furi_delay_ms(250);
        hunter_process_capture(&hunter);
        hunter_ble_drain(&hunter);
        if(hunter.receiver_active) {
            float rssi = furi_hal_subghz_get_rssi();
            if(hunter.rssi_samples == 0 || rssi < hunter.rssi_min) hunter.rssi_min = rssi;
            if(hunter.rssi_samples == 0 || rssi > hunter.rssi_max) hunter.rssi_max = rssi;
            hunter.rssi_sum += rssi;
            hunter.rssi_samples++;
            if(hunter.mode == HunterModeScout) {
                hunter.frequency_index = (hunter.frequency_index + 1) % (sizeof(hunter_frequencies) / sizeof(hunter_frequencies[0]));
                furi_hal_subghz_set_frequency(hunter_frequencies[hunter.frequency_index]);
            }
        }
        view_port_update(hunter.viewport);
    }

    if(hunter.receiver_active) {
        furi_hal_subghz_stop_async_rx();
        furi_hal_subghz_idle();
    }
    if(hunter.nfc_detect_active) furi_hal_nfc_field_detect_stop();
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
