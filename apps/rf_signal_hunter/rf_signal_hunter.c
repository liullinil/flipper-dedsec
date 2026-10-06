/* RF Signal Hunter: passive Scout vertical slice.
 *
 * This adapter only starts the asynchronous Sub-GHz receiver and records pulse
 * activity counters. It deliberately has no TX/replay path. Desktop metadata,
 * grouping and upload acknowledgement live in uplink/uplink/rf_hunter.py.
 */
#include <furi.h>
#include <furi_hal_subghz.h>
#include <gui/gui.h>
#include <input/input.h>
#include <notification/notification_messages.h>
#include <storage/storage.h>
#include <furi_hal_rtc.h>

#define HUNTER_EVENTS_DIR APP_DATA_PATH("rf_signal_hunter")
#define HUNTER_EVENTS_PATH HUNTER_EVENTS_DIR "/events.jsonl"

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
    char session_id[16];
    bool running;
} Hunter;

static void hunter_record(Hunter* hunter) {
    if(!hunter->events_file || !storage_file_is_open(hunter->events_file)) return;
    DateTime now;
    furi_hal_rtc_get_datetime(&now);
    hunter->sequence++;
    char line[256];
    snprintf(
        line,
        sizeof(line),
        "{\"event_id\":\"rf-%s-%lu\",\"session_id\":\"%s\",\"sequence_number\":%lu,"
        "\"captured_at_utc\":\"%04u-%02u-%02uT%02u:%02u:%02uZ\",\"monotonic_ms\":%lu,"
        "\"source_type\":\"subghz\",\"frequency_hz\":433920000,\"rssi_dbm\":0,"
        "\"pulse_count\":%lu,\"last_duration_us\":%lu,\"upload_state\":\"pending\"}\n",
        hunter->session_id,
        (unsigned long)hunter->sequence,
        hunter->session_id,
        (unsigned long)hunter->sequence,
        now.year,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
        (unsigned long)furi_get_tick(),
        (unsigned long)hunter->pulses,
        (unsigned long)hunter->last_duration);
    storage_file_write(hunter->events_file, line, strlen(line));
    storage_file_sync(hunter->events_file);
}

static void hunter_capture(bool level, uint32_t duration, void* context) {
    Hunter* hunter = context;
    UNUSED(level);
    hunter->pulses++;
    hunter->last_duration = duration;
    // A long low gap ends a burst. The callback stays lightweight and never
    // touches GUI/storage from the radio context.
    if(duration > 8000) hunter->bursts++;
}

static void hunter_draw(Canvas* canvas, void* context) {
    Hunter* hunter = context;
    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 2, "RF SIGNAL HUNTER");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 2, 16, hunter->running ? "SCOUT  PASSIVE" : "SCOUT  STOPPED");
    canvas_draw_str(canvas, 2, 28, "No RF/NFC transmission");
    char line[32];
    snprintf(line, sizeof(line), "Bursts: %lu", (unsigned long)hunter->bursts);
    canvas_draw_str(canvas, 2, 40, line);
    snprintf(line, sizeof(line), "Pulses: %lu", (unsigned long)hunter->pulses);
    canvas_draw_str(canvas, 2, 51, line);
    canvas_draw_str(canvas, 2, 62, "Back: stop");
}

static void hunter_input(InputEvent* event, void* context) {
    Hunter* hunter = context;
    if(event->type == InputTypeShort && event->key == InputKeyBack) {
        view_port_enabled_set(hunter->viewport, false);
    }
}

int32_t rf_signal_hunter_app(void* context) {
    UNUSED(context);
    Hunter hunter = {0};
    hunter.gui = furi_record_open(RECORD_GUI);
    hunter.notifications = furi_record_open(RECORD_NOTIFICATION);
    snprintf(hunter.session_id, sizeof(hunter.session_id), "s%lu", (unsigned long)furi_get_tick());
    hunter.storage = furi_record_open(RECORD_STORAGE);
    storage_common_mkdir(hunter.storage, HUNTER_EVENTS_DIR);
    hunter.events_file = storage_file_alloc(hunter.storage);
    if(!storage_file_open(hunter.events_file, HUNTER_EVENTS_PATH, FSAM_WRITE, FSOM_OPEN_APPEND)) {
        storage_file_free(hunter.events_file);
        hunter.events_file = NULL;
    }
    hunter.viewport = view_port_alloc();
    view_port_draw_callback_set(hunter.viewport, hunter_draw, &hunter);
    view_port_input_callback_set(hunter.viewport, hunter_input, &hunter);
    gui_add_view_port(hunter.gui, hunter.viewport, GuiLayerFullscreen);

    // RX only: there is intentionally no furi_hal_subghz_tx or async TX call.
    furi_hal_subghz_set_frequency(433920000);
    furi_hal_subghz_rx();
    hunter.running = true;
    furi_hal_subghz_start_async_rx(hunter_capture, &hunter);
    view_port_update(hunter.viewport);

    while(hunter.running) {
        furi_delay_ms(250);
        if(!view_port_is_enabled(hunter.viewport)) hunter.running = false;
        if(hunter.bursts != hunter.notified_bursts) {
            hunter.notified_bursts = hunter.bursts;
            notification_message(hunter.notifications, &sequence_audiovisual_alert);
            notification_message(hunter.notifications, &sequence_set_only_green_255);
            hunter_record(&hunter);
        }
        view_port_update(hunter.viewport);
    }

    furi_hal_subghz_stop_async_rx();
    furi_hal_subghz_idle();
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
