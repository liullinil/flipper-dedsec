#include "uplink_settings.h"

#include <storage/storage.h>
#include <flipper_format/flipper_format.h>

#define SETTINGS_PATH   APP_DATA_PATH(".uplink.settings")
#define SETTINGS_HEADER "DedSec Uplink Settings"
#define SETTINGS_VER    1

void uplink_settings_default(UplinkSettings* s) {
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
}

static uint32_t clamp(uint32_t v, uint32_t hi) {
    return v > hi ? hi : v;
}

void uplink_settings_load(UplinkSettings* s) {
    uplink_settings_default(s);
    Storage* storage = furi_record_open(RECORD_STORAGE);
    FlipperFormat* ff = flipper_format_file_alloc(storage);
    FuriString* header = furi_string_alloc();
    uint32_t ver = 0, v = 0;
    do {
        if(!flipper_format_file_open_existing(ff, SETTINGS_PATH)) break;
        if(!flipper_format_read_header(ff, header, &ver)) break;
        if(furi_string_cmp_str(header, SETTINGS_HEADER)) break;

#define RD(key, field, hi)                               \
    if(flipper_format_read_uint32(ff, key, &v, 1)) {     \
        s->field = clamp(v, hi);                         \
    }                                                    \
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
#undef RD
        uint32_t tabs[4];
        if(flipper_format_read_uint32(ff, "Tabs", tabs, 4)) {
            for(int i = 0; i < 4; i++) s->tabs[i] = clamp(tabs[i], ScreenOff);
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
        v = s->vibro;
        flipper_format_write_uint32(ff, "Vibro", &v, 1);
        v = s->led;
        flipper_format_write_uint32(ff, "Led", &v, 1);
        v = s->backlight;
        flipper_format_write_uint32(ff, "Backlight", &v, 1);
        v = s->cmd_vibro;
        flipper_format_write_uint32(ff, "CmdVibro", &v, 1);
        v = s->indicators;
        flipper_format_write_uint32(ff, "Indicators", &v, 1);
        v = s->theme;
        flipper_format_write_uint32(ff, "Theme", &v, 1);
        v = s->font;
        flipper_format_write_uint32(ff, "Font", &v, 1);
        v = s->orientation;
        flipper_format_write_uint32(ff, "Orientation", &v, 1);
        v = s->auto_update;
        flipper_format_write_uint32(ff, "AutoUpdate", &v, 1);
        uint32_t tabs[4];
        for(int i = 0; i < 4; i++) tabs[i] = s->tabs[i];
        flipper_format_write_uint32(ff, "Tabs", tabs, 4);
    } while(false);
    flipper_format_free(ff);
    furi_record_close(RECORD_STORAGE);
}
