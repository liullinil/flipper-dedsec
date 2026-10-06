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

#define HUNTER_EVENTS_DIR APP_DATA_PATH("rf_signal_hunter")
#define HUNTER_EVENTS_PATH HUNTER_EVENTS_DIR "/events.jsonl"
#define HUNTER_DEVICE_ID_PATH HUNTER_EVENTS_DIR "/device_id"
#define HUNTER_FREQUENCY_HZ 433920000U
#define HUNTER_PULSE_RING_SIZE 16U

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
    volatile uint32_t pulses;
    volatile uint32_t bursts;
    volatile uint32_t last_duration;
    uint32_t notified_bursts;
    uint32_t sequence;
    uint32_t follow_duration;
    volatile uint32_t pulse_ring[HUNTER_PULSE_RING_SIZE];
    volatile uint8_t pulse_ring_count;
    volatile uint8_t pulse_ring_head;
    char session_id[16];
    char device_id[17];
    HunterMode mode;
    bool running;
    bool receiver_active;
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
        "\"source_type\":\"subghz\",\"mode\":\"%s\",\"frequency_hz\":%lu,\"rssi_dbm\":0,"
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
        (unsigned long)HUNTER_FREQUENCY_HZ,
        (unsigned long)hunter->pulses,
        (unsigned long)hunter->last_duration);
    size_t used = strlen(line);
    uint8_t count = hunter->pulse_ring_count;
    if(count > HUNTER_PULSE_RING_SIZE) count = HUNTER_PULSE_RING_SIZE;
    for(uint8_t i = 0; i < count && used + 24 < sizeof(line); i++) {
        uint8_t index = (hunter->pulse_ring_head + HUNTER_PULSE_RING_SIZE - count + i) % HUNTER_PULSE_RING_SIZE;
        used += snprintf(line + used, sizeof(line) - used, "%s%lu", i ? "," : "", (unsigned long)hunter->pulse_ring[index]);
    }
    snprintf(line + used, sizeof(line) - used, "],\"upload_state\":\"pending\"}\n");
    storage_file_write(hunter->events_file, line, strlen(line));
    storage_file_sync(hunter->events_file);
}

static void hunter_capture(bool level, uint32_t duration, void* context) {
    Hunter* hunter = context;
    UNUSED(level);
    hunter->pulses++;
    hunter->last_duration = duration;
    hunter->pulse_ring[hunter->pulse_ring_head] = duration;
    hunter->pulse_ring_head = (hunter->pulse_ring_head + 1) % HUNTER_PULSE_RING_SIZE;
    if(hunter->pulse_ring_count < HUNTER_PULSE_RING_SIZE) hunter->pulse_ring_count++;
    if(duration > 8000) hunter->bursts++;
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
        canvas_draw_str(canvas, 2, 29, "NFC observation unavailable");
        canvas_draw_str(canvas, 2, 40, "SDK 88.9 external poller");
        canvas_draw_str(canvas, 2, 51, "No field / no transmit");
    } else {
        snprintf(line, sizeof(line), "Bursts: %lu", (unsigned long)hunter->bursts);
        canvas_draw_str(canvas, 2, 29, line);
        snprintf(line, sizeof(line), "Pulses: %lu", (unsigned long)hunter->pulses);
        canvas_draw_str(canvas, 2, 40, line);
        snprintf(line, sizeof(line), "Last: %lu us", (unsigned long)hunter->last_duration);
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
        view_port_update(hunter->viewport);
    } else if(event->key == InputKeyOk) {
        if(hunter->mode == HunterModeNfc) return;
        hunter->receiver_active = !hunter->receiver_active;
        if(hunter->receiver_active) {
            furi_hal_subghz_set_frequency(HUNTER_FREQUENCY_HZ);
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
    hunter.storage = furi_record_open(RECORD_STORAGE);
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
    view_port_update(hunter.viewport);

    while(hunter.running) {
        furi_delay_ms(250);
        if(hunter.bursts != hunter.notified_bursts) {
            hunter.notified_bursts = hunter.bursts;
            if(hunter_event_selected(&hunter)) {
                notification_message(hunter.notifications, &sequence_audiovisual_alert);
                notification_message(hunter.notifications, &sequence_set_only_green_255);
                hunter_record(&hunter);
                if(hunter.mode == HunterModeFollow && hunter.follow_duration == 0) {
                    hunter.follow_duration = hunter.last_duration;
                }
            }
        }
        view_port_update(hunter.viewport);
    }

    if(hunter.receiver_active) {
        furi_hal_subghz_stop_async_rx();
        furi_hal_subghz_idle();
    }
    notification_message(hunter.notifications, &sequence_reset_rgb);
    if(hunter.events_file) {
        storage_file_close(hunter.events_file);
        storage_file_free(hunter.events_file);
    }
    if(hunter.storage) furi_record_close(RECORD_STORAGE);
    gui_remove_view_port(hunter.gui, hunter.viewport);
    view_port_free(hunter.viewport);
    furi_record_close(RECORD_NOTIFICATION);
    furi_record_close(RECORD_GUI);
    return 0;
}
