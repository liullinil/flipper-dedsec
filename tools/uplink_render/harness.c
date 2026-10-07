/* Host renderer for DedSec Uplink screens: compiles the real uplink.c drawing code against
 * u8g2 and writes one PBM per screen (physical 128x64 framebuffer). */
#include "uplink.c"
#include "hood_xbm.h"

const FuriHalBleProfileTemplate* const uplink_ble_profile = NULL;
bool uplink_ble_tx(FuriHalBleProfileBase* p, const uint8_t* d, uint16_t n) {
    (void)p;
    (void)d;
    (void)n;
    return true;
}

void uplink_settings_default(UplinkSettings* s) {
    memset(s, 0, sizeof(*s));
    s->vibro = 1;
    s->led = 1;
    s->backlight = 1;
    s->cmd_vibro = 1;
    s->indicators = IndicatorsBars;
    s->font = FontNormal;
    s->tabs[0] = ScreenSys;
    s->tabs[1] = ScreenCodex;
    s->tabs[2] = ScreenClaude;
    s->tabs[3] = ScreenCmd;
    s->tabs[4] = ScreenRf;
    s->rf_rssi = -75;
    s->rf_dwell_ms = 250;
    s->rf_capture_ms = 1000;
    s->rf_feedback = 1;
    s->rf_sync = 1;
}
void uplink_settings_load(UplinkSettings* s) {
    uplink_settings_default(s);
}
void uplink_settings_save(const UplinkSettings* s) {
    (void)s;
}

void ota_init(Ota* o) {
    memset(o, 0, sizeof(*o));
}
void ota_free(Ota* o) {
    (void)o;
}
bool ota_newer(const char* tag, const char* mine) {
    return strcmp(tag + (tag[0] == 'v'), mine) > 0;
}
bool ota_begin(Ota* o, const char* tag, uint32_t size, uint32_t crc) {
    (void)crc;
    strlcpy(o->tag, tag, sizeof(o->tag));
    o->size = size;
    o->state = OtaReceiving;
    return true;
}
int32_t ota_chunk(Ota* o, uint32_t off, const char* b64) {
    (void)off;
    (void)b64;
    return (int32_t)o->written;
}
bool ota_finish(Ota* o) {
    o->state = OtaDone;
    return true;
}
void ota_abort(Ota* o, const char* why) {
    o->state = OtaFailed;
    strlcpy(o->error, why, sizeof(o->error));
}
uint8_t ota_percent(const Ota* o) {
    return o->size ? (uint8_t)(o->written * 100 / o->size) : 0;
}


/* ---- fake RF engine: the screens only need its status */
struct RfEngine {
    int unused;
};
static struct RfEngine g_rf;
static RfStatus g_rf_status;
RfEngine* rf_engine_alloc(RfReplyCallback reply, RfChangedCallback changed, void* context) {
    (void)reply;
    (void)changed;
    (void)context;
    return &g_rf;
}
void rf_engine_free(RfEngine* e) {
    (void)e;
}
void rf_engine_configure(RfEngine* e, const RfConfig* c) {
    (void)e;
    (void)c;
}
void rf_engine_set_mode(RfEngine* e, RfMode m) {
    (void)e;
    g_rf_status.mode = m;
}
void rf_engine_start(RfEngine* e) {
    (void)e;
    g_rf_status.running = true;
}
void rf_engine_stop(RfEngine* e) {
    (void)e;
    g_rf_status.running = false;
}
void rf_engine_mark_seen(RfEngine* e) {
    (void)e;
}
void rf_engine_get_status(RfEngine* e, RfStatus* out) {
    (void)e;
    *out = g_rf_status;
}
void rf_engine_request(RfEngine* e, const char* line) {
    (void)e;
    (void)line;
}
void rf_engine_status_line(RfEngine* e, char* out, size_t size) {
    (void)e;
    snprintf(out, size, "R|0|0|0|0|0");
}

const uint8_t* canvas_host_buffer(void);
static Canvas g_canvas;
static const char* g_out = "out";

static void feed(App* app, const char* line) {
    char buf[LINE_MAX];
    strlcpy(buf, line, sizeof(buf));
    parse_line(app, buf);
}

static void dump(const char* name) {
    char path[256];
    snprintf(path, sizeof(path), "%s/%s.pbm", g_out, name);
    FILE* f = fopen(path, "wb");
    if(!f) {
        printf("cannot write %s\n", path);
        return;
    }
    fprintf(f, "P4\n128 64\n");
    const uint8_t* fb = canvas_host_buffer();
    for(int y = 0; y < 64; y++) {
        for(int xb = 0; xb < 16; xb++) {
            uint8_t out = 0;
            for(int i = 0; i < 8; i++) {
                int x = xb * 8 + i;
                if(fb[(y / 8) * 128 + x] & (1 << (y % 8))) out |= 0x80 >> i;
            }
            fputc(out, f);
        }
    }
    fclose(f);
}

static void render(App* app, const char* name) {
    char full[128];
    bool vertical = app->settings.orientation == OrientationVertical;
    static const char* fonts[] = {"normal", "large", "small", "micro"};
    snprintf(full, sizeof(full), "%s_%s_%s", name, vertical ? "v" : "h", fonts[app->settings.font]);
    canvas_set_orientation(
        &g_canvas, vertical ? CanvasOrientationVertical : CanvasOrientationHorizontal);
    canvas_clear(&g_canvas);
    canvas_set_color(&g_canvas, ColorBlack);
    canvas_set_font(&g_canvas, FontSecondary);
    MainModel m = {app};
    main_draw(&g_canvas, &m);
    dump(full);
}

static App* make_app(void) {
    App* app = calloc(1, sizeof(App));
    uplink_settings_default(&app->settings);
    ota_init(&app->ota);
    rebuild_tabs(app);
    app->sys.net_max = 64;
    app->sys.dsk_max = 256;
    app->link = true;
    app->tick = 400;
    app->last_rx_tick = 400;
    feed(app, "H|AIDAR-PC");
    static const int cpu[] = {12, 18, 25, 40, 33, 22, 19, 55, 71, 64, 38, 27, 23, 30, 45, 52, 41,
                              28, 21, 17, 26, 35, 62, 80, 74, 51, 33, 24, 20, 23};
    char line[96];
    for(int r = 0; r < 2; r++)
        for(int i = 0; i < 30; i++) {
            // a download spike and a disk burst near the end, so the autoscaled bars sit mid-way
            int spike = r == 1 && i == 27, burst = r == 1 && i == 26;
            snprintf(
                line,
                sizeof(line),
                "S|%d|61|40|%d|2400|35|%d|9800|16000",
                cpu[i],
                burst ? 3600 : 1200,
                spike ? 1500 : 880);
            feed(app, line);
        }
    feed(app, "I|X|0|k1|A|0|0|15|1|router: миграция БД|Ждёт подтверждения: применить миграцию 0042 к базе staging и перезапустить воркеры?");
    feed(app, "I|X|1|k2|W|3|7|120|0|flipper-dedsec|Пишу тесты для rf_store.c: восстановление после обрыва записи, проверка CRC и повторная отправка подтверждений после разрыва BLE-связи. Затем обновлю README и соберу релиз.");
    feed(app, "I|X|2|k3|W|0|0|42|0|- explorer Kepler|reading uplink/link.py");
    feed(app, "I|X|3|k4|I|0|0|300|1|docs: README|Turn finished: updated the screenshots and the release notes");
    feed(app, "I|X|4|k5|W|1|2|8|0|ci-fix|Running pytest -q");
    feed(app, "I|X|5|k6|W|0|0|65|0|- worker Hubble|grep -n ota_begin apps/");
    feed(app, "I|X|6|k7|S|0|0|9000|0|old session|idle");
    feed(app, "L|X|7");
    feed(app, "I|C|0|c1|W|4|9|33|0|Flipper: доведение релиза|Собираю рендер экранов приложения на ПК, чтобы проверить вертикальную ориентацию и новые шрифты");
    feed(app, "I|C|1|c2|I|0|0|610|1|Robot cell program|Turn finished");
    feed(app, "L|C|2");
    app->cmd.seq = 3;
    feed(app, "W|D:\\src\\Flipper");
    cmd_push(app, "> dir /b");
    app->cmd.running = true;
    feed(app, "O|3|apps");
    feed(app, "O|3|assets");
    feed(app, "O|3|10/07/2026  09:57    <DIR>          uplink");
    feed(app, "O|3|Привет мир.txt");
    feed(app, "O|3|README.md");
    feed(app, "X|3|0");
    app->alert = false; // parse of I lines may raise alerts
    return app;
}

static void render_all(App* base, int orientation, int font) {
    App* app = malloc(sizeof(App));
    memcpy(app, base, sizeof(App));
    app->settings.orientation = orientation;
    app->settings.font = font;

    // tabs: SYS CDX CLD CMD = indices 0..3
    app->tab_index = 1;
    app->lists[KindCodex].cursor = 1;
    render(app, "cdx_list");
    app->detail = true;
    render(app, "cdx_detail");
    app->lists[KindCodex].detail_scroll = 2;
    render(app, "cdx_detail_scrolled");
    app->lists[KindCodex].detail_scroll = 0;
    app->detail = false;
    app->tab_index = 3;
    render(app, "cmd");
    {
        app->tab_index = 0;
        render(app, "sys_bars");
        app->settings.indicators = IndicatorsText;
        render(app, "sys_text");
        app->settings.indicators = IndicatorsBars;
        app->tab_index = 2;
        render(app, "cld_list");
        app->lists[KindCodex].cursor = 5; // scrolled list
        app->tab_index = 1;
        render(app, "cdx_list_scrolled");
        app->lists[KindCodex].cursor = 1;
        app->alert = true;
        app->alert_kind = KindCodex;
        app->alert_state = 'A';
        strlcpy(app->alert_key, "k1", sizeof(app->alert_key));
        strlcpy(app->alert_name, "router: миграция БД", sizeof(app->alert_name));
        render(app, "alert_approval");
        app->alert_state = 'I';
        render(app, "alert_turn");
        app->alert_update = true;
        strlcpy(app->ota.tag, "v2.0.0", sizeof(app->ota.tag));
        render(app, "alert_update");
        app->alert = false;
        app->alert_update = false;
        app->ota.state = OtaReceiving;
        app->ota.size = 48000;
        app->ota.written = 20500;
        render(app, "ota_progress");
        app->ota.state = OtaIdle;
        app->link = false;
        render(app, "offline");
        app->link = true;
        // RF tab: tabs are SYS CDX CLD CMD RF -> index 4
        app->rf = &g_rf;
        app->tab_index = 4;
        memset(&g_rf_status, 0, sizeof(g_rf_status));
        g_rf_status.mode = RfModeScout;
        g_rf_status.running = true;
        g_rf_status.frequency_hz = 433920000;
        g_rf_status.events = 37;
        g_rf_status.families = 5;
        g_rf_status.unseen = 0;
        g_rf_status.pending = 12;
        g_rf_status.free_kb = 3 * 1024 * 1024 + 300 * 1024;
        g_rf_status.last_unix = 1791358800u + 41 * 60 + 7;
        g_rf_status.last_frequency_hz = 433920000;
        g_rf_status.last_rssi_dbm = -63;
        app->settings.rf_tz = 180;
        app->rf_status = g_rf_status;
        render(app, "rf_scout");
        g_rf_status.mode = RfModeFollow;
        g_rf_status.follow_valid = true;
        g_rf_status.last_similarity = 87;
        app->rf_status = g_rf_status;
        render(app, "rf_follow");
        g_rf_status.mode = RfModeNfc;
        g_rf_status.frequency_hz = 0;
        g_rf_status.nfc_field = true;
        app->rf_status = g_rf_status;
        render(app, "rf_nfc");
        g_rf_status.mode = RfModeScout;
        g_rf_status.running = false;
        g_rf_status.storage_full = true;
        app->rf_status = g_rf_status;
        render(app, "rf_full");
        memset(&g_rf_status, 0, sizeof(g_rf_status));
        app->rf_status = g_rf_status;
        render(app, "rf_idle");
        app->link = false;
        render(app, "rf_nolink");
    }
    free(app);
}

int main(int argc, char** argv) {
    if(argc > 1) g_out = argv[1];
    canvas_init_host(&g_canvas);
    App* base = make_app();
    for(int o = 0; o < 2; o++)
        for(int f = 0; f < 4; f++)
            render_all(base, o, f);
    printf("done\n");
    return 0;
}
