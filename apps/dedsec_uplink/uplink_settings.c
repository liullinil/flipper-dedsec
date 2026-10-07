#include "uplink_settings.h"

#include <storage/storage.h>
#include <flipper_format/flipper_format.h>

#define SETTINGS_PATH   APP_DATA_PATH(".uplink.settings")
#define SETTINGS_HEADER "DedSec Uplink Settings"
#define SETTINGS_VER    1

void uplink_settings_default(UplinkSettings* s) {
    memset(s, 0, sizeof(*s));
    s->vibro = 1;
    s->led = 1;
    s->backlight = 1;
    s->cmd_vibro = 1;
    s->indicators = IndicatorsBars;
    s->theme = ThemeNormal;
    s->font = FontNormal;
    s->orientation = OrientationHorizontal;
    s->auto_update = 0;
    s->tabs[0] = ScreenSys;
    s->tabs[1] = ScreenCodex;
    s->tabs[2] = ScreenClaude;
    s->tabs[3] = ScreenCmd;
    s->tabs[4] = ScreenRf;
    s->rf_band = 0;
    s->rf_rssi = -75;
    s->rf_dwell_ms = 250;
    s->rf_capture_ms = 1000;
    s->rf_feedback = 1;
    s->rf_keep = 0;
    s->rf_autostart = 0;
    s->rf_sync = 1;
    s->rf_tz = 0;
}

static uint32_t clamp(uint32_t v, uint32_t hi) {
    return v > hi ? hi : v;
}

static int32_t clamp_range(int32_t v, int32_t lo, int32_t hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

static uint8_t clamp_tab(uint32_t v) {
    return v < ScreenIdCount ? (uint8_t)v : ScreenOff;
}

void uplink_settings_load(UplinkSettings* s) {
    uplink_settings_default(s);
    Storage* storage = furi_record_open(RECORD_STORAGE);
    FlipperFormat* ff = flipper_format_file_alloc(storage);
    FuriString* header = furi_string_alloc();
    uint32_t ver = 0, v = 0;
    int32_t iv = 0;
    do {
        if(!flipper_format_file_open_existing(ff, SETTINGS_PATH)) break;
        if(!flipper_format_read_header(ff, header, &ver)) break;
        if(furi_string_cmp_str(header, SETTINGS_HEADER)) break;

        // every key is optional (files from older versions lack the newer ones)
#define RD(key, field, hi)                           \
    if(flipper_format_read_uint32(ff, key, &v, 1)) { \
        s->field = clamp(v, hi);                     \
    }                                                \
    flipper_format_rewind(ff);
#define RDI(key, field, lo, hi)                     \
    if(flipper_format_read_int32(ff, key, &iv, 1)) { \
        s->field = clamp_range(iv, lo, hi);         \
    }                                               \
    flipper_format_rewind(ff);
        RD("Vibro", vibro, 1);
        RD("Led", led, 1);
        RD("Backlight", backlight, 1);
        RD("CmdVibro", cmd_vibro, 1);
        RD("Indicators", indicators, 1);
        RD("Theme", theme, 1);
        RD("Font", font, 3);
        RD("Orientation", orientation, 1);
        RD("AutoUpdate", auto_update, 1);
        RD("RfBand", rf_band, 3);
        RDI("RfRssi", rf_rssi, -110, -30);
        RDI("RfDwell", rf_dwell_ms, 50, 10000);
        RDI("RfCapture", rf_capture_ms, 200, 2000);
        RD("RfFeedback", rf_feedback, 1);
        RD("RfKeep", rf_keep, 1);
        RD("RfAutostart", rf_autostart, 1);
        RD("RfSync", rf_sync, 1);
        RDI("RfTz", rf_tz, -14 * 60, 14 * 60);
#undef RD
#undef RDI
        uint32_t tabs[TAB_SLOTS];
        if(flipper_format_read_uint32(ff, "Tabs", tabs, TAB_SLOTS)) {
            for(int i = 0; i < TAB_SLOTS; i++)
                s->tabs[i] = clamp_tab(tabs[i]);
        } else {
            // v1.1 saved four slots: keep them and put the new RF tab last
            flipper_format_rewind(ff);
            if(flipper_format_read_uint32(ff, "Tabs", tabs, 4)) {
                for(int i = 0; i < 4; i++)
                    s->tabs[i] = clamp_tab(tabs[i]);
                s->tabs[4] = ScreenRf;
            }
        }
    } while(false);
    furi_string_free(header);
    flipper_format_free(ff);
    furi_record_close(RECORD_STORAGE);
}

void uplink_settings_save(const UplinkSettings* s) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    FlipperFormat* ff = flipper_format_file_alloc(storage);
    do {
        if(!flipper_format_file_open_always(ff, SETTINGS_PATH)) break;
        if(!flipper_format_write_header_cstr(ff, SETTINGS_HEADER, SETTINGS_VER)) break;
        uint32_t v;
        int32_t iv;
#define WR(key, value)                              \
    v = (value);                                    \
    flipper_format_write_uint32(ff, key, &v, 1);
#define WRI(key, value)                             \
    iv = (value);                                   \
    flipper_format_write_int32(ff, key, &iv, 1);
        WR("Vibro", s->vibro);
        WR("Led", s->led);
        WR("Backlight", s->backlight);
        WR("CmdVibro", s->cmd_vibro);
        WR("Indicators", s->indicators);
        WR("Theme", s->theme);
        WR("Font", s->font);
        WR("Orientation", s->orientation);
        WR("AutoUpdate", s->auto_update);
        WR("RfBand", s->rf_band);
        WRI("RfRssi", s->rf_rssi);
        WRI("RfDwell", s->rf_dwell_ms);
        WRI("RfCapture", s->rf_capture_ms);
        WR("RfFeedback", s->rf_feedback);
        WR("RfKeep", s->rf_keep);
        WR("RfAutostart", s->rf_autostart);
        WR("RfSync", s->rf_sync);
        WRI("RfTz", s->rf_tz);
#undef WR
#undef WRI
        uint32_t tabs[TAB_SLOTS];
        for(int i = 0; i < TAB_SLOTS; i++)
            tabs[i] = s->tabs[i];
        flipper_format_write_uint32(ff, "Tabs", tabs, TAB_SLOTS);
    } while(false);
    flipper_format_free(ff);
    furi_record_close(RECORD_STORAGE);
}
