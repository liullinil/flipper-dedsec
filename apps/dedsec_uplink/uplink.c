/* DedSec Uplink - PC / Codex / Claude monitor + remote shell for Flipper Zero.
 *
 * The PC companion (uplink/dedsec_uplink.pyw) connects over BLE.
 *   PC -> Flipper (RX char, text lines):
 *     H|host                                   hello
 *     S|cpu|ram|dsk|rd|wr|up|dn|used|total     system load (rates KB/s)
 *     L|k|count                                list size, k = X (Codex) / C (Claude)
 *     I|k|idx|key|state|done|total|age|attn|name|detail
 *     O|seq|text                               one line of command output
 *     X|seq|code                               command finished with exit code
 *     W|cwd                                    shell working directory
 *     B                                        host is going away
 *     BO|active|auto                           Blackout: the PC's lock screen is up (1) or not,
 *                                              auto-blackout on link loss 0 off / 1 arming / 2 armed
 *     Z|utc_unix|tz_minutes                    PC clock (RF timestamps)
 *     RL|cursor  RR|id|offset  RA|id|size|crc  RF journal import (see rf_engine.h)
 *   Flipper -> PC (TX notify char):
 *     C|seq|command                            run this command
 *     K|seq                                    cancel the running command
 *     T|seq|text                               send text to a running command's stdin
 *     BO|1 / BO|0                              Blackout the PC / restore it (SYS tab, OK)
 *     BB                                       the app is closing on purpose (no auto-blackout)
 *     R|pending|stored|free_kb|state|errors    RF journal status; RI/RE/RD/RK/RX answer RL/RR/RA
 * state: W working, A needs approval, I your turn, S idle, E error.
 * OTA (N/UB/UD/UE, V/U/UA) is described in uplink_ota.h.
 */
#include <furi.h>
#include <furi_hal_bt.h>
#include <gui/gui.h>
#include <gui/view_dispatcher.h>
#include <gui/modules/text_input.h>
#include <gui/modules/variable_item_list.h>
#include <input/input.h>
#include <notification/notification_messages.h>
#include <bt/bt_service/bt.h>
#include <storage/storage.h>
#include <stdlib.h>
#include <string.h>

#include "uplink_ble.h"
#include "uplink_settings.h"
#include "dedsec_uplink_icons.h"
#include "uplink_fonts.h"
#include "uplink_ota.h"
#include "rf_engine.h"
#include <loader/loader.h>
#include <furi_hal_rtc.h>
#include <datetime/datetime.h>

#define TAG          "Uplink"
#define MAX_ITEMS    10
#define HIST         62
#define SEEN_MAX     40
#define LINE_MAX     600
#define LAG_TICKS    (3 * 4)
#define LINK_TICKS   (8 * 4)
#define ALERT_TICKS  (5 * 4)
#define CMD_LINES    40
#define CMD_COLW     128
#define CMD_INPUT    200

enum { ViewMain = 0, ViewKeyboard, ViewSettings };
enum { EvRx = 1, EvTick, EvRf, EvRfTx };

#define RF_STATUS_TICKS (10 * 4) // R| status line to the PC every 10 s
#define RF_REMIND_TICKS (4 * 4) // LED reminder of RF events nobody has looked at, every 4 s
#define RF_TX_BUF       2048
#define BLACKOUT_ASK_TICKS  (3 * 4) // "BLACKOUT PC?" waits this long for a second OK
#define BLACKOUT_SENT_TICKS (10 * 4) // "sent, waiting for the PC" shown this long at most

typedef enum { KindCodex, KindClaude, KindCount } Kind;

typedef struct {
    char key[8];
    char state;
    uint8_t done;
    uint8_t total;
    uint32_t age;
    uint32_t attn;
    char name[64];   // UTF-8
    char detail[256]; // UTF-8
} Item;

typedef struct {
    Item items[MAX_ITEMS];
    uint8_t count;
    uint8_t cursor;
    uint8_t scroll;
    uint8_t detail_scroll;
} List;

typedef struct {
    uint8_t cpu, ram, dsk;
    uint32_t rd, wr, up, dn;
    uint32_t used_mb, total_mb;
    uint32_t net_max, dsk_max;
    uint8_t cpu_hist[HIST];
    uint8_t hist_len;
    bool valid;
} Sys;

typedef struct {
    char key[8];
    uint32_t attn;
    bool dismissed;
} Seen;

typedef struct {
    char lines[CMD_LINES][CMD_COLW];
    uint8_t count;
    uint8_t head;
    int16_t scroll;
    uint8_t seq;
    bool running;
    bool have_exit;
    int exit_code;
    bool unseen;
    char cwd[48];
    char input[CMD_INPUT];
} Cmd;

typedef struct {
    Gui* gui;
    ViewDispatcher* views;
    View* main_view;
    TextInput* keyboard;
    VariableItemList* settings_view;
    FuriTimer* timer;
    FuriStreamBuffer* rx;
    FuriMutex* mutex;
    NotificationApp* notifications;
    Bt* bt;
    FuriHalBleProfileBase* profile;
    UplinkProfileParams params;
    uint32_t current_view;

    UplinkSettings settings;

    // model (guarded by mutex)
    ScreenId tabs[TAB_SLOTS];
    uint8_t tab_count;
    uint8_t tab_index;
    bool detail;
    bool link;
    bool host_closed;
    uint32_t tick;
    uint32_t last_rx_tick;
    char host[24];
    Sys sys;
    List lists[KindCount];
    Seen seen[SEEN_MAX];
    uint8_t seen_n;
    Cmd cmd;
    bool alert;
    uint32_t alert_until;
    Kind alert_kind;
    char alert_state;
    char alert_key[8];
    char alert_name[64];
    bool alert_update;      // the banner is an update offer
    Ota ota;
    uint32_t ver_tick;      // last time we told the PC our version
    uint32_t restart_at;    // relaunch the freshly installed .fap at this tick
    uint32_t ota_req_tick;

    // Blackout: the PC companion's own lock screen, driven from the SYS tab
    uint8_t blackout; // the PC says its lock screen is up
    uint8_t blackout_auto; // the PC: 0 off, 1 arming (first minutes after start), 2 armed
    uint32_t blackout_ask; // OK pressed once on SYS: a second OK until this tick confirms
    uint32_t blackout_sent; // BO|1 or BO|0 sent at this tick, waiting for the PC (0 = none)

    // RF Hunter (the engine has its own thread; these are app-thread copies)
    RfEngine* rf;
    RfStatus rf_status; // snapshot for drawing, refreshed on EvRf and every tick
    FuriStreamBuffer* rf_tx; // reply lines from the engine thread, sent by the app thread
    char rf_line[256];
    uint16_t rf_line_len;
    uint32_t rf_status_tick; // last R| line sent to the PC
    uint32_t rf_status_listed; // listed count in that line
    uint32_t rf_status_carry; // carried count in that line
    // At most one EvRf and one EvRfTx wait in the dispatcher queue: posting blocks when the
    // queue is full, and after view_dispatcher_run returns nobody drains it any more.
    volatile bool rf_event_posted;
    volatile bool rf_tx_posted;
    volatile bool rf_closing; // set when the dispatcher has stopped (also stops EvRx posts)
    volatile bool rx_posted;

    char line[LINE_MAX];
    uint16_t line_len;
} App;

typedef struct {
    App* app;
} MainModel;

static void uplink_send(App* app, const char* line);
static void send_version(App* app);
static void ota_request(App* app);
static void rf_request(App* app, const char* line);
static void rf_clock(App* app, uint32_t utc);
static void rf_send_status(App* app);
static void build_settings(App* app);

/* ------------------------------------------------------------------ theme */
static uint8_t g_fg = ColorBlack, g_bg = ColorWhite;

static void fg(Canvas* c) {
    canvas_set_color(c, g_fg);
}
static void bg(Canvas* c) {
    canvas_set_color(c, g_bg);
}

/* ------------------------------------------------------------------ notifications */
typedef enum {
    NotifyApproval,
    NotifyYourTurn,
    NotifyCmdDone,
    NotifyLink,
    NotifyUpdate,
} NotifyKind;

static void uplink_notify(App* app, NotifyKind kind) {
    const UplinkSettings* s = &app->settings;
    const NotificationMessage* on = NULL;
    const NotificationMessage* off = NULL;
    int pulses = 1;
    bool longp = false;
    bool vib = s->vibro;
    // The LED is reserved for agent attention (approval or your turn). Other
    // notifications may still wake the screen or vibrate, but must stay quiet.
    bool led = s->led && (kind == NotifyApproval || kind == NotifyYourTurn);
    switch(kind) {
    case NotifyApproval:
        on = &message_red_255;
        off = &message_red_0;
        pulses = 3;
        longp = true;
        break;
    case NotifyYourTurn:
        on = &message_green_255;
        off = &message_green_0;
        pulses = 2;
        break;
    case NotifyCmdDone:
        on = &message_blue_255;
        off = &message_blue_0;
        pulses = 2;
        vib = s->vibro && s->cmd_vibro;
        break;
    case NotifyLink:
        on = &message_blue_255;
        off = &message_blue_0;
        pulses = 1;
        vib = false;
        break;
    case NotifyUpdate:
        on = &message_green_255;
        off = &message_green_0;
        pulses = 1;
        longp = true;
        break;
    }
    const NotificationMessage* seq[32];
    int n = 0;
    if(s->backlight) seq[n++] = &message_display_backlight_on;
    if(vib) seq[n++] = &message_force_vibro_setting_on;
    for(int i = 0; i < pulses; i++) {
        if(led) seq[n++] = on;
        if(vib) seq[n++] = &message_vibro_on;
        seq[n++] = longp ? &message_delay_250 : &message_delay_100;
        if(vib) seq[n++] = &message_vibro_off;
        if(led) seq[n++] = off;
        if(i + 1 < pulses) seq[n++] = &message_delay_100;
    }
    if(vib) seq[n++] = &message_force_vibro_setting_off;
    seq[n++] = NULL;
    notification_message_block(app->notifications, (const NotificationSequence*)seq);
}

/* ------------------------------------------------------------------ helpers */
static void fmt_rate(char* out, size_t size, uint32_t kbps) {
    if(kbps < 1000) {
        snprintf(out, size, "%luK", (unsigned long)kbps);
    } else if(kbps < 100 * 1024) {
        uint32_t tenth = kbps * 10 / 1024;
        snprintf(out, size, "%lu.%luM", (unsigned long)(tenth / 10), (unsigned long)(tenth % 10));
    } else {
        snprintf(out, size, "%luM", (unsigned long)(kbps / 1024));
    }
}

static void fmt_age(char* out, size_t size, uint32_t s) {
    if(s < 60)
        snprintf(out, size, "%lus", (unsigned long)s);
    else if(s < 3600)
        snprintf(out, size, "%lum", (unsigned long)(s / 60));
    else
        snprintf(out, size, "%luh", (unsigned long)(s / 3600));
}

/* ---- UTF-8: names, details and console output can be Cyrillic (drawn with the embedded
 * u8g2 cyrillic font; canvas_draw_str renders UTF-8). Never cut a character in half. */
static size_t utf8_len_at(const char* p) {
    unsigned char ch = (unsigned char)p[0];
    if(ch >= 0xF0) return 4;
    if(ch >= 0xE0) return 3;
    if(ch >= 0xC0) return 2;
    return 1;
}

/* drop a trailing incomplete sequence left by byte-level truncation */
static void utf8_trim(char* s) {
    size_t n = strlen(s);
    size_t i = n;
    while(i > 0 && (((unsigned char)s[i - 1]) & 0xC0) == 0x80)
        i--;
    if(i == 0) {
        s[0] = 0;
        return;
    }
    if(n - (i - 1) < utf8_len_at(&s[i - 1])) s[i - 1] = 0;
}

/* remove the last whole character */
static void utf8_pop(char* s) {
    size_t n = strlen(s);
    if(!n) return;
    do {
        n--;
    } while(n > 0 && (((unsigned char)s[n]) & 0xC0) == 0x80);
    s[n] = 0;
}

static void utf8_fit(Canvas* c, char* s, int max_w) {
    while(s[0] && canvas_string_width(c, s) > max_w)
        utf8_pop(s);
}

/* keep printable ASCII and UTF-8 bytes, drop control chars */
static void clean_copy(char* dst, size_t size, const char* src) {
    size_t o = 0;
    for(size_t i = 0; src && src[i] && o + 1 < size; i++) {
        unsigned char ch = (unsigned char)src[i];
        if(ch >= 0x20 && ch != 0x7f) dst[o++] = (char)ch;
    }
    dst[o] = 0;
    utf8_trim(dst);
}

/* ---- text fonts: every size carries Latin + Cyrillic (u8g2 fonts embedded in the .fap).
 * The size picked in the settings is used for everything below the tab bar; the tab bar and
 * the system panels (update, offline) keep the firmware fonts. All four are monospace. */
typedef struct {
    const uint8_t* font;
    uint8_t cap; // height of capitals and digits: AlignTop puts their top at y
    uint8_t line; // line step for wrapped text and the console
    uint8_t row; // list row height
} TextFont;

static const TextFont text_fonts[] = {
    [FontNormal] = {u8g2_font_uplink_6x12, 7, 9, 10},
    [FontLarge] = {u8g2_font_uplink_7x13, 9, 11, 13},
    [FontSmall] = {u8g2_font_uplink_5x7, 6, 8, 8},
    [FontMicro] = {u8g2_font_uplink_4x6, 5, 7, 7},
};

static const TextFont* text_font(const App* app) {
    uint8_t f = app->settings.font;
    return &text_fonts[f < COUNT_OF(text_fonts) ? f : FontNormal];
}

static void font_text(Canvas* c, const App* app) {
    canvas_set_custom_u8g2_font(c, text_font(app)->font);
}

static int text_width(Canvas* c, const char* s) {
    return canvas_string_width(c, s);
}

/* the first of the texts that fits into w pixels with the current font, else the last one */
static const char* first_fit(Canvas* c, int w, const char* const* texts, size_t count) {
    for(size_t i = 0; i + 1 < count; i++)
        if(text_width(c, texts[i]) <= w) return texts[i];
    return texts[count - 1];
}

/* one UTF-8 character at p: its code point and its length in bytes (always >= 1) */
static size_t utf8_decode(const char* p, uint16_t* cp) {
    const unsigned char* s = (const unsigned char*)p;
    size_t n = 1;
    while(n < utf8_len_at(p) && (s[n] & 0xC0) == 0x80)
        n++;
    if(n == 1) {
        *cp = s[0] < 0x80 ? s[0] : '?';
    } else if(n == 2) {
        *cp = ((s[0] & 0x1F) << 6) | (s[1] & 0x3F);
    } else if(n == 3) {
        *cp = ((s[0] & 0x0F) << 12) | ((s[1] & 0x3F) << 6) | (s[2] & 0x3F);
    } else {
        *cp = '?';
    }
    return n;
}

/* How many bytes of p fit into w pixels with the current font. Prefers to break after a
 * space so words stay whole; *next is where the following line starts. Measures glyph by
 * glyph (one pass), so it is cheap enough to run on every frame. */
static size_t wrap_fit(Canvas* c, const char* p, int w, const char** next) {
    int width = 0;
    size_t n = 0, last_space = 0;
    while(p[n]) {
        uint16_t cp;
        size_t cl = utf8_decode(p + n, &cp);
        int gw = canvas_glyph_width(c, cp);
        if(n > 0 && width + gw > w) break;
        width += gw;
        n += cl;
        if(cp == ' ') last_space = n;
    }
    if(p[n] && p[n] != ' ' && last_space > 0) n = last_space;
    const char* q = p + n;
    while(*q == ' ')
        q++;
    *next = q;
    return n;
}

static void draw_str_fit(Canvas* c, int x, int y, const char* s, int max_w) {
    char buf[128];
    clean_copy(buf, sizeof(buf), s);
    if(canvas_string_width(c, buf) > max_w) {
        char tmp[132];
        while(buf[0]) {
            utf8_pop(buf);
            size_t m = strlen(buf);
            memcpy(tmp, buf, m);
            tmp[m] = '~';
            tmp[m + 1] = 0;
            if(canvas_string_width(c, tmp) <= max_w) break;
        }
        canvas_draw_str_aligned(c, x, y, AlignLeft, AlignTop, buf[0] ? tmp : "~");
        return;
    }
    canvas_draw_str_aligned(c, x, y, AlignLeft, AlignTop, buf);
}

/* Word-wraps text into w pixels and draws lines [skip, skip + lines) from y down.
 * Returns the total number of lines, so a caller can pass lines = 0 to just count. */
static int draw_wrapped(
    Canvas* c,
    int x,
    int y,
    int w,
    const char* text,
    int lines,
    int step,
    int skip) {
    char buf[128];
    const char* p = text;
    while(*p == ' ')
        p++;
    int total = 0, drawn = 0;
    while(*p) {
        const char* next;
        size_t n = wrap_fit(c, p, w, &next);
        if(total >= skip && drawn < lines) {
            if(n >= sizeof(buf)) n = sizeof(buf) - 1;
            memcpy(buf, p, n);
            buf[n] = 0;
            canvas_draw_str_aligned(c, x, y + drawn * step, AlignLeft, AlignTop, buf);
            drawn++;
        }
        total++;
        p = next;
    }
    return total;
}

/* Word-wraps text into w pixels and draws it centred on cx from y down (draw = false only
 * counts the lines). Returns the number of lines. */
static int draw_centered(Canvas* c, int cx, int y, int w, const char* text, int step, bool draw) {
    char buf[64];
    const char* p = text;
    int lines = 0;
    while(*p) {
        const char* next;
        size_t n = wrap_fit(c, p, w, &next);
        while(n && p[n - 1] == ' ')
            n--;
        if(n >= sizeof(buf)) n = sizeof(buf) - 1;
        if(draw) {
            memcpy(buf, p, n);
            buf[n] = 0;
            canvas_draw_str_aligned(c, cx, y + lines * step, AlignCenter, AlignTop, buf);
        }
        lines++;
        p = next;
    }
    return lines;
}

/* a short message in the middle of the area under the tab bar */
static void draw_message(Canvas* c, const App* app, const char* text) {
    int W = canvas_width(c), H = canvas_height(c);
    int step = text_font(app)->line + 1;
    font_text(c, app);
    int lines = draw_centered(c, W / 2, 0, W - 4, text, step, false);
    draw_centered(c, W / 2, 11 + (H - 11 - lines * step) / 2, W - 4, text, step, true);
}

static const char* state_text(char st) {
    switch(st) {
    case 'W':
        return "WORKING";
    case 'A':
        return "NEEDS APPROVAL";
    case 'I':
        return "YOUR TURN";
    case 'E':
        return "ERROR";
    default:
        return "IDLE";
    }
}

static Kind screen_kind(ScreenId s) {
    return s == ScreenClaude ? KindClaude : KindCodex;
}

static Seen* seen_find(App* app, const char* key);

static bool item_attention(const App* app, const Item* it) {
    if(it->state == 'A') return true;
    if(it->state != 'I') return false;
    for(uint8_t i = 0; i < app->seen_n; i++) {
        const Seen* seen = &app->seen[i];
        if(!strcmp(seen->key, it->key)) return !seen->dismissed;
    }
    return true;
}

static bool list_attention(const App* app, Kind kind) {
    const List* l = &app->lists[kind];
    for(uint8_t i = 0; i < l->count; i++)
        if(item_attention(app, &l->items[i])) return true;
    return false;
}

static void dismiss_item(App* app, const Item* it) {
    if(it->state != 'I') return;
    Seen* seen = seen_find(app, it->key);
    if(seen) seen->dismissed = true;
}

static bool item_visible(const App* app, const Item* it) {
    if(it->state == 'S') return false; // quiet/idle sessions do not occupy the list
    if(it->state == 'I') {
        const Seen* seen = seen_find((App*)app, it->key);
        if(seen && seen->dismissed) return false;
    }
    return true;
}

static uint8_t visible_count(const App* app, Kind kind) {
    const List* l = &app->lists[kind];
    uint8_t n = 0;
    for(uint8_t i = 0; i < l->count; i++)
        if(item_visible(app, &l->items[i])) n++;
    return n;
}

static int visible_raw_index(const App* app, Kind kind, uint8_t ordinal) {
    const List* l = &app->lists[kind];
    uint8_t n = 0;
    for(uint8_t i = 0; i < l->count; i++) {
        if(!item_visible(app, &l->items[i])) continue;
        if(n++ == ordinal) return i;
    }
    return -1;
}

static uint8_t visible_ordinal(const App* app, Kind kind, uint8_t raw) {
    const List* l = &app->lists[kind];
    uint8_t n = 0;
    for(uint8_t i = 0; i < l->count && i <= raw; i++)
        if(item_visible(app, &l->items[i])) n++;
    return n ? n - 1 : 0;
}

static void rebuild_tabs(App* app) {
    uint8_t n = 0;
    bool used[ScreenIdCount] = {false};
    for(int i = 0; i < TAB_SLOTS; i++) {
        ScreenId s = app->settings.tabs[i];
        if(s >= ScreenIdCount || s == ScreenOff || used[s]) continue;
        used[s] = true;
        app->tabs[n++] = s;
    }
    if(n == 0) app->tabs[n++] = ScreenSys;
    app->tab_count = n;
    if(app->tab_index >= n) app->tab_index = 0;
}

static ScreenId current_screen(App* app) {
    return app->tabs[app->tab_index];
}

/* ------------------------------------------------------------------ cmd console */
static void cmd_push(App* app, const char* text) {
    Cmd* cmd = &app->cmd;
    uint8_t idx;
    if(cmd->count == CMD_LINES) {
        cmd->head = (cmd->head + 1) % CMD_LINES;
        idx = (cmd->head + cmd->count - 1) % CMD_LINES;
    } else {
        idx = (cmd->head + cmd->count) % CMD_LINES;
        cmd->count++;
    }
    clean_copy(cmd->lines[idx], CMD_COLW, text);
    cmd->scroll = 0;
}

static const char* cmd_line(Cmd* cmd, int visible_index) {
    return cmd->lines[(cmd->head + visible_index) % CMD_LINES];
}

/* ------------------------------------------------------------------ protocol */
static Seen* seen_find(App* app, const char* key) {
    for(uint8_t i = 0; i < app->seen_n; i++)
        if(!strcmp(app->seen[i].key, key)) return &app->seen[i];
    return NULL;
}

static void raise_alert(App* app, Kind kind, const Item* it) {
    app->alert = true;
    app->alert_until = app->tick + ALERT_TICKS;
    app->alert_kind = kind;
    app->alert_state = it->state;
    clean_copy(app->alert_key, sizeof(app->alert_key), it->key);
    clean_copy(app->alert_name, sizeof(app->alert_name), it->name);
    uplink_notify(app, it->state == 'A' ? NotifyApproval : NotifyYourTurn);
}

static void track_attention(App* app, Kind kind, const Item* it) {
    Seen* s = seen_find(app, it->key);
    if(!s) {
        if(app->seen_n == SEEN_MAX) {
            memmove(&app->seen[0], &app->seen[1], sizeof(Seen) * (SEEN_MAX - 1));
            app->seen_n--;
        }
        s = &app->seen[app->seen_n++];
        clean_copy(s->key, sizeof(s->key), it->key);
        s->attn = it->attn;
        s->dismissed = false;
        if(it->state == 'A') raise_alert(app, kind, it);
        return;
    }
    if(it->attn > s->attn) {
        s->attn = it->attn;
        s->dismissed = false;
        if(it->state == 'A' || it->state == 'I') raise_alert(app, kind, it);
    }
}

static int split(char* line, char** f, int max) {
    int n = 0;
    f[n++] = line;
    for(char* p = line; *p && n < max; p++)
        if(*p == '|') {
            *p = 0;
            f[n++] = p + 1;
        }
    return n;
}

static Kind kind_of(const char* s) {
    return (s[0] == 'C') ? KindClaude : KindCodex;
}

static void parse_line(App* app, char* line) {
    if(line[0] == 'R' && line[1] && line[2] == '|') {
        rf_request(app, line); // RL / RR / RA: the RF engine answers on its own thread
        return;
    }
    char* f[12];
    int n = split(line, f, 12);
    switch(f[0][0]) {
    case 'H':
        if(n >= 2) clean_copy(app->host, sizeof(app->host), f[1]);
        app->host_closed = false;
        break;
    case 'S':
        if(n >= 10) {
            Sys* s = &app->sys;
            s->cpu = MIN(100, atoi(f[1]));
            s->ram = MIN(100, atoi(f[2]));
            s->dsk = MIN(100, atoi(f[3]));
            s->rd = strtoul(f[4], NULL, 10);
            s->wr = strtoul(f[5], NULL, 10);
            s->up = strtoul(f[6], NULL, 10);
            s->dn = strtoul(f[7], NULL, 10);
            s->used_mb = strtoul(f[8], NULL, 10);
            s->total_mb = strtoul(f[9], NULL, 10);
            uint32_t net = s->up + s->dn, dsk = s->rd + s->wr;
            s->net_max = MAX(net, s->net_max - s->net_max / 16);
            s->dsk_max = MAX(dsk, s->dsk_max - s->dsk_max / 16);
            if(s->net_max < 64) s->net_max = 64;
            if(s->dsk_max < 256) s->dsk_max = 256;
            if(s->hist_len < HIST) {
                s->cpu_hist[s->hist_len++] = s->cpu;
            } else {
                memmove(s->cpu_hist, s->cpu_hist + 1, HIST - 1);
                s->cpu_hist[HIST - 1] = s->cpu;
            }
            s->valid = true;
        }
        break;
    case 'L':
        if(n >= 3) {
            List* l = &app->lists[kind_of(f[1])];
            l->count = MIN(MAX_ITEMS, atoi(f[2]));
            if(l->cursor >= l->count) l->cursor = l->count ? l->count - 1 : 0;
            if(l->scroll > l->cursor) l->scroll = l->cursor;
        }
        break;
    case 'I':
        if(n >= 11) {
            Kind k = kind_of(f[1]);
            int idx = atoi(f[2]);
            if(idx < 0 || idx >= MAX_ITEMS) break;
            Item* it = &app->lists[k].items[idx];
            clean_copy(it->key, sizeof(it->key), f[3]);
            it->state = f[4][0];
            it->done = atoi(f[5]);
            it->total = atoi(f[6]);
            it->age = strtoul(f[7], NULL, 10);
            it->attn = strtoul(f[8], NULL, 10);
            clean_copy(it->name, sizeof(it->name), f[9]);
            clean_copy(it->detail, sizeof(it->detail), f[10]);
            track_attention(app, k, it);
        }
        break;
    case 'O':
        if(n >= 3 && (uint8_t)atoi(f[1]) == app->cmd.seq) {
            cmd_push(app, f[2]);
            if(current_screen(app) != ScreenCmd) app->cmd.unseen = true;
        }
        break;
    case 'X':
        if(n >= 3 && (uint8_t)atoi(f[1]) == app->cmd.seq) {
            char buf[24];
            app->cmd.running = false;
            app->cmd.have_exit = true;
            app->cmd.exit_code = atoi(f[2]);
            snprintf(buf, sizeof(buf), "[exit %d]", app->cmd.exit_code);
            cmd_push(app, buf);
            if(current_screen(app) != ScreenCmd) app->cmd.unseen = true;
            uplink_notify(app, NotifyCmdDone);
        }
        break;
    case 'W':
        if(n >= 2) clean_copy(app->cmd.cwd, sizeof(app->cmd.cwd), f[1]);
        break;
    case 'N':
        // a newer release is available on the PC side
        if(n >= 2 && ota_newer(f[1], UPLINK_VERSION) && app->ota.state != OtaReceiving &&
           app->ota.state != OtaDone) {
            if(strcmp(app->ota.tag, f[1]) != 0) {
                clean_copy(app->ota.tag, sizeof(app->ota.tag), f[1]);
                app->ota.notified = false;
            }
            app->ota.available = true;
            if(!app->ota.notified) {
                // updates are always automatic: ask for it right away (the companion from
                // 1.2.1 on also pushes it by itself; the request covers older companions)
                app->ota.notified = true;
                ota_request(app);
            }
        }
        break;
    case 'U':
        if(f[0][1] == 'B' && n >= 4) {
            char ack[24];
            if(ota_begin(&app->ota, f[1], strtoul(f[2], NULL, 10), strtoul(f[3], NULL, 10))) {
                uplink_send(app, "UA|0");
            } else {
                snprintf(ack, sizeof(ack), "UA|-1");
                uplink_send(app, ack);
            }
        } else if(f[0][1] == 'D' && n >= 3) {
            int32_t w = ota_chunk(&app->ota, strtoul(f[1], NULL, 10), f[2]);
            char ack[24];
            snprintf(ack, sizeof(ack), "UA|%ld", (long)w);
            uplink_send(app, ack);
        } else if(f[0][1] == 'E') {
            if(ota_finish(&app->ota)) {
                uplink_notify(app, NotifyUpdate);
                app->restart_at = app->tick + 8; // show "updated" for 2 s, then relaunch
            } else {
                uplink_notify(app, NotifyApproval);
            }
        }
        break;
    case 'B':
        if(f[0][1] == 'O') {
            if(n >= 3) {
                app->blackout = atoi(f[1]) != 0;
                app->blackout_auto = (uint8_t)atoi(f[2]);
                app->blackout_sent = 0;
            }
        } else {
            app->host_closed = true;
        }
        break;
    case 'Z':
        if(n >= 2) rf_clock(app, strtoul(f[1], NULL, 10));
        break;
    default:
        break;
    }
}

static void drain_rx(App* app) {
    uint8_t buf[64];
    size_t got;
    while((got = furi_stream_buffer_receive(app->rx, buf, sizeof(buf), 0)) > 0) {
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        app->last_rx_tick = app->tick;
        if(!app->link) {
            app->link = true;
            app->host_closed = false;
            uplink_notify(app, NotifyLink);
            send_version(app);
            rf_send_status(app);
        }
        for(size_t i = 0; i < got; i++) {
            char ch = buf[i];
            if(ch == '\n' || ch == '\r') {
                if(app->line_len) {
                    app->line[app->line_len] = 0;
                    parse_line(app, app->line);
                    app->line_len = 0;
                }
            } else if(app->line_len < LINE_MAX - 1) {
                app->line[app->line_len++] = ch;
            } else {
                app->line_len = 0;
            }
        }
        furi_mutex_release(app->mutex);
    }
}

static void uplink_send(App* app, const char* line) {
    /* Always frame TX messages explicitly.  The Windows side can otherwise see a
     * notification split at the negotiated ATT MTU as several independent protocol
     * lines (especially for a long command typed on the Flipper keyboard).  A trailing
     * newline is cheap, and the RX side already treats CR/LF as the line delimiter. */
    if(!app->profile || !line) return;
    char framed[UPLINK_TX_MAX + 1];
    size_t n = strlen(line);
    if(n >= sizeof(framed)) n = sizeof(framed) - 2;
    memcpy(framed, line, n);
    framed[n++] = '\n';
    framed[n] = 0;
    uplink_ble_tx(app->profile, (const uint8_t*)framed, n);
}

static void send_version(App* app) {
    uplink_send(app, "V|" UPLINK_VERSION);
    app->ver_tick = app->tick;
}

/* ask the PC to stream the newer release */
static void ota_request(App* app) {
    if(!app->ota.available || app->ota.state == OtaReceiving) return;
    char line[32];
    snprintf(line, sizeof(line), "U|%s", app->ota.tag);
    app->ota.state = OtaRequested;
    app->ota_req_tick = app->tick;
    app->ota.error[0] = 0;
    app->alert = false;
    app->alert_update = false;
    uplink_send(app, line);
}

/* ------------------------------------------------------------------ RF Hunter glue
 * The engine (rf_engine.c) runs its own thread. Its reply lines come back through a stream
 * buffer and are sent from this thread; its status is copied into app->rf_status. */
static void rf_reply_callback(const char* line, void* context) {
    App* app = context;
    size_t n = strlen(line);
    // a reply that does not fit is dropped: the PC asks again after its timeout
    if(app->rf_closing || furi_stream_buffer_spaces_available(app->rf_tx) < n + 1) return;
    furi_stream_buffer_send(app->rf_tx, line, n, 0);
    furi_stream_buffer_send(app->rf_tx, "\n", 1, 0);
    if(!app->rf_tx_posted) {
        app->rf_tx_posted = true;
        view_dispatcher_send_custom_event(app->views, EvRfTx);
    }
}

static void rf_changed_callback(void* context) {
    App* app = context;
    if(app->rf_closing || app->rf_event_posted) return;
    app->rf_event_posted = true;
    view_dispatcher_send_custom_event(app->views, EvRf);
}

static void rf_apply_config(App* app) {
    if(!app->rf) return;
    const UplinkSettings* s = &app->settings;
    RfConfig cfg = {
        .band = s->rf_band,
        .rssi_threshold_dbm = s->rf_rssi,
        .dwell_ms = s->rf_dwell_ms,
        .capture_ms = s->rf_capture_ms,
        .silence_us = 8000,
        .feedback = s->rf_feedback,
        .geiger = s->rf_geiger,
        .keep_uploaded = s->rf_keep,
        .tz_offset_minutes = s->rf_tz,
    };
    rf_engine_configure(app->rf, &cfg);
}

/* the engine exists while the RF tab is enabled, so the PC can import records at any time */
static void rf_lifecycle(App* app) {
    bool wanted = false;
    for(uint8_t i = 0; i < app->tab_count; i++)
        if(app->tabs[i] == ScreenRf) wanted = true;
    if(wanted && !app->rf) {
        app->rf = rf_engine_alloc(rf_reply_callback, rf_changed_callback, app);
        rf_apply_config(app);
        if(app->settings.rf_autostart) rf_engine_start(app->rf);
    } else if(!wanted && app->rf) {
        rf_engine_free(app->rf);
        app->rf = NULL;
        memset(&app->rf_status, 0, sizeof(app->rf_status));
    }
}

static void rf_send_status(App* app) {
    if(!app->rf || !app->link) return;
    char line[64];
    rf_engine_status_line(app->rf, line, sizeof(line));
    uplink_send(app, line);
    app->rf_status_tick = app->tick;
    app->rf_status_listed = app->rf_status.listed;
    app->rf_status_carry = app->rf_status.carry;
}

static void rf_refresh_status(App* app) {
    if(!app->rf) return;
    rf_engine_get_status(app->rf, &app->rf_status);
    if(app->rf_status.unseen && app->tabs[app->tab_index] == ScreenRf && !app->alert)
        rf_engine_mark_seen(app->rf); // the user is looking at the RF tab
    if(app->link && (app->rf_status.listed != app->rf_status_listed ||
                     app->rf_status.carry != app->rf_status_carry ||
                     app->tick - app->rf_status_tick > RF_STATUS_TICKS))
        rf_send_status(app);
}

/* Like the missed-call light of a phone: while RF events wait to be looked at, a short cyan blink
 * every few seconds (with "RF vibrate on signal"); opening the RF tab stops it. */
static const NotificationSequence rf_remind_sequence = {
    &message_green_255,
    &message_blue_255,
    &message_delay_25,
    &message_green_0,
    &message_blue_0,
    NULL,
};

static void rf_remind(App* app) {
    if(!app->rf || !app->rf_status.unseen || !app->settings.rf_feedback) return;
    if(app->tick % RF_REMIND_TICKS) return;
    notification_message(app->notifications, &rf_remind_sequence);
}

static void rf_request(App* app, const char* line) {
    if(!app->rf || !app->settings.rf_sync) {
        uplink_send(app, "RX|off|RF sync is off on the Flipper");
        return;
    }
    rf_engine_request(app->rf, line);
}

static void rf_drain_tx(App* app) {
    uint8_t buf[64];
    size_t got;
    while((got = furi_stream_buffer_receive(app->rf_tx, buf, sizeof(buf), 0)) > 0) {
        for(size_t i = 0; i < got; i++) {
            char ch = buf[i];
            if(ch == '\n') {
                app->rf_line[app->rf_line_len] = 0;
                if(app->rf_line_len) uplink_send(app, app->rf_line);
                app->rf_line_len = 0;
            } else if(app->rf_line_len < sizeof(app->rf_line) - 1) {
                app->rf_line[app->rf_line_len++] = ch;
            }
        }
    }
}

/* Z|utc: the PC's clock. Our RTC keeps local time; the difference turns RF timestamps into
 * UTC and is saved, so records stay right when the PC is away. */
static void rf_clock(App* app, uint32_t utc) {
    if(utc < 1600000000u) return;
    int64_t diff = (int64_t)furi_hal_rtc_get_timestamp() - (int64_t)utc;
    int32_t minutes = (int32_t)((diff + (diff >= 0 ? 30 : -30)) / 60);
    if(minutes < -14 * 60 || minutes > 14 * 60) return; // the RTC is not set: keep what we had
    if(minutes == app->settings.rf_tz) return;
    app->settings.rf_tz = (int16_t)minutes;
    uplink_settings_save(&app->settings);
    rf_apply_config(app);
}

static void uplink_rx_callback(const uint8_t* data, uint16_t size, void* context) {
    App* app = context;
    furi_stream_buffer_send(app->rx, data, size, 0);
    // one pending EvRx is enough (drain_rx empties the buffer); never block the BLE thread
    if(!app->rx_posted && !app->rf_closing) {
        app->rx_posted = true;
        view_dispatcher_send_custom_event(app->views, EvRx);
    }
}

static void bt_status_callback(BtStatus status, void* context) {
    UNUSED(status);
    UNUSED(context);
}

/* ------------------------------------------------------------------ drawing
 * Every screen works in both orientations: horizontal 128x64 and vertical 64x128 (the view
 * dispatcher rotates the canvas, so all layout below derives from canvas_width/height). */
static bool narrow(Canvas* c) {
    return canvas_width(c) < 100;
}

static bool tab_attention(App* app, ScreenId sid) {
    if(sid == ScreenCodex || sid == ScreenClaude) return list_attention(app, screen_kind(sid));
    if(sid == ScreenCmd) return app->cmd.unseen || app->cmd.running;
    if(sid == ScreenRf) return app->rf_status.unseen || app->rf_status.storage_full;
    return false;
}

/* three signal bars, 8x7 at (x, 1): all lit = data flowing, one = link is lagging */
static void draw_link_bars(Canvas* c, App* app, int x) {
    bool lag = app->tick - app->last_rx_tick > LAG_TICKS;
    for(int i = 0; i < 3; i++) {
        int h = 3 + i * 2;
        if(i == 0 || !lag)
            canvas_draw_box(c, x + i * 3, 8 - h, 2, h);
        else
            canvas_draw_dot(c, x + i * 3, 7);
    }
}

static const char* const tab_label[ScreenIdCount] = {"SYS", "CDX", "CLD", "CMD", "", "RF"};

static void draw_header(Canvas* c, App* app) {
    int W = canvas_width(c);
    fg(c);
    canvas_draw_box(c, 0, 0, W, 10);
    canvas_set_font(c, FontSecondary);
    if(narrow(c)) {
        // current tab in a lit box, link bars, then one marker per tab (filled = current)
        const char* lab = tab_label[current_screen(app)];
        int lw = canvas_string_width(c, lab) + 4;
        bg(c);
        canvas_draw_box(c, 1, 1, lw, 8);
        fg(c);
        canvas_draw_str_aligned(c, 3, 1, AlignLeft, AlignTop, lab);
        bg(c);
        draw_link_bars(c, app, lw + 4);
        for(int i = 0; i < app->tab_count; i++) {
            int x = W - 2 - (app->tab_count - i) * 6 + 1;
            bool attn = i != app->tab_index && tab_attention(app, app->tabs[i]);
            if(i == app->tab_index || (attn && (app->tick & 2)))
                canvas_draw_box(c, x, 3, 4, 4);
            else
                canvas_draw_frame(c, x, 3, 4, 4);
        }
        fg(c);
        return;
    }
    // each tab is as wide as its label plus an even share of the free space
    int setw = canvas_string_width(c, "SET");
    int avail = W - setw - 15, labels = 0;
    for(int i = 0; i < app->tab_count; i++)
        labels += canvas_string_width(c, tab_label[app->tabs[i]]);
    int pad = MIN(14, (avail - labels) / MAX(1, app->tab_count));
    int x = 1;
    for(int i = 0; i < app->tab_count; i++) {
        ScreenId sid = app->tabs[i];
        int tabw = canvas_string_width(c, tab_label[sid]) + pad;
        bool active = (i == app->tab_index);
        bool attn = tab_attention(app, sid);
        bool lit = active || (attn && (app->tick & 2));
        if(lit) {
            bg(c);
            canvas_draw_box(c, x, 1, tabw - 1, 8);
        }
        canvas_set_color(c, lit ? g_fg : g_bg); // lit: dark text on light box; else light on dark bar
        canvas_draw_str_aligned(c, x + tabw / 2, 1, AlignCenter, AlignTop, tab_label[sid]);
        if(attn && !active && pad >= 12)
            canvas_draw_str_aligned(c, x + tabw - 3, 1, AlignCenter, AlignTop, "!");
        x += tabw;
    }
    bg(c);
    draw_link_bars(c, app, W - 2 - setw - 11);
    // Right on the last tab opens the settings (as does holding OK)
    canvas_draw_str_aligned(c, W - 2, 1, AlignRight, AlignTop, "SET");
    fg(c);
}

static void draw_meter(Canvas* c, int x, int y, int w, int h, uint8_t pct) {
    canvas_draw_frame(c, x, y, w, h);
    int fill = (w - 2) * (pct > 100 ? 100 : pct) / 100;
    if(fill > 0) canvas_draw_box(c, x + 1, y + 1, fill, h - 2);
    canvas_set_color(c, ColorXOR);
    for(int t = 1; t < 4; t++)
        canvas_draw_dot(c, x + t * w / 4, y + h / 2);
    fg(c);
}

static void draw_cpu_graph(Canvas* c, const Sys* s, int x, int y, int w, int h, int step) {
    canvas_draw_line(c, x, y + h, x + w - 1, y + h);
    int n = MIN((int)s->hist_len, w / step);
    for(int i = 0; i < n; i++) {
        int px = x + (w / step - n + i) * step;
        int v = s->cpu_hist[s->hist_len - n + i] * h / 100;
        if(v > 0) canvas_draw_line(c, px, y + h - v, px, y + h);
    }
    for(int px = x; px < x + w; px += 4)
        canvas_draw_dot(c, px, y);
}

static void draw_sys(Canvas* c, App* app) {
    Sys* s = &app->sys;
    int W = canvas_width(c), H = canvas_height(c);
    const TextFont* tf = text_font(app);
    int cap = tf->cap;
    if(!s->valid) {
        draw_message(c, app, "waiting for data...");
        return;
    }
    font_text(c, app);
    uint32_t net = s->up + s->dn, dsk = s->rd + s->wr;
    static const char* const labels[4] = {"CPU", "RAM", "NET", "DSK"};
    uint8_t pcts[4] = {
        s->cpu, s->ram, (uint8_t)(net * 100 / s->net_max), (uint8_t)(dsk * 100 / s->dsk_max)};
    char vals[4][12];
    snprintf(vals[0], sizeof(vals[0]), "%u%%", s->cpu);
    snprintf(vals[1], sizeof(vals[1]), "%u%%", s->ram);
    fmt_rate(vals[2], sizeof(vals[2]), net);
    fmt_rate(vals[3], sizeof(vals[3]), dsk);
    int top = 12, graph = top; // graph: where the CPU history starts

    if(app->settings.indicators == IndicatorsBars) {
        if(narrow(c)) {
            // label and value on one line, the meter under them
            int mh = cap < 7 ? 5 : 7, pitch = cap + 2 + mh + 3;
            for(int i = 0; i < 4; i++) {
                int y = top + i * pitch;
                canvas_draw_str_aligned(c, 1, y, AlignLeft, AlignTop, labels[i]);
                canvas_draw_str_aligned(c, W - 1, y, AlignRight, AlignTop, vals[i]);
                draw_meter(c, 1, y + cap + 2, W - 2, mh, pcts[i]);
            }
            graph = top + 4 * pitch;
        } else if(top + 4 * (cap + 3) + 8 <= H) {
            // one row per value: label, meter, value
            int lw = text_width(c, "CPU"), vw = text_width(c, "100%"), mh = cap + 2;
            for(int i = 0; i < 4; i++) {
                int y = top + i * (mh + 1);
                canvas_draw_str_aligned(c, 2, y + 1, AlignLeft, AlignTop, labels[i]);
                draw_meter(c, lw + 6, y, W - lw - vw - 11, mh, pcts[i]);
                canvas_draw_str_aligned(c, W - 1, y + 1, AlignRight, AlignTop, vals[i]);
            }
            graph = top + 4 * (mh + 1) + 1;
        } else {
            // large text: two columns, a thin meter under each label and value
            int cw = (W - 10) / 2, pitch = cap + 2 + 5 + 3;
            for(int i = 0; i < 4; i++) {
                int x = (i % 2) ? W - 2 - cw : 2, y = top + (i / 2) * pitch;
                canvas_draw_str_aligned(c, x, y, AlignLeft, AlignTop, labels[i]);
                canvas_draw_str_aligned(c, x + cw, y, AlignRight, AlignTop, vals[i]);
                draw_meter(c, x, y + cap + 2, cw, 5, pcts[i]);
            }
            graph = top + 2 * pitch;
        }
    } else {
        char up[12], dn[12], rd[12], wr[12];
        fmt_rate(up, sizeof(up), s->up);
        fmt_rate(dn, sizeof(dn), s->dn);
        fmt_rate(rd, sizeof(rd), s->rd);
        fmt_rate(wr, sizeof(wr), s->wr);
        if(narrow(c)) {
            // label left, value right; NET/DSK spelled out while the values fit next to them
            const char* lab[6] = {"CPU", "RAM", "NET", "", "DSK", ""};
            char v[6][16];
            snprintf(v[0], sizeof(v[0]), "%u%%", s->cpu);
            snprintf(v[1], sizeof(v[1]), "%u%%", s->ram);
            snprintf(v[2], sizeof(v[2]), "up %s", up);
            snprintf(v[3], sizeof(v[3]), "dn %s", dn);
            snprintf(v[4], sizeof(v[4]), "rd %s", rd);
            snprintf(v[5], sizeof(v[5]), "wr %s", wr);
            bool fits = true;
            for(int i = 2; i < 6; i++)
                if(text_width(c, "NET") + text_width(c, v[i]) + 4 > W - 2) fits = false;
            if(!fits) {
                static const char* const brief[4] = {"UP", "DN", "RD", "WR"};
                const char* rates[4] = {up, dn, rd, wr};
                for(int i = 0; i < 4; i++) {
                    lab[2 + i] = brief[i];
                    snprintf(v[2 + i], sizeof(v[2 + i]), "%s", rates[i]);
                }
            }
            int pitch = tf->line + 1;
            for(int i = 0; i < 6; i++) {
                canvas_draw_str_aligned(c, 1, top + i * pitch, AlignLeft, AlignTop, lab[i]);
                canvas_draw_str_aligned(c, W - 1, top + i * pitch, AlignRight, AlignTop, v[i]);
            }
            graph = top + 6 * pitch + 2;
        } else {
            char l[3][40];
            snprintf(l[0], sizeof(l[0]), "CPU %u%%   RAM %u%%", s->cpu, s->ram);
            snprintf(l[1], sizeof(l[1]), "NET up %s  dn %s", up, dn);
            snprintf(l[2], sizeof(l[2]), "DSK rd %s  wr %s", rd, wr);
            bool fits = true;
            for(int i = 0; i < 3; i++)
                if(text_width(c, l[i]) > W - 3) fits = false;
            if(!fits) {
                snprintf(l[0], sizeof(l[0]), "CPU %u%% RAM %u%%", s->cpu, s->ram);
                snprintf(l[1], sizeof(l[1]), "UP %s DN %s", up, dn);
                snprintf(l[2], sizeof(l[2]), "RD %s WR %s", rd, wr);
            }
            int pitch = tf->line + 2;
            for(int i = 0; i < 3; i++)
                canvas_draw_str_aligned(c, 2, top + 1 + i * pitch, AlignLeft, AlignTop, l[i]);
            graph = top + 1 + 3 * pitch;
        }
    }
    int gh = H - 1 - graph;
    if(gh >= 4) {
        if(narrow(c))
            draw_cpu_graph(c, s, 1, graph, W - 2, gh, 1);
        else
            draw_cpu_graph(c, s, 2, graph, W - 4, gh, 2);
    }
}

/* 7x7 state glyph; `big` allows the 9x9 blinking box of "needs approval" */
static void draw_glyph(Canvas* c, int x, int y, char st, uint32_t tick, bool big) {
    switch(st) {
    case 'W': {
        static const int8_t dx[4][4] = {{3, 0, 3, 6}, {0, 0, 6, 6}, {0, 3, 6, 3}, {0, 6, 6, 0}};
        const int8_t* d = dx[(tick / 2) % 4];
        canvas_draw_line(c, x + d[0], y + d[1], x + d[2], y + d[3]);
        break;
    }
    case 'A':
        if(tick & 2) {
            if(big)
                canvas_draw_box(c, x - 1, y - 1, 9, 9);
            else
                canvas_draw_box(c, x, y, 7, 7);
            bg(c);
        }
        canvas_draw_line(c, x + 3, y + 1, x + 3, y + 3);
        canvas_draw_dot(c, x + 3, y + 5);
        fg(c);
        break;
    case 'I':
        canvas_draw_line(c, x, y, x + 3, y + 3);
        canvas_draw_line(c, x + 3, y + 3, x, y + 6);
        if(tick & 2) canvas_draw_line(c, x + 4, y + 6, x + 7, y + 6);
        break;
    case 'E':
        canvas_draw_line(c, x, y, x + 6, y + 6);
        canvas_draw_line(c, x + 6, y, x, y + 6);
        break;
    default:
        canvas_draw_frame(c, x + 2, y + 2, 3, 3);
        break;
    }
}

static void item_info(const Item* it, char* out, size_t size) {
    if(it->total)
        snprintf(out, size, "%u/%u", it->done, it->total);
    else
        fmt_age(out, size, it->age);
}

static void draw_list(Canvas* c, App* app, Kind k) {
    List* l = &app->lists[k];
    int W = canvas_width(c), H = canvas_height(c);
    const TextFont* tf = text_font(app);
    bool two_lines = narrow(c); // vertical: name, then age/progress and what it is doing
    int rh = tf->row + (two_lines ? tf->line : 0);
    int rows = (H - 11) / rh;
    uint8_t total = visible_count(app, k);
    if(l->cursor >= total) l->cursor = total ? total - 1 : 0;
    if(!total) {
        draw_message(c, app, "NO ACTIVE SESSIONS");
        return;
    }
    font_text(c, app);
    if(l->cursor < l->scroll) l->scroll = l->cursor;
    if(l->cursor >= l->scroll + rows) l->scroll = l->cursor - rows + 1;
    if(l->scroll + rows > total) l->scroll = total > rows ? total - rows : 0;
    bool bar = total > rows;
    int right_edge = bar ? W - 5 : W - 2;
    int ty = (tf->row - tf->cap - 1) / 2; // text inside a row
    for(int r = 0; r < rows && l->scroll + r < total; r++) {
        int i = visible_raw_index(app, k, l->scroll + r);
        if(i < 0) break;
        Item* it = &l->items[i];
        int y = 11 + r * rh;
        char info[12];
        item_info(it, info, sizeof(info));
        int iw = text_width(c, info);
        draw_glyph(c, 2, y + (tf->row - 7) / 2, it->state, app->tick, tf->row >= 10);
        if(two_lines) {
            draw_str_fit(c, 12, y + ty, it->name, right_edge - 12);
            canvas_draw_str_aligned(c, right_edge, y + tf->row, AlignRight, AlignTop, info);
            draw_str_fit(c, 12, y + tf->row, it->detail, right_edge - iw - 15);
        } else {
            canvas_draw_str_aligned(c, right_edge, y + ty, AlignRight, AlignTop, info);
            draw_str_fit(c, 12, y + ty, it->name, right_edge - iw - 15);
        }
        if(l->scroll + r == l->cursor) {
            canvas_set_color(c, ColorXOR);
            canvas_draw_box(c, 0, y, right_edge + 2, rh);
            fg(c);
        }
    }
    if(bar) {
        int track = rh * rows;
        int h = MAX(3, track * rows / total);
        int y = 11 + (track - h) * l->scroll / (total - rows);
        canvas_draw_line(c, W - 2, 11, W - 2, 11 + track - 1);
        canvas_draw_box(c, W - 3, y, 3, h);
    }
}

static void draw_detail(Canvas* c, App* app, Kind k) {
    List* l = &app->lists[k];
    int W = canvas_width(c), H = canvas_height(c);
    const TextFont* tf = text_font(app);
    int raw = l->cursor < visible_count(app, k) ? visible_raw_index(app, k, l->cursor) : -1;
    if(raw < 0) {
        app->detail = false;
        return;
    }
    Item* it = &l->items[raw];
    int y = 11;
    font_text(c, app);
    draw_str_fit(c, 2, y + 1, it->name, W - 4);
    y += tf->line + 2;

    // state in a lit box, the age right of it (under it when there is no room)
    const char* st = state_text(it->state);
    int sw = text_width(c, st), bh = tf->cap + 4;
    canvas_draw_box(c, 2, y, sw + 6, bh);
    bg(c);
    canvas_draw_str_aligned(c, 5, y + 2, AlignLeft, AlignTop, st);
    fg(c);
    char age[16], buf[24];
    fmt_age(age, sizeof(age), it->age);
    snprintf(buf, sizeof(buf), "%s ago", age);
    if(sw + 12 + text_width(c, buf) <= W) {
        canvas_draw_str_aligned(c, W - 1, y + 2, AlignRight, AlignTop, buf);
        y += bh + 2;
    } else {
        y += bh + 2;
        canvas_draw_str_aligned(c, W - 1, y, AlignRight, AlignTop, buf);
        y += tf->line + 1;
    }
    if(it->total) {
        snprintf(buf, sizeof(buf), "%u/%u", it->done, it->total);
        int tw = text_width(c, buf);
        int bw = W - tw - 7, ph = MAX(5, tf->cap);
        canvas_draw_frame(c, 2, y, bw, ph);
        int w = (bw - 2) * MIN(it->done, it->total) / it->total;
        if(w) canvas_draw_box(c, 3, y + 1, w, ph - 2);
        canvas_draw_str_aligned(c, W - 1, y + (ph - tf->cap) / 2, AlignRight, AlignTop, buf);
        y += ph + 3;
    }
    int rows = (H - y) / tf->line;
    if(rows <= 0) return;
    int total = draw_wrapped(c, 2, y, W - 6, it->detail, 0, tf->line, 0);
    int max_scroll = total > rows ? total - rows : 0;
    if(l->detail_scroll > max_scroll) l->detail_scroll = max_scroll;
    draw_wrapped(c, 2, y, W - 6, it->detail, rows, tf->line, l->detail_scroll);
    if(max_scroll) {
        // a thin scrollbar on the right edge of the text block
        int track = H - y - 1;
        int h = MAX(3, track * rows / total);
        int py = y + (track - h) * l->detail_scroll / max_scroll;
        canvas_draw_line(c, W - 2, y, W - 2, H - 2);
        canvas_draw_box(c, W - 3, py, 3, h);
    }
}

/* screen rows a console line takes once wrapped (at least 1) */
static int console_rows(Canvas* c, const char* p, int w) {
    int n = 0;
    do {
        const char* next = p;
        if(*p) wrap_fit(c, p, w, &next);
        p = next;
        n++;
    } while(*p);
    return n;
}

/* Console lines are word-wrapped to the screen width. Like a terminal, output starts at the
 * top and, once the screen is full, the newest line stays at the bottom; Up/Down scroll by
 * stored lines. */
static void draw_console(Canvas* c, App* app, int top, int bottom, int w) {
    Cmd* cmd = &app->cmd;
    int step = text_font(app)->line;
    int capacity = (bottom - top) / step;
    int newest = cmd->count - 1 - cmd->scroll;
    int shown = 0;
    font_text(c, app);
    int used = 0;
    for(int i = newest; i >= 0 && used < capacity; i--)
        used += console_rows(c, cmd_line(cmd, i), w);
    int y = used < capacity ? top + used * step : bottom;
    for(int i = newest; i >= 0 && y - step >= top; i--) {
        const char* p = cmd_line(cmd, i);
        const char* seg[24];
        uint8_t len[24];
        int n = 0;
        do {
            const char* next = p;
            seg[n] = p;
            len[n] = *p ? (uint8_t)wrap_fit(c, p, w, &next) : 0;
            p = next;
            n++;
        } while(*p && n < 24);
        for(int s = n - 1; s >= 0 && y - step >= top; s--) {
            char buf[CMD_COLW];
            y -= step;
            memcpy(buf, seg[s], len[s]);
            buf[len[s]] = 0;
            canvas_draw_str_aligned(c, 1, y, AlignLeft, AlignTop, buf);
        }
        shown++;
    }
    if(cmd->count > shown) {
        int track = bottom - top;
        int h = MAX(3, track * shown / cmd->count);
        int span = cmd->count - shown;
        int pos = span - MIN(cmd->scroll, span); // 0 = oldest at the top
        int py = top + (track - h) * pos / span;
        canvas_draw_box(c, w + 2, py, 2, h);
    }
}

static void draw_cmd(Canvas* c, App* app) {
    Cmd* cmd = &app->cmd;
    int W = canvas_width(c), H = canvas_height(c);
    const TextFont* tf = text_font(app);
    bool n = narrow(c);
    font_text(c, app);
    char status[24] = "";
    if(cmd->running) {
        static const char* const run[] = {"RUN Back=stop", "RUN"};
        snprintf(status, sizeof(status), "%s", n ? "RUN" : first_fit(c, W / 2, run, 2));
    } else if(cmd->have_exit)
        snprintf(status, sizeof(status), "exit %d", cmd->exit_code);
    int sw = status[0] ? text_width(c, status) + 4 : 0;
    canvas_draw_str_aligned(c, W - 1, 11, AlignRight, AlignTop, status);
    // working directory: keep its end, it is the informative part
    char cwd[64];
    clean_copy(cwd, sizeof(cwd), cmd->cwd[0] ? cmd->cwd : "cmd");
    const char* shown = cwd;
    char head[72];
    for(;;) {
        snprintf(head, sizeof(head), "%s%s>", shown == cwd ? "" : "~", shown);
        if(!shown[0] || text_width(c, head) <= W - 2 - sw) break;
        shown += utf8_len_at(shown);
    }
    canvas_draw_str_aligned(c, 1, 11, AlignLeft, AlignTop, head);
    int sep = 11 + tf->line + 1;
    canvas_draw_line(c, 0, sep, W, sep);

    if(cmd->count == 0) {
        int step = tf->line + 1;
        int y = sep + 2 + (H - sep - 2) / 4;
        y += step * draw_centered(c, W / 2, y, W - 4, "OK: type a command", step, true) + 3;
        draw_centered(c, W / 2, y, W - 4, n ? "on the PC" : "cmd.exe in your pocket", step, true);
        return;
    }
    draw_console(c, app, sep + 2, H, W - 5);
}

/* a framed panel with an inverted title bar; returns the y below the title */
static int draw_panel(Canvas* c, int x, int y, int w, int h, const char* title) {
    bg(c);
    canvas_draw_box(c, x, y, w, h);
    fg(c);
    canvas_draw_frame(c, x, y, w, h);
    canvas_draw_box(c, x + 2, y + 2, w - 4, 11);
    canvas_set_font(c, FontSecondary);
    bg(c);
    canvas_draw_str_aligned(c, x + w / 2, y + 4, AlignCenter, AlignTop, title);
    fg(c);
    return y + 15;
}

static void draw_alert(Canvas* c, App* app) {
    int W = canvas_width(c), H = canvas_height(c);
    bool n = narrow(c);
    if(app->alert_update) {
        int pw = W - 8, ph = n ? 70 : 42;
        int px = 4, py = n ? (H - ph) / 2 : 12;
        char line[40];
        int y = draw_panel(c, px, py, pw, ph, n ? "UPDATE" : ">> UPDATE AVAILABLE <<");
        snprintf(line, sizeof(line), n ? "%s ->" : "%s -> %s", UPLINK_VERSION, app->ota.tag);
        canvas_draw_str_aligned(c, W / 2, y + 1, AlignCenter, AlignTop, line);
        if(n) {
            canvas_draw_str_aligned(c, W / 2, y + 11, AlignCenter, AlignTop, app->ota.tag);
            canvas_draw_str_aligned(c, W / 2, y + 27, AlignCenter, AlignTop, "OK: install");
            canvas_draw_str_aligned(c, W / 2, y + 37, AlignCenter, AlignTop, "Back: later");
        } else {
            canvas_draw_str_aligned(c, W / 2, y + 12, AlignCenter, AlignTop, "OK: install   Back: later");
        }
        return;
    }
    // the panel grows with the text size: title bar, CODEX/CLAUDE, the session name
    const TextFont* tf = text_font(app);
    char name[64];
    clean_copy(name, sizeof(name), app->alert_name);
    int px = n ? 2 : 4, pw = W - 2 * px; // narrow: every pixel counts for the wrapped name
    font_text(c, app);
    int lines = n ? MAX(1, MIN(3, draw_wrapped(c, 0, 0, pw - 4, name, 0, tf->line, 0))) : 1;
    int ph = 15 + tf->line + 3 + lines * tf->line + 5;
    int py = n ? (H - ph) / 2 : 12;
    const char* title = app->alert_state == 'A' ? (n ? "APPROVAL" : "!! APPROVAL NEEDED !!") :
                                                  (n ? "YOUR TURN" : ">> YOUR TURN <<");
    int y = draw_panel(c, px, py, pw, ph, title);
    font_text(c, app);
    canvas_draw_str_aligned(
        c, W / 2, y + 1, AlignCenter, AlignTop, app->alert_kind == KindCodex ? "CODEX" : "CLAUDE");
    y += tf->line + 3;
    if(n) {
        draw_wrapped(c, px + 2, y, pw - 4, name, 3, tf->line, 0);
    } else {
        utf8_fit(c, name, pw - 10);
        canvas_draw_str_aligned(c, W / 2, y, AlignCenter, AlignTop, name);
    }
}

static void draw_ota(Canvas* c, App* app) {
    Ota* o = &app->ota;
    int W = canvas_width(c), H = canvas_height(c);
    bool n = narrow(c);
    int pw = W - 6, ph = n ? 56 : 44;
    int px = 3, py = n ? (H - ph) / 2 : 11;
    char line[48];
    const char* title = o->state == OtaDone   ? (n ? "UPDATED" : "UPDATED - RESTARTING") :
                        o->state == OtaFailed ? (n ? "FAILED" : "UPDATE FAILED") :
                                                "UPDATING";
    int y = draw_panel(c, px, py, pw, ph, title);
    if(o->state == OtaFailed) {
        canvas_draw_str_aligned(c, W / 2, y + 2, AlignCenter, AlignTop, o->error[0] ? o->error : "error");
        canvas_draw_str_aligned(c, W / 2, y + 14, AlignCenter, AlignTop, n ? "any key" : "any key: close");
        return;
    }
    snprintf(line, sizeof(line), n ? "-> %s" : "%s -> %s", n ? o->tag : UPLINK_VERSION, o->tag);
    canvas_draw_str_aligned(c, W / 2, y + 1, AlignCenter, AlignTop, line);
    uint8_t pct = (o->state == OtaRequested) ? 0 : ota_percent(o);
    int by = y + (n ? 24 : 13);
    canvas_draw_frame(c, px + 5, by, pw - 10, 9);
    int w = (pw - 12) * pct / 100;
    if(w) canvas_draw_box(c, px + 6, by + 1, w, 7);
    if(o->state == OtaRequested)
        snprintf(line, sizeof(line), n ? "wait PC" : "waiting for PC...");
    else
        snprintf(line, sizeof(line), "%u%%", pct);
    canvas_set_color(c, ColorXOR); // readable over both the filled and the empty part
    canvas_draw_str_aligned(c, W / 2, by + 1, AlignCenter, AlignTop, line);
    fg(c);
}

static void draw_offline(Canvas* c, App* app) {
    int W = canvas_width(c), H = canvas_height(c);
    bool n = narrow(c);
    canvas_draw_box(c, 0, 0, W, 12);
    bg(c);
    canvas_set_font(c, FontPrimary);
    canvas_draw_str_aligned(c, W / 2, 2, AlignCenter, AlignTop, n ? "UPLINK" : "DEDSEC // UPLINK");
    fg(c);
    canvas_set_font(c, FontSecondary);
    const char* state = app->host_closed ? "HOST WENT DARK" : "WAITING FOR HOST";
    if(n) {
        canvas_draw_icon(c, (W - 26) / 2, 15, &I_hood_26x30);
        int y = 49;
        canvas_draw_str_aligned(c, W / 2, y, AlignCenter, AlignTop, app->host_closed ? "HOST WENT" : "WAITING");
        canvas_draw_str_aligned(c, W / 2, y + 9, AlignCenter, AlignTop, app->host_closed ? "DARK" : "FOR HOST");
        canvas_draw_str_aligned(c, W / 2, y + 23, AlignCenter, AlignTop, "BLE name:");
        canvas_draw_str_aligned(c, W / 2, y + 32, AlignCenter, AlignTop, UPLINK_NAME_PREFIX);
        canvas_draw_str_aligned(c, W / 2, y + 41, AlignCenter, AlignTop, furi_hal_version_get_name_ptr());
        canvas_draw_str_aligned(c, W / 2, y + 55, AlignCenter, AlignTop, "run uplink");
        canvas_draw_str_aligned(c, W / 2, y + 64, AlignCenter, AlignTop, "on the PC");
    } else {
        char name[32];
        snprintf(name, sizeof(name), "%s %s", UPLINK_NAME_PREFIX, furi_hal_version_get_name_ptr());
        canvas_draw_icon(c, 2, 16, &I_hood_26x30);
        canvas_draw_str_aligned(c, 32, 17, AlignLeft, AlignTop, state);
        canvas_draw_str_aligned(c, 32, 27, AlignLeft, AlignTop, "BLE name:");
        canvas_draw_str_aligned(c, 32, 36, AlignLeft, AlignTop, name);
        canvas_draw_str_aligned(c, 32, 46, AlignLeft, AlignTop, "run uplink on PC");
    }
    int span = W - 16;
    int pos = (app->tick * 3) % (span * 2);
    int x = pos < span ? pos : span * 2 - 1 - pos;
    canvas_draw_frame(c, 2, H - 6, W - 4, 5);
    canvas_draw_box(c, 3 + x, H - 5, 10, 3);
}

/* Blackout over the SYS tab: the question, the wait for the PC, or the dark state. The
 * panel grows with the text size like the alert banner. */
static void draw_blackout(Canvas* c, App* app) {
    int W = canvas_width(c), H = canvas_height(c);
    bool n = narrow(c);
    const TextFont* tf = text_font(app);
    const char* title;
    // every text in a few lengths: the longest that fits the panel is drawn
    const char* l1[3];
    const char* l2[3];
    const char* l3[3] = {"", "", ""};
    if(app->blackout) {
        title = n ? "BLACKOUT" : ">> BLACKOUT <<";
        if(app->blackout_sent) {
            l1[0] = l1[1] = l1[2] = "restoring...";
            l2[0] = l2[1] = l2[2] = "";
        } else {
            l1[0] = "the PC is dark", l1[1] = "PC is dark", l1[2] = "dark";
            l2[0] = "OK: restore", l2[1] = "OK: wake", l2[2] = "OK";
        }
    } else if(app->blackout_sent) {
        title = "BLACKOUT";
        l1[0] = l1[1] = l1[2] = "sent...";
        l2[0] = "waiting for the PC", l2[1] = "waiting for PC", l2[2] = "wait";
    } else if(app->blackout_ask) {
        title = n ? "BLACKOUT?" : "BLACKOUT THE PC?";
        if(n) {
            l1[0] = "lock, mute,", l1[1] = "lock+mute", l1[2] = "lock";
            l3[0] = "hide all", l3[1] = "hide", l3[2] = "hide";
        } else {
            l1[0] = "lock screen, mute, hide all", l1[1] = "lock, mute, hide all", l1[2] = "lock+mute+hide";
        }
        l2[0] = "OK: yes   Back: no", l2[1] = "OK: yes  Back: no", l2[2] = "OK: yes";
    } else {
        return;
    }
    int lines = 1 + (l2[0][0] ? 1 : 0) + (l3[0][0] ? 1 : 0);
    int px = n ? 2 : 6, pw = W - 2 * px;
    int ph = 15 + lines * (tf->line + 1) + 4;
    int py = n ? (H - ph) / 2 : 12 + (H - 12 - ph) / 2;
    int y = draw_panel(c, px, py, pw, ph, title);
    font_text(c, app);
    canvas_draw_str_aligned(c, W / 2, y + 1, AlignCenter, AlignTop, first_fit(c, pw - 4, l1, 3));
    y += tf->line + 1;
    if(l3[0][0]) {
        canvas_draw_str_aligned(c, W / 2, y + 1, AlignCenter, AlignTop, first_fit(c, pw - 4, l3, 3));
        y += tf->line + 1;
    }
    if(l2[0][0]) canvas_draw_str_aligned(c, W / 2, y + 1, AlignCenter, AlignTop, first_fit(c, pw - 4, l2, 3));
}

/* ------------------------------------------------------------------ RF tab */
static const char* const rf_mode_names[RfModeCount] = {"SCOUT", "CAPTURE", "FOLLOW", "NFC"};

static void fmt_mhz(char* out, size_t size, uint32_t hz) {
    snprintf(
        out, size, "%lu.%02lu", (unsigned long)(hz / 1000000), (unsigned long)(hz / 10000 % 100));
}

static void fmt_kb(char* out, size_t size, uint32_t kb) {
    if(kb >= 1024 * 1024)
        snprintf(
            out,
            size,
            "%lu.%luG",
            (unsigned long)(kb / 1048576),
            (unsigned long)(kb % 1048576 * 10 / 1048576));
    else if(kb >= 1024)
        snprintf(out, size, "%luM", (unsigned long)(kb / 1024));
    else
        snprintf(out, size, "%luK", (unsigned long)kb);
}

/* local wall-clock time of a UTC instant (the offset is what the PC told us) */
static void fmt_clock(char* out, size_t size, uint32_t utc, int16_t tz_minutes) {
    DateTime dt;
    int64_t local = (int64_t)utc + (int64_t)tz_minutes * 60;
    datetime_timestamp_to_datetime((uint32_t)(local > 0 ? local : 0), &dt);
    snprintf(out, size, "%02u:%02u:%02u", dt.hour, dt.minute, dt.second);
}

/* dBm -> x within a bar w wide, -100 at the left, -30 at the right */
static int rssi_px(int16_t dbm, int w) {
    int v = dbm < -100 ? -100 : (dbm > -30 ? -30 : dbm);
    return (v + 100) * (w - 1) / 70;
}

/* a 9x10 speaker, with sound waves while it clicks */
static void draw_speaker(Canvas* c, int x, int y, bool loud) {
    canvas_draw_box(c, x, y + 3, 2, 4);
    canvas_draw_line(c, x + 2, y + 3, x + 5, y);
    canvas_draw_line(c, x + 2, y + 6, x + 5, y + 9);
    canvas_draw_line(c, x + 5, y, x + 5, y + 9);
    if(loud) {
        canvas_draw_dot(c, x + 7, y + 2);
        canvas_draw_dot(c, x + 8, y + 4);
        canvas_draw_dot(c, x + 8, y + 5);
        canvas_draw_dot(c, x + 7, y + 7);
    }
}

/* -100..-30 dBm bar: the live level fills it, the peak is a notch, the noise floor a dotted
 * mark under it */
static void draw_rssi_bar(Canvas* c, const RfStatus* s, int x, int y, int w) {
    canvas_draw_frame(c, x, y, w, 7);
    int fill = rssi_px(s->live_rssi_dbm, w - 2);
    if(fill > 0) canvas_draw_box(c, x + 1, y + 1, fill, 5);
    int px = x + 1 + rssi_px(s->peak_rssi_dbm, w - 2);
    canvas_set_color(c, ColorXOR);
    canvas_draw_line(c, px, y - 2, px, y + 8);
    fg(c);
    if(s->floor_dbm) {
        int fx = x + 1 + rssi_px(s->floor_dbm, w - 2);
        canvas_draw_dot(c, fx, y + 8);
        canvas_draw_dot(c, fx - 1, y + 9);
        canvas_draw_dot(c, fx + 1, y + 9);
    }
}

/* Follow: the Geiger counter.  The live RSSI big, the peak and the noise floor, the bar, the
 * speaker while it clicks, and what is being followed (the first signal becomes the profile). */
static void draw_rf_geiger(Canvas* c, App* app, int y) {
    int W = canvas_width(c), H = canvas_height(c);
    bool n = narrow(c);
    const RfStatus* s = &app->rf_status;
    const TextFont* tf = text_font(app);
    int line = tf->line;
    char num[8], peak[16], noise[16], followed[80], problem[32] = "";
    snprintf(num, sizeof(num), "%d", s->live_rssi_dbm);
    snprintf(peak, sizeof(peak), "PEAK %d", s->peak_rssi_dbm);
    snprintf(noise, sizeof(noise), "NOISE %d", s->floor_dbm);
    if(s->follow_valid) {
        // "KeeLoq 0ABCDEF btn 2": the protocol name and the identity, the bits and "sn" dropped
        char name[20];
        snprintf(name, sizeof(name), "%s", s->follow_label);
        char* sp = strrchr(name, ' ');
        if(sp && sp[1] >= '0' && sp[1] <= '9') *sp = 0;
        const char* info = s->last_info;
        if(strncmp(info, "sn ", 3) == 0) info += 3;
        snprintf(followed, sizeof(followed), "%s %s", name, info);
    } else {
        snprintf(followed, sizeof(followed), "first signal = profile");
    }
    if(s->storage_full)
        snprintf(problem, sizeof(problem), "SD FULL: IMPORT ON PC");
    else if(s->errors)
        snprintf(problem, sizeof(problem), "WRITE ERRORS: %lu", (unsigned long)s->errors);
    // the big number sits on a baseline 17 px below y (FontBigNumbers digits are 16 px tall)
    canvas_set_font(c, FontBigNumbers);
    int nw = canvas_string_width(c, num);
    canvas_draw_str_aligned(c, n ? 1 : 2, y + 17, AlignLeft, AlignBottom, num);
    font_text(c, app);
    int rw = MAX(text_width(c, peak), text_width(c, noise));
    int rx = W - 2 - rw;
    int ux = (n ? 1 : 2) + nw + 3; // where the unit goes
    if(n || ux + text_width(c, "dBm") + 2 <= rx)
        canvas_draw_str_aligned(c, ux, y + 17, AlignLeft, AlignBottom, "dBm");
    if(n) {
        int yy = y + 20;
        canvas_draw_str_aligned(c, 1, yy, AlignLeft, AlignTop, peak);
        if(s->geiger_sound && text_width(c, peak) + 12 <= W) draw_speaker(c, W - 10, yy, true);
        yy += line;
        canvas_draw_str_aligned(c, 1, yy, AlignLeft, AlignTop, noise);
        yy += line + 3;
        draw_rssi_bar(c, s, 1, yy, W - 2);
        yy += 12;
        if(problem[0]) {
            draw_str_fit(c, 1, yy, problem, W - 2);
        } else {
            draw_str_fit(c, 1, yy, s->follow_valid ? s->follow_label : "first signal", W - 2);
            yy += line;
            if(yy + line <= H - line)
                draw_str_fit(c, 1, yy, s->follow_valid ? s->last_info : "= profile", W - 2);
        }
        canvas_draw_str_aligned(c, 1, H - line, AlignLeft, AlignTop, "OK: stop");
        return;
    }
    canvas_draw_str_aligned(c, rx, y, AlignLeft, AlignTop, peak);
    canvas_draw_str_aligned(c, rx, y + line, AlignLeft, AlignTop, noise);
    int sx = ux + text_width(c, "dBm") + 6;
    if(s->geiger_sound && sx + 10 < rx) draw_speaker(c, sx, y + 4, true);
    int fy = H - line;
    int by = MIN(y + 21, fy - 11);
    draw_rssi_bar(c, s, 2, by, W - 4);
    draw_str_fit(c, 2, fy, problem[0] ? problem : followed, W - 3);
}

static void draw_rf(Canvas* c, App* app) {
    int W = canvas_width(c), H = canvas_height(c);
    bool n = narrow(c);
    const RfStatus* s = &app->rf_status;
    const TextFont* tf = text_font(app);
    int cap = tf->cap, line = tf->line;
    if(!app->rf) {
        draw_message(c, app, "RF engine unavailable");
        return;
    }
    font_text(c, app);
    // mode in a lit box; the receiver state right of it (under it when narrow) with a
    // blinking dot while it listens
    char a[16], state[24];
    const char* mode = rf_mode_names[s->mode < RfModeCount ? s->mode : 0];
    int bh = cap + 3;
    canvas_draw_box(c, 1, 12, text_width(c, mode) + 6, bh);
    bg(c);
    canvas_draw_str_aligned(c, 4, 14, AlignLeft, AlignTop, mode);
    fg(c);
    if(!s->running)
        snprintf(state, sizeof(state), "OFF");
    else if(s->mode == RfModeNfc)
        snprintf(state, sizeof(state), s->nfc_field ? "FIELD!" : "LISTEN");
    else {
        fmt_mhz(a, sizeof(a), s->frequency_hz);
        snprintf(state, sizeof(state), "RX %s", a);
    }
    int sy = n ? 12 + bh + 2 : 14;
    canvas_draw_str_aligned(c, W - 1, sy, AlignRight, AlignTop, state);
    if(s->running && (app->tick & 2))
        canvas_draw_disc(c, W - text_width(c, state) - 5, sy + cap / 2, 2);
    int y = n ? sy + line + 1 : 12 + bh + 2;
    if(s->mode == RfModeFollow && s->running) {
        draw_rf_geiger(c, app, y);
        return;
    }

    // counters: two columns (one when narrow), long labels where they fit next to the value
    static const char* const full[4] = {"EVENTS", "FAMILIES", "PENDING", "SD FREE"};
    static const char* const brief[4] = {"EVT", "FAM", "PEND", "SD"};
    char v[4][16];
    snprintf(v[0], sizeof(v[0]), "%lu", (unsigned long)s->events);
    snprintf(v[1], sizeof(v[1]), "%lu", (unsigned long)s->families);
    snprintf(v[2], sizeof(v[2]), "%lu", (unsigned long)(s->pending + s->carry));
    fmt_kb(v[3], sizeof(v[3]), s->free_kb);
    for(int i = 0; i < 4; i++) {
        int x = n ? 1 : (i % 2 ? W / 2 + 3 : 2);
        int right = n ? W - 1 : (i % 2 ? W - 1 : W / 2 - 3);
        int yy = y + (n ? i : i / 2) * line;
        int room = right - x - text_width(c, v[i]) - 3;
        canvas_draw_str_aligned(
            c, x, yy, AlignLeft, AlignTop, text_width(c, full[i]) <= room ? full[i] : brief[i]);
        canvas_draw_str_aligned(c, right, yy, AlignRight, AlignTop, v[i]);
    }
    y += (n ? 4 : 2) * line;
    canvas_draw_line(c, 0, y, W, y);
    y += 2;

    // the last event: time, frequency (or NFC), level or Follow match
    char when[12] = "", hm[8] = "", where[12] = "", tail[12] = "", tail_short[12] = "";
    if(s->last_unix) {
        fmt_clock(when, sizeof(when), s->last_unix, app->settings.rf_tz);
        snprintf(hm, sizeof(hm), "%.5s", when);
        if(s->last_frequency_hz)
            fmt_mhz(where, sizeof(where), s->last_frequency_hz);
        else
            snprintf(where, sizeof(where), "NFC");
        if(s->mode == RfModeFollow && s->follow_valid) {
            snprintf(tail, sizeof(tail), "%u%%", s->last_similarity);
            snprintf(tail_short, sizeof(tail_short), "%s", tail);
        } else if(s->last_frequency_hz) {
            snprintf(tail, sizeof(tail), "%ddB", s->last_rssi_dbm);
            snprintf(tail_short, sizeof(tail_short), "%d", s->last_rssi_dbm);
        }
    }
    const char* sp = tail[0] ? " " : "";
    // a problem replaces the key hints
    char problem[32] = "", problem_short[16] = "";
    if(s->storage_full) {
        snprintf(problem, sizeof(problem), "SD FULL: IMPORT ON PC");
        snprintf(problem_short, sizeof(problem_short), "SD FULL");
    } else if(s->errors) {
        snprintf(problem, sizeof(problem), "WRITE ERRORS: %lu", (unsigned long)s->errors);
        snprintf(problem_short, sizeof(problem_short), "ERRORS %lu", (unsigned long)s->errors);
    }
    const char* problems[2] = {problem, problem_short};
    int bar = cap + 4; // the problem bar
    const char* ok = s->running ? "OK: stop" : "OK: start";

    if(!n) {
        // one line for the event, one for the hints or the problem; the problem wins over the
        // event, the event over the hints
        int foot = problem[0] ? bar : line;
        bool both = y + line + foot <= H;
        if(both || !problem[0]) {
            char l1[64], l2[56], l3[48], l4[32];
            if(s->last_unix && s->last_label[0]) {
                // "LAST 12:41 KeeLoq 66b -63dB": what it was, then how strong
                snprintf(l1, sizeof(l1), "LAST %s %s%s%s", hm, s->last_label, sp, tail);
                snprintf(l2, sizeof(l2), "%s %s%s%s", hm, s->last_label, sp, tail);
                snprintf(l3, sizeof(l3), "%s %s", hm, s->last_label);
                snprintf(l4, sizeof(l4), "%s", s->last_label);
            } else if(s->last_unix) {
                snprintf(l1, sizeof(l1), "LAST %s %s%s%s", when, where, sp, tail);
                snprintf(l2, sizeof(l2), "%s %s%s%s", when, where, sp, tail);
                snprintf(l3, sizeof(l3), "%s %s%s%s", hm, where, sp, tail);
                snprintf(l4, sizeof(l4), "%s %s", hm, where);
            } else {
                snprintf(l1, sizeof(l1), "LAST: no events yet");
                snprintf(l2, sizeof(l2), "no events yet");
                snprintf(l3, sizeof(l3), "no events");
                snprintf(l4, sizeof(l4), "-");
            }
            const char* last[4] = {l1, l2, l3, l4};
            canvas_draw_str_aligned(c, 2, y, AlignLeft, AlignTop, first_fit(c, W - 3, last, 4));
        }
        if(both || problem[0]) {
            if(problem[0]) {
                canvas_draw_box(c, 0, H - bar, W, bar);
                bg(c);
                canvas_draw_str_aligned(
                    c, W / 2, H - bar + 2, AlignCenter, AlignTop, first_fit(c, W - 4, problems, 2));
                fg(c);
            } else if(s->last_unix && s->last_info[0]) {
                // the decode of the last event ("sn 0ABCDEF btn 2") takes the hint line
                char info[48];
                snprintf(info, sizeof(info), "%s%s", s->last_info, s->last_rolling ? " ~" : "");
                draw_str_fit(c, 2, H - line, info, W - 3);
            } else {
                char h1[32], h2[32];
                snprintf(h1, sizeof(h1), "%s   UP/DN: mode", ok);
                snprintf(h2, sizeof(h2), "%s UP/DN:mode", ok);
                const char* hints[3] = {h1, h2, ok};
                canvas_draw_str_aligned(c, 2, H - line, AlignLeft, AlignTop, first_fit(c, W - 3, hints, 3));
            }
        }
        return;
    }

    // narrow: "LAST EVENT", time, frequency + level, then two hint rows or the problem bar;
    // when it does not fit, the label goes first, then the hints
    const char* rows[6];
    int count = 0;
    char wt[24], wt2[24];
    rows[count++] = text_width(c, "LAST EVENT") <= W - 2 ? "LAST EVENT" : "LAST";
    char label_short[20];
    if(s->last_unix && s->last_label[0]) {
        rows[count++] = when;
        snprintf(label_short, sizeof(label_short), "%s", s->last_label);
        char* sp = strrchr(label_short, ' ');
        if(sp && text_width(c, s->last_label) > W - 2) *sp = 0;
        rows[count++] = label_short;
        snprintf(wt, sizeof(wt), "%s%s%s", where, sp, tail);
        snprintf(wt2, sizeof(wt2), "%s%s%s", where, sp, tail_short);
        rows[count++] = text_width(c, wt) <= W - 2 ? wt : wt2;
        if(s->last_info[0]) rows[count++] = s->last_info;
    } else if(s->last_unix) {
        rows[count++] = when;
        snprintf(wt, sizeof(wt), "%s%s%s", where, sp, tail);
        snprintf(wt2, sizeof(wt2), "%s%s%s", where, sp, tail_short);
        if(text_width(c, wt) <= W - 2)
            rows[count++] = wt;
        else if(text_width(c, wt2) <= W - 2)
            rows[count++] = wt2;
        else {
            rows[count++] = where;
            if(tail[0]) rows[count++] = tail;
        }
    } else {
        rows[count++] = "none yet";
    }
    int first = 0, hints = problem[0] ? 0 : 2;
    int foot = problem[0] ? bar : hints * line;
    while(y + (count - first) * line + foot > H) {
        if(first == 0 && count > 2)
            first = 1;
        else if(hints > 0)
            foot = --hints * line;
        else
            break;
    }
    for(int i = first; i < count && y + line <= H - foot; i++, y += line)
        draw_str_fit(c, 1, y, rows[i], W - 2);
    if(problem[0]) {
        canvas_draw_box(c, 0, H - bar, W, bar);
        bg(c);
        canvas_draw_str_aligned(
            c, W / 2, H - bar + 2, AlignCenter, AlignTop, first_fit(c, W - 4, problems, 2));
        fg(c);
    } else {
        static const char* const mode_hint[2] = {"UP/DN: mode", "UP/DN"};
        if(hints == 2)
            canvas_draw_str_aligned(
                c, 1, H - 2 * line, AlignLeft, AlignTop, first_fit(c, W - 2, mode_hint, 2));
        if(hints >= 1) canvas_draw_str_aligned(c, 1, H - line, AlignLeft, AlignTop, ok);
    }
}

static void main_draw(Canvas* c, void* model) {
    MainModel* m = model;
    App* app = m->app;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    canvas_clear(c);
    fg(c);
    ScreenId screen = current_screen(app);
    // CMD and RF stay usable without the PC (RF is meant to run in a pocket)
    bool offline = (!app->link || app->host_closed) && screen != ScreenCmd && screen != ScreenRf;
    if(offline) {
        draw_offline(c, app);
    } else {
        draw_header(c, app);
        if(screen == ScreenSys) {
            draw_sys(c, app);
            draw_blackout(c, app);
        } else if(screen == ScreenRf)
            draw_rf(c, app);
        else if(screen == ScreenCmd) {
            if(!app->link)
                draw_message(c, app, "link down");
            else
                draw_cmd(c, app);
        } else if(app->detail)
            draw_detail(c, app, screen_kind(screen));
        else
            draw_list(c, app, screen_kind(screen));
        if(app->alert) draw_alert(c, app);
    }
    if(app->ota.state != OtaIdle) draw_ota(c, app);
    furi_mutex_release(app->mutex);
}

/* ------------------------------------------------------------------ navigation */
static void keyboard_done(void* context);

static void go_view(App* app, uint32_t id) {
    app->current_view = id;
    view_dispatcher_switch_to_view(app->views, id);
}

static void open_keyboard(App* app) {
    // reset clears stale cursor/selection state but keeps our external buffer (last command)
    text_input_reset(app->keyboard);
    // while a command runs, the text goes to its input (e.g. a y/n prompt) instead
    text_input_set_header_text(
        app->keyboard, app->cmd.running ? "Input for the running command" : "Command (OK=save)");
    text_input_set_result_callback(
        app->keyboard, keyboard_done, app, app->cmd.input, sizeof(app->cmd.input), false);
    go_view(app, ViewKeyboard);
}

static void open_alert_target(App* app) {
    for(uint8_t t = 0; t < app->tab_count; t++) {
        ScreenId sid = app->tabs[t];
        if((sid == ScreenCodex || sid == ScreenClaude) && screen_kind(sid) == app->alert_kind) {
            List* l = &app->lists[app->alert_kind];
            for(uint8_t i = 0; i < l->count; i++)
                if(!strcmp(l->items[i].key, app->alert_key)) {
                    app->tab_index = t;
                    l->cursor = visible_ordinal(app, app->alert_kind, i);
                    l->detail_scroll = 0;
                    app->detail = true;
                    break;
                }
            break;
        }
    }
    app->alert = false;
}

static bool main_input(InputEvent* in, void* context) {
    App* app = context;
    bool handled = true;
    furi_mutex_acquire(app->mutex, FuriWaitForever);

    if(in->type == InputTypeLong && in->key == InputKeyOk) {
        furi_mutex_release(app->mutex);
        build_settings(app); // refresh the Version row (an update may have arrived)
        variable_item_list_set_selected_item(app->settings_view, 0);
        go_view(app, ViewSettings);
        return true;
    }
    if(in->type != InputTypeShort && in->type != InputTypeRepeat) {
        furi_mutex_release(app->mutex);
        return handled;
    }
    // an update in progress owns the screen
    if(app->ota.state == OtaFailed) {
        app->ota.state = OtaIdle; // any key closes the error
        furi_mutex_release(app->mutex);
        return true;
    }
    if(app->ota.state == OtaRequested || app->ota.state == OtaReceiving) {
        if(in->key == InputKeyBack) ota_abort(&app->ota, "cancelled");
        furi_mutex_release(app->mutex);
        return true;
    }
    if(app->ota.state == OtaDone) {
        furi_mutex_release(app->mutex);
        return true;
    }
    if(app->alert) {
        if(app->alert_update) {
            if(in->key == InputKeyOk) {
                ota_request(app);
            } else {
                app->alert = false;
                app->alert_update = false;
            }
        } else if(in->key == InputKeyOk) {
            open_alert_target(app);
        } else {
            app->alert = false;
        }
        furi_mutex_release(app->mutex);
        return true;
    }

    ScreenId screen = current_screen(app);
    bool exit = false;
    switch(in->key) {
    case InputKeyLeft:
        app->detail = false;
        if(screen == ScreenCodex || screen == ScreenClaude)
            app->lists[screen_kind(screen)].detail_scroll = 0;
        app->tab_index = (app->tab_index + app->tab_count - 1) % app->tab_count;
        break;
    case InputKeyRight:
        app->detail = false;
        if(screen == ScreenCodex || screen == ScreenClaude)
            app->lists[screen_kind(screen)].detail_scroll = 0;
        if(app->tab_index + 1 >= app->tab_count) {
            furi_mutex_release(app->mutex);
            build_settings(app);
            variable_item_list_set_selected_item(app->settings_view, 0);
            go_view(app, ViewSettings);
            return true;
        }
        app->tab_index++;
        break;
    case InputKeyUp:
        if(screen == ScreenRf && app->rf) {
            rf_engine_set_mode(
                app->rf, (RfMode)((app->rf_status.mode + RfModeCount - 1) % RfModeCount));
            rf_refresh_status(app);
        } else if(screen == ScreenCmd) {
            if(app->cmd.scroll < app->cmd.count - 1) app->cmd.scroll++;
        } else if((screen == ScreenCodex || screen == ScreenClaude) && !app->detail) {
            List* l = &app->lists[screen_kind(screen)];
            if(l->cursor > 0) l->cursor--;
        } else if((screen == ScreenCodex || screen == ScreenClaude) && app->detail) {
            List* l = &app->lists[screen_kind(screen)];
            if(l->detail_scroll > 0) l->detail_scroll--;
        }
        break;
    case InputKeyDown:
        if(screen == ScreenRf && app->rf) {
            rf_engine_set_mode(app->rf, (RfMode)((app->rf_status.mode + 1) % RfModeCount));
            rf_refresh_status(app);
        } else if(screen == ScreenCmd) {
            if(app->cmd.scroll > 0) app->cmd.scroll--;
        } else if((screen == ScreenCodex || screen == ScreenClaude) && !app->detail) {
            List* l = &app->lists[screen_kind(screen)];
            uint8_t n = visible_count(app, screen_kind(screen));
            if(l->cursor + 1 < n) l->cursor++;
        } else if((screen == ScreenCodex || screen == ScreenClaude) && app->detail) {
            List* l = &app->lists[screen_kind(screen)];
            if(l->detail_scroll < 255) l->detail_scroll++;
        }
        break;
    case InputKeyOk:
        if(screen == ScreenSys && app->link) {
            if(app->blackout) {
                uplink_send(app, "BO|0"); // the Flipper is the key: restore without a PIN
                app->blackout_sent = app->tick;
            } else if(app->blackout_ask) {
                uplink_send(app, "BO|1");
                app->blackout_ask = 0;
                app->blackout_sent = app->tick;
            } else {
                app->blackout_ask = app->tick + BLACKOUT_ASK_TICKS;
            }
        } else if(screen == ScreenRf && app->rf) {
            if(app->rf_status.running)
                rf_engine_stop(app->rf);
            else
                rf_engine_start(app->rf);
            rf_refresh_status(app);
        } else if(screen == ScreenCmd) {
            app->cmd.unseen = false;
            furi_mutex_release(app->mutex);
            open_keyboard(app);
            return true;
        } else if(screen == ScreenCodex || screen == ScreenClaude) {
            List* l = &app->lists[screen_kind(screen)];
            uint8_t n = visible_count(app, screen_kind(screen));
            if(n) {
                if(app->detail) {
                    int raw = visible_raw_index(app, screen_kind(screen), l->cursor);
                    if(raw >= 0) dismiss_item(app, &l->items[raw]);
                    app->detail = false;
                } else {
                    app->detail = true;
                    l->detail_scroll = 0;
                }
            }
        }
        break;
    case InputKeyBack:
        if(app->blackout_ask) {
            app->blackout_ask = 0;
        } else if(app->detail) {
            if(screen == ScreenCodex || screen == ScreenClaude) {
                List* l = &app->lists[screen_kind(screen)];
                int raw = visible_raw_index(app, screen_kind(screen), l->cursor);
                if(raw >= 0) dismiss_item(app, &l->items[raw]);
            }
            app->detail = false;
        } else if(screen == ScreenCmd && app->cmd.running) {
            char msg[16];
            snprintf(msg, sizeof(msg), "K|%u", app->cmd.seq);
            uplink_send(app, msg);
            cmd_push(app, "[cancel sent]");
        } else
            exit = true;
        break;
    default:
        handled = false;
        break;
    }
    if(screen == ScreenCmd) app->cmd.unseen = false;
    furi_mutex_release(app->mutex);
    if(exit) view_dispatcher_stop(app->views);
    return handled;
}

static void keyboard_done(void* context) {
    App* app = context;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    if(strlen(app->cmd.input) > 0) {
        char out[CMD_INPUT + 16];
        if(app->cmd.running) {
            // A running command owns the persistent shell.  Feed the keyboard text to
            // its stdin instead of queuing a second command (the companion appends Enter).
            snprintf(out, sizeof(out), "T|%u|%s", app->cmd.seq, app->cmd.input);
        } else {
            app->cmd.seq++;
            app->cmd.running = true;
            app->cmd.have_exit = false;
            app->cmd.scroll = 0;
            char shown[CMD_INPUT + 4];
            snprintf(shown, sizeof(shown), "> %s", app->cmd.input);
            cmd_push(app, shown);
            snprintf(out, sizeof(out), "C|%u|%s", app->cmd.seq, app->cmd.input);
        }
        uplink_send(app, out);
    }
    furi_mutex_release(app->mutex);
    go_view(app, ViewMain);
}

static bool nav_event(void* context) {
    App* app = context;
    if(app->current_view == ViewSettings) {
        uplink_settings_save(&app->settings);
        go_view(app, ViewMain);
        return true;
    }
    if(app->current_view == ViewKeyboard) {
        go_view(app, ViewMain);
        return true;
    }
    return false; // on main: let the dispatcher stop
}

/* ------------------------------------------------------------------ settings view */
static const char* const on_off[] = {"OFF", "ON"};
static const char* const ind_vals[] = {"Bars", "Text"};
static const char* const font_vals[] = {"Micro", "Small", "Normal", "Large"};
static const uint8_t font_sizes[] = {FontMicro, FontSmall, FontNormal, FontLarge};
static const char* const orientation_vals[] = {"Horizontal", "Vertical"};
// tab choices in the order the user sees them; stored as ScreenId
static const char* const tab_vals[] = {"SYS", "CDX", "CLD", "CMD", "RF", "Off"};
static const uint8_t tab_screens[] = {ScreenSys, ScreenCodex, ScreenClaude, ScreenCmd, ScreenRf, ScreenOff};
static const char* const rf_band_vals[] = {"All", "433", "315", "868"};
static const uint16_t rf_dwell_vals[] = {100, 250, 500, 1000, 2000};
static const uint16_t rf_capture_vals[] = {250, 500, 1000, 2000};
static const char* const rf_keep_vals[] = {"Delete", "Keep"};
#define RF_RSSI_MIN   (-100)
#define RF_RSSI_STEP  5
#define RF_RSSI_COUNT 13 // -100 ... -40 dBm

enum {
    SetVibro,
    SetCmdVibro,
    SetLed,
    SetBacklight,
    SetIndicators,
    SetFont,
    SetOrientation,
    SetTab0,
    SetTab1,
    SetTab2,
    SetTab3,
    SetTab4,
    SetRfBand,
    SetRfRssi,
    SetRfDwell,
    SetRfCapture,
    SetRfFeedback,
    SetRfGeiger,
    SetRfKeep,
    SetRfAutostart,
    SetRfSync,
    SetVersion, // read-only; OK installs a pending update
};

static uint8_t tab_choice(uint8_t screen) {
    for(uint8_t i = 0; i < COUNT_OF(tab_screens); i++)
        if(tab_screens[i] == screen) return i;
    return COUNT_OF(tab_screens) - 1; // Off
}

static uint8_t font_choice(uint8_t font) {
    for(uint8_t i = 0; i < COUNT_OF(font_sizes); i++)
        if(font_sizes[i] == font) return i;
    return 2; // Normal
}

static uint8_t nearest_index(const uint16_t* vals, uint8_t count, uint16_t v) {
    uint8_t best = 0;
    for(uint8_t i = 1; i < count; i++)
        if(abs((int)vals[i] - (int)v) < abs((int)vals[best] - (int)v)) best = i;
    return best;
}

/* the text shown for value `idx` of settings row `row` */
static void setting_text(uint8_t row, uint8_t idx, char* out, size_t size) {
    const char* text = NULL;
    switch(row) {
    case SetIndicators:
        text = ind_vals[idx];
        break;
    case SetFont:
        text = font_vals[idx];
        break;
    case SetOrientation:
        text = orientation_vals[idx];
        break;
    case SetTab0:
    case SetTab1:
    case SetTab2:
    case SetTab3:
    case SetTab4:
        text = tab_vals[idx];
        break;
    case SetRfBand:
        text = rf_band_vals[idx];
        break;
    case SetRfRssi:
        snprintf(out, size, "%d dBm", RF_RSSI_MIN + idx * RF_RSSI_STEP);
        return;
    case SetRfDwell:
        snprintf(out, size, "%u ms", rf_dwell_vals[idx]);
        return;
    case SetRfCapture:
        snprintf(out, size, "%u ms", rf_capture_vals[idx]);
        return;
    case SetRfKeep:
        text = rf_keep_vals[idx];
        break;
    default:
        text = on_off[idx];
        break;
    }
    snprintf(out, size, "%s", text);
}

static void setting_changed(VariableItem* item) {
    App* app = variable_item_get_context(item);
    uint8_t idx = variable_item_get_current_value_index(item);
    uint8_t sel = variable_item_list_get_selected_item_index(app->settings_view);
    if(sel == SetVersion) return;
    char text[24];
    setting_text(sel, idx, text, sizeof(text));
    variable_item_set_current_value_text(item, text);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    UplinkSettings* s = &app->settings;
    bool rf_config = false;
    switch(sel) {
    case SetVibro:
        s->vibro = idx;
        break;
    case SetCmdVibro:
        s->cmd_vibro = idx;
        break;
    case SetLed:
        s->led = idx;
        break;
    case SetBacklight:
        s->backlight = idx;
        break;
    case SetIndicators:
        s->indicators = idx;
        break;
    case SetFont:
        s->font = font_sizes[idx];
        break;
    case SetOrientation:
        // only the main view turns: the stock keyboard and this settings list are drawn
        // for 128x64 and stay horizontal (applied when the main view is shown again)
        s->orientation = idx;
        view_set_orientation(
            app->main_view, idx ? ViewOrientationVertical : ViewOrientationHorizontal);
        break;
    case SetTab0:
    case SetTab1:
    case SetTab2:
    case SetTab3:
    case SetTab4:
        s->tabs[sel - SetTab0] = tab_screens[idx];
        rebuild_tabs(app);
        rf_lifecycle(app); // the RF engine lives while the RF tab is enabled
        break;
    case SetRfBand:
        s->rf_band = idx;
        rf_config = true;
        break;
    case SetRfRssi:
        s->rf_rssi = RF_RSSI_MIN + idx * RF_RSSI_STEP;
        rf_config = true;
        break;
    case SetRfDwell:
        s->rf_dwell_ms = rf_dwell_vals[idx];
        rf_config = true;
        break;
    case SetRfCapture:
        s->rf_capture_ms = rf_capture_vals[idx];
        rf_config = true;
        break;
    case SetRfFeedback:
        s->rf_feedback = idx;
        rf_config = true;
        break;
    case SetRfGeiger:
        s->rf_geiger = idx;
        rf_config = true;
        break;
    case SetRfKeep:
        s->rf_keep = idx;
        rf_config = true;
        break;
    case SetRfAutostart:
        s->rf_autostart = idx;
        break;
    case SetRfSync:
        s->rf_sync = idx;
        break;
    default:
        break;
    }
    if(rf_config) rf_apply_config(app);
    furi_mutex_release(app->mutex);
}

/* OK on the Version row installs a pending update */
static void settings_enter(void* context, uint32_t index) {
    App* app = context;
    if(index != SetVersion) return;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    bool go = app->ota.available && app->ota.state != OtaReceiving;
    if(go) ota_request(app);
    furi_mutex_release(app->mutex);
    if(go) {
        uplink_settings_save(&app->settings);
        go_view(app, ViewMain);
    }
}

static void add_row(App* app, const char* label, uint8_t row, uint8_t count, uint8_t val) {
    VariableItem* it =
        variable_item_list_add(app->settings_view, label, count, setting_changed, app);
    if(val >= count) val = 0;
    char text[24];
    setting_text(row, val, text, sizeof(text));
    variable_item_set_current_value_index(it, val);
    variable_item_set_current_value_text(it, text);
}

static void build_settings(App* app) {
    const UplinkSettings* s = &app->settings;
    variable_item_list_reset(app->settings_view);
    // rows must be added in the order of the Set* enum
    add_row(app, "Vibration", SetVibro, 2, s->vibro);
    add_row(app, "Vibrate on cmd reply", SetCmdVibro, 2, s->cmd_vibro);
    add_row(app, "LED alerts", SetLed, 2, s->led);
    add_row(app, "Wake screen on alert", SetBacklight, 2, s->backlight);
    add_row(app, "Indicators", SetIndicators, 2, s->indicators);
    add_row(app, "Font size", SetFont, COUNT_OF(font_sizes), font_choice(s->font));
    add_row(app, "Orientation", SetOrientation, 2, s->orientation);
    static const char* const tab_names[TAB_SLOTS] = {"Tab 1", "Tab 2", "Tab 3", "Tab 4", "Tab 5"};
    for(uint8_t i = 0; i < TAB_SLOTS; i++)
        add_row(app, tab_names[i], SetTab0 + i, COUNT_OF(tab_vals), tab_choice(s->tabs[i]));
    add_row(app, "RF band", SetRfBand, COUNT_OF(rf_band_vals), s->rf_band);
    int rssi = (s->rf_rssi - RF_RSSI_MIN + RF_RSSI_STEP / 2) / RF_RSSI_STEP;
    add_row(app, "RF trigger level", SetRfRssi, RF_RSSI_COUNT, (uint8_t)CLAMP(rssi, RF_RSSI_COUNT - 1, 0));
    add_row(
        app,
        "RF hop time",
        SetRfDwell,
        COUNT_OF(rf_dwell_vals),
        nearest_index(rf_dwell_vals, COUNT_OF(rf_dwell_vals), s->rf_dwell_ms));
    add_row(
        app,
        "RF capture window",
        SetRfCapture,
        COUNT_OF(rf_capture_vals),
        nearest_index(rf_capture_vals, COUNT_OF(rf_capture_vals), s->rf_capture_ms));
    add_row(app, "RF vibrate on signal", SetRfFeedback, 2, s->rf_feedback);
    add_row(app, "RF Geiger clicks", SetRfGeiger, 2, s->rf_geiger);
    add_row(app, "RF after import", SetRfKeep, COUNT_OF(rf_keep_vals), s->rf_keep);
    add_row(app, "RF on at app start", SetRfAutostart, 2, s->rf_autostart);
    add_row(app, "RF import by PC", SetRfSync, 2, s->rf_sync);
    // Version row: shows what is installed and what can be installed
    static char version_text[40];
    if(app->ota.available) {
        snprintf(version_text, sizeof(version_text), "OK: get %s", app->ota.tag);
    } else {
        snprintf(version_text, sizeof(version_text), "v%s", UPLINK_VERSION);
    }
    VariableItem* it = variable_item_list_add(app->settings_view, "Version", 1, NULL, app);
    variable_item_set_current_value_text(it, version_text);
    variable_item_list_set_enter_callback(app->settings_view, settings_enter, app);
}

/* ------------------------------------------------------------------ events/tick */
static void refresh(App* app) {
    with_view_model(app->main_view, MainModel * m, { UNUSED(m); }, true);
}

static bool custom_event(void* context, uint32_t event) {
    App* app = context;
    bool restart = false;
    if(event == EvRx) {
        app->rx_posted = false;
        drain_rx(app);
    } else if(event == EvTick) {
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        app->tick++;
        if(app->alert && app->tick > app->alert_until) {
            app->alert = false;
            app->alert_update = false;
        }
        if(app->link && app->tick - app->ver_tick > 120) send_version(app); // every 30 s
        if(app->ota.state == OtaReceiving && !app->link) ota_abort(&app->ota, "link lost");
        if(app->ota.state == OtaRequested && app->tick - app->ota_req_tick > 80)
            ota_abort(&app->ota, "no answer from PC");
        if(app->restart_at && app->tick >= app->restart_at) {
            app->restart_at = 0;
            restart = true;
        }
        if(app->link && app->tick - app->last_rx_tick > LINK_TICKS) app->link = false;
        if(app->blackout_ask && app->tick > app->blackout_ask) app->blackout_ask = 0;
        if(app->blackout_sent && app->tick - app->blackout_sent > BLACKOUT_SENT_TICKS)
            app->blackout_sent = 0;
        rf_refresh_status(app);
        rf_remind(app);
        furi_mutex_release(app->mutex);
    } else if(event == EvRf) {
        app->rf_event_posted = false; // clear first: a change after this posts a new event
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        rf_refresh_status(app);
        furi_mutex_release(app->mutex);
    } else if(event == EvRfTx) {
        app->rf_tx_posted = false;
        rf_drain_tx(app);
    }
    if(restart) {
        // the new .fap is on the SD card: exit and let the loader start it
        Loader* loader = furi_record_open(RECORD_LOADER);
        loader_enqueue_launch(loader, app->ota.self_path, NULL, LoaderDeferredLaunchFlagNone);
        furi_record_close(RECORD_LOADER);
        view_dispatcher_stop(app->views);
        return true;
    }
    if(app->current_view == ViewMain) refresh(app);
    return true;
}

static void tick_callback(void* context) {
    App* app = context;
    view_dispatcher_send_custom_event(app->views, EvTick);
}

/* ------------------------------------------------------------------ main */
int32_t uplink_app(void* p) {
    UNUSED(p);
    App* app = malloc(sizeof(App));
    memset(app, 0, sizeof(App));
    uplink_settings_load(&app->settings);
    ota_init(&app->ota);
    rebuild_tabs(app);
    app->sys.net_max = 64;
    app->sys.dsk_max = 256;

    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->rx = furi_stream_buffer_alloc(4096, 1);
    app->notifications = furi_record_open(RECORD_NOTIFICATION);
    app->gui = furi_record_open(RECORD_GUI);
    app->bt = furi_record_open(RECORD_BT);

    app->views = view_dispatcher_alloc();
    view_dispatcher_set_event_callback_context(app->views, app);
    view_dispatcher_set_custom_event_callback(app->views, custom_event);
    view_dispatcher_set_navigation_event_callback(app->views, nav_event);

    app->main_view = view_alloc();
    view_set_context(app->main_view, app);
    view_set_draw_callback(app->main_view, main_draw);
    view_set_input_callback(app->main_view, main_input);
    view_allocate_model(app->main_view, ViewModelTypeLocking, sizeof(MainModel));
    with_view_model(app->main_view, MainModel * m, { m->app = app; }, false);
    view_set_orientation(
        app->main_view,
        app->settings.orientation ? ViewOrientationVertical : ViewOrientationHorizontal);
    view_dispatcher_add_view(app->views, ViewMain, app->main_view);

    app->keyboard = text_input_alloc();
    text_input_set_result_callback(
        app->keyboard, keyboard_done, app, app->cmd.input, sizeof(app->cmd.input), false);
    view_dispatcher_add_view(app->views, ViewKeyboard, text_input_get_view(app->keyboard));

    app->settings_view = variable_item_list_alloc();
    build_settings(app);
    view_dispatcher_add_view(
        app->views, ViewSettings, variable_item_list_get_view(app->settings_view));

    view_dispatcher_attach_to_gui(app->views, app->gui, ViewDispatcherTypeFullscreen);

    bt_disconnect(app->bt);
    furi_delay_ms(200);
    bt_keys_storage_set_storage_path(app->bt, APP_DATA_PATH(".uplink.keys"));
    app->params.rx_callback = uplink_rx_callback;
    app->params.rx_context = app;
    app->profile = bt_profile_start(app->bt, uplink_ble_profile, &app->params);
    if(app->profile) {
        bt_set_status_changed_callback(app->bt, bt_status_callback, app);
        furi_hal_bt_start_advertising();
    }

    // RF Hunter: its callbacks post to app->views, so it starts after the dispatcher exists
    app->rf_tx = furi_stream_buffer_alloc(RF_TX_BUF, 1);
    rf_lifecycle(app);

    app->timer = furi_timer_alloc(tick_callback, FuriTimerTypePeriodic, app);
    furi_timer_start(app->timer, furi_ms_to_ticks(250));

    go_view(app, ViewMain);
    view_dispatcher_run(app->views);

    furi_timer_stop(app->timer);
    furi_timer_free(app->timer);
    app->rf_closing = true; // the dispatcher no longer runs: the engine must not post to it
    if(app->rf) {
        rf_engine_free(app->rf); // stops the receiver and releases the radio
        app->rf = NULL;
    }
    furi_stream_buffer_free(app->rf_tx);

    bt_set_status_changed_callback(app->bt, NULL, NULL);
    if(app->link) {
        uplink_send(app, "BB"); // leaving on purpose: the PC must not blackout for this
        furi_delay_ms(150);
    }
    bt_disconnect(app->bt);
    furi_delay_ms(200);
    bt_keys_storage_set_default_path(app->bt);
    if(!bt_profile_restore_default(app->bt)) FURI_LOG_E(TAG, "restore default BLE profile failed");

    view_dispatcher_remove_view(app->views, ViewMain);
    view_dispatcher_remove_view(app->views, ViewKeyboard);
    view_dispatcher_remove_view(app->views, ViewSettings);
    view_free(app->main_view);
    text_input_free(app->keyboard);
    ota_free(&app->ota);
    variable_item_list_free(app->settings_view);
    view_dispatcher_free(app->views);

    furi_record_close(RECORD_BT);
    furi_record_close(RECORD_GUI);
    furi_record_close(RECORD_NOTIFICATION);
    furi_stream_buffer_free(app->rx);
    furi_mutex_free(app->mutex);
    free(app);
    return 0;
}
