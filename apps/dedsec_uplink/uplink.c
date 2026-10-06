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
 *   Flipper -> PC (TX notify char):
 *     C|seq|command                            run this command
 *     K|seq                                    cancel the running command
 *     T|seq|text                               send text to a running command's stdin
 * state: W working, A needs approval, I your turn, S idle, E error.
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
#include "uplink_cyr_font.h"
#include "uplink_ota.h"
#include <loader/loader.h>

#define TAG          "Uplink"
#define MAX_ITEMS    10
#define HIST         62
#define SEEN_MAX     40
#define LINE_MAX     600
#define LAG_TICKS    (3 * 4)
#define LINK_TICKS   (8 * 4)
#define ALERT_TICKS  (5 * 4)
#define CMD_LINES    40
#define CMD_COLW     64
#define CMD_INPUT    200

enum { ViewMain = 0, ViewKeyboard, ViewSettings };
enum { EvRx = 1, EvTick };

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
    ScreenId tabs[4];
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

    char line[LINE_MAX];
    uint16_t line_len;
} App;

typedef struct {
    App* app;
} MainModel;

static void uplink_send(App* app, const char* line);
static void send_version(App* app);
static void ota_request(App* app);
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

static void font_text(Canvas* c) {
    canvas_set_custom_u8g2_font(c, u8g2_font_uplink_cyr);
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

static int draw_wrapped_scroll(
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
    int total = 0;
    int drawn = 0;
    while(*p) {
        while(*p == ' ')
            p++;
        if(!*p) break;
        size_t best = 0, n = 0;
        while(p[n]) {
            size_t cl = utf8_len_at(p + n);
            if(n + cl >= sizeof(buf)) break;
            memcpy(buf, p, n + cl);
            buf[n + cl] = 0;
            if(canvas_string_width(c, buf) > w) break;
            n += cl;
            if(p[n] == ' ' || p[n] == 0) best = n;
        }
        if(n == 0) n = utf8_len_at(p); // a single glyph wider than the line: show it anyway
        if(best == 0 || (total >= skip && drawn == lines - 1 && p[best])) best = n;
        if(total >= skip && drawn < lines) {
            memcpy(buf, p, best);
            buf[best] = 0;
            canvas_draw_str_aligned(c, x, y + drawn * step, AlignLeft, AlignTop, buf);
            drawn++;
        }
        p += best;
        total++;
    }
    return total;
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
    bool used[ScreenKindCount + 1] = {false};
    for(int i = 0; i < 4; i++) {
        ScreenId s = app->settings.tabs[i];
        if(s >= ScreenOff || used[s]) continue;
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
                app->ota.notified = true;
                if(app->settings.auto_update) {
                    ota_request(app);
                } else {
                    app->alert = true;
                    app->alert_update = true;
                    app->alert_until = app->tick + ALERT_TICKS * 3;
                    uplink_notify(app, NotifyUpdate);
                }
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
        app->host_closed = true;
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

static void uplink_rx_callback(const uint8_t* data, uint16_t size, void* context) {
    App* app = context;
    furi_stream_buffer_send(app->rx, data, size, 0);
    view_dispatcher_send_custom_event(app->views, EvRx);
}

static void bt_status_callback(BtStatus status, void* context) {
    UNUSED(status);
    UNUSED(context);
}

/* ------------------------------------------------------------------ drawing */
static void draw_header(Canvas* c, App* app) {
    fg(c);
    canvas_draw_box(c, 0, 0, 128, 10);
    canvas_set_font(c, FontSecondary);
    static const char* label[ScreenKindCount] = {"SYS", "CDX", "CLD", "CMD"};
    int tabw = app->tab_count ? (102 / app->tab_count) : 25;
    if(tabw > 28) tabw = 28;
    for(int i = 0; i < app->tab_count; i++) {
        int x = 1 + i * tabw;
        ScreenId sid = app->tabs[i];
        bool active = (i == app->tab_index);
        bool attn = false;
        if(sid == ScreenCodex || sid == ScreenClaude)
            attn = list_attention(app, screen_kind(sid));
        else if(sid == ScreenCmd)
            attn = app->cmd.unseen || app->cmd.running;
        bool lit = active || (attn && (app->tick & 2));
        if(lit) {
            bg(c);
            canvas_draw_box(c, x, 1, tabw - 2, 8);
        }
        canvas_set_color(c, lit ? g_fg : g_bg); // lit: dark text on light box; else light on dark bar
        canvas_draw_str_aligned(c, x + (tabw - 2) / 2, 1, AlignCenter, AlignTop, label[sid]);
        if(attn && !active) canvas_draw_str_aligned(c, x + tabw - 3, 1, AlignCenter, AlignTop, "!");
    }
    bg(c);
    // The rightmost header cell is a real settings entry point: Right from the last
    // configured tab opens the same settings view as holding OK.
    canvas_draw_str_aligned(c, 126, 1, AlignRight, AlignTop, "SET");
    fg(c);
}

static void draw_meter(Canvas* c, int y, const char* label, uint8_t pct, const char* right) {
    canvas_draw_str_aligned(c, 2, y + 1, AlignLeft, AlignTop, label);
    canvas_draw_frame(c, 22, y, 78, 9);
    int w = 76 * (pct > 100 ? 100 : pct) / 100;
    if(w > 0) canvas_draw_box(c, 23, y + 1, w, 7);
    for(int t = 1; t < 4; t++) {
        canvas_set_color(c, ColorXOR);
        canvas_draw_dot(c, 22 + t * 19, y + 4);
    }
    fg(c);
    canvas_draw_str_aligned(c, 127, y + 1, AlignRight, AlignTop, right);
}

static void draw_sys(Canvas* c, App* app) {
    Sys* s = &app->sys;
    char a[16], b[16], line[64];
    canvas_set_font(c, FontSecondary);
    if(!s->valid) {
        canvas_draw_str_aligned(c, 64, 32, AlignCenter, AlignTop, "waiting for telemetry...");
        return;
    }
    if(app->settings.indicators == IndicatorsBars) {
        snprintf(line, sizeof(line), "%u%%", s->cpu);
        draw_meter(c, 12, "CPU", s->cpu, line);
        snprintf(line, sizeof(line), "%u%%", s->ram);
        draw_meter(c, 22, "RAM", s->ram, line);
        uint32_t net = s->up + s->dn, dsk = s->rd + s->wr;
        fmt_rate(a, sizeof(a), net);
        draw_meter(c, 32, "NET", (uint8_t)(net * 100 / s->net_max), a);
        fmt_rate(b, sizeof(b), dsk);
        draw_meter(c, 42, "DSK", (uint8_t)(dsk * 100 / s->dsk_max), b);
    } else {
        snprintf(line, sizeof(line), "CPU %u%%   RAM %u%%", s->cpu, s->ram);
        canvas_draw_str_aligned(c, 2, 13, AlignLeft, AlignTop, line);
        fmt_rate(a, sizeof(a), s->up);
        fmt_rate(b, sizeof(b), s->dn);
        snprintf(line, sizeof(line), "NET up %s  dn %s", a, b);
        canvas_draw_str_aligned(c, 2, 24, AlignLeft, AlignTop, line);
        fmt_rate(a, sizeof(a), s->rd);
        fmt_rate(b, sizeof(b), s->wr);
        snprintf(line, sizeof(line), "DSK rd %s  wr %s", a, b);
        canvas_draw_str_aligned(c, 2, 35, AlignLeft, AlignTop, line);
    }
    const int gx = 2, gy = 52, gh = 11;
    canvas_draw_line(c, gx, gy + gh, 125, gy + gh);
    for(int i = 0; i < s->hist_len; i++) {
        int x = gx + (HIST - s->hist_len + i) * 2;
        int h = s->cpu_hist[i] * gh / 100;
        if(h > 0) canvas_draw_line(c, x, gy + gh - h, x, gy + gh);
    }
    for(int x = gx; x < 126; x += 4)
        canvas_draw_dot(c, x, gy);
}

static void draw_glyph(Canvas* c, int x, int y, char st, uint32_t tick) {
    switch(st) {
    case 'W': {
        static const int8_t dx[4][4] = {{3, 0, 3, 6}, {0, 0, 6, 6}, {0, 3, 6, 3}, {0, 6, 6, 0}};
        const int8_t* d = dx[(tick / 2) % 4];
        canvas_draw_line(c, x + d[0], y + d[1], x + d[2], y + d[3]);
        break;
    }
    case 'A':
        if(tick & 2) {
            canvas_draw_box(c, x - 1, y - 1, 9, 9);
            bg(c);
        }
        canvas_draw_line(c, x + 3, y, x + 3, y + 4);
        canvas_draw_dot(c, x + 3, y + 6);
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

static void draw_list(Canvas* c, App* app, Kind k) {
    List* l = &app->lists[k];
    uint8_t total_visible = visible_count(app, k);
    if(l->cursor >= total_visible) l->cursor = total_visible ? total_visible - 1 : 0;
    if(l->scroll > l->cursor) l->scroll = l->cursor;
    bool large = app->settings.font == FontLarge;
    int rows = large ? 4 : 5;
    int rh = large ? 13 : 10;
    canvas_set_font(c, FontSecondary);
    if(!total_visible) {
        canvas_draw_str_aligned(c, 64, 26, AlignCenter, AlignTop, "NO ACTIVE SESSIONS");
        canvas_draw_str_aligned(
            c, 64, 38, AlignCenter, AlignTop, k == KindCodex ? "codex is quiet" : "claude is quiet");
        return;
    }
    if(l->cursor < l->scroll) l->scroll = l->cursor;
    if(l->cursor >= l->scroll + rows) l->scroll = l->cursor - rows + 1;
    bool bar = total_visible > rows;
    int right_edge = bar ? 123 : 126;
    for(int r = 0; r < rows && l->scroll + r < total_visible; r++) {
        int i = visible_raw_index(app, k, l->scroll + r);
        if(i < 0) break;
        Item* it = &l->items[i];
        int y = 11 + r * rh;
        draw_glyph(c, 2, y + (large ? 3 : 1), it->state, app->tick);
        char right[12];
        if(it->total)
            snprintf(right, sizeof(right), "%u/%u", it->done, it->total);
        else
            fmt_age(right, sizeof(right), it->age);
        canvas_set_font(c, FontSecondary);
        int rw = canvas_string_width(c, right);
        canvas_draw_str_aligned(c, right_edge, y + 2, AlignRight, AlignTop, right);
        font_text(c);
        draw_str_fit(c, 12, y + (large ? 2 : 0), it->name, right_edge - rw - 15);
        if(r == l->cursor) {
            canvas_set_color(c, ColorXOR);
            canvas_draw_box(c, 0, y, right_edge + 2, rh);
            fg(c);
        }
    }
    if(bar) {
        int track = rh * rows;
        int h = track * rows / total_visible;
        int y = 11 + (track - h) * l->scroll / (total_visible - rows);
        canvas_draw_line(c, 126, 11, 126, 11 + track);
        canvas_draw_box(c, 125, y, 3, h);
    }
}

static void draw_detail(Canvas* c, App* app, Kind k) {
    List* l = &app->lists[k];
    uint8_t total_visible = visible_count(app, k);
    if(l->cursor >= total_visible) {
        app->detail = false;
        return;
    }
    int raw = visible_raw_index(app, k, l->cursor);
    if(raw < 0) {
        app->detail = false;
        return;
    }
    Item* it = &l->items[raw];
    font_text(c);
    draw_str_fit(c, 2, 11, it->name, 124);
    canvas_set_font(c, FontSecondary);
    const char* st = state_text(it->state);
    int sw = canvas_string_width(c, st);
    canvas_draw_box(c, 2, 23, sw + 6, 9);
    bg(c);
    canvas_draw_str_aligned(c, 5, 24, AlignLeft, AlignTop, st);
    fg(c);
    char age[16], buf[24];
    fmt_age(age, sizeof(age), it->age);
    snprintf(buf, sizeof(buf), "%s ago", age);
    canvas_draw_str_aligned(c, 127, 24, AlignRight, AlignTop, buf);
    int y = 34;
    if(it->total) {
        snprintf(buf, sizeof(buf), "%u/%u", it->done, it->total);
        canvas_draw_frame(c, 2, y, 100, 7);
        int w = 98 * MIN(it->done, it->total) / it->total;
        if(w) canvas_draw_box(c, 3, y + 1, w, 5);
        canvas_draw_str_aligned(c, 127, y, AlignRight, AlignTop, buf);
        y += 9;
    }
    font_text(c);
    int rows = (64 - y) / 9;
    if(rows > 0) {
        int total = draw_wrapped_scroll(c, 2, y, 124, it->detail, 0, 9, UINT8_MAX);
        int max_scroll = total > rows ? total - rows : 0;
        if(l->detail_scroll > max_scroll) l->detail_scroll = max_scroll;
        draw_wrapped_scroll(c, 2, y, 124, it->detail, rows, 9, l->detail_scroll);
        if(max_scroll) {
            canvas_set_font(c, FontSecondary);
            canvas_draw_str_aligned(c, 126, 55, AlignCenter, AlignTop,
                                    l->detail_scroll < max_scroll ? "v" : "^");
        }
    }
}

static void draw_cmd(Canvas* c, App* app) {
    Cmd* cmd = &app->cmd;
    canvas_set_font(c, FontSecondary);
    char head[96];
    const char* tail = cmd->cwd;
    size_t cl = strlen(tail);
    if(cl > 18) tail += cl - 18;
    if(cmd->running)
        snprintf(head, sizeof(head), "%s> RUN  (Back=stop)", tail);
    else if(cmd->have_exit)
        snprintf(head, sizeof(head), "%s> exit %d", tail, cmd->exit_code);
    else
        snprintf(head, sizeof(head), "%s>", tail[0] ? tail : "cmd");
    char hbuf[96];
    clean_copy(hbuf, sizeof(hbuf), head);
    font_text(c);
    utf8_fit(c, hbuf, 126);
    canvas_draw_str_aligned(c, 2, 10, AlignLeft, AlignTop, hbuf);
    canvas_set_font(c, FontSecondary);
    canvas_draw_line(c, 0, 19, 128, 19);

    if(cmd->count == 0) {
        canvas_draw_str_aligned(c, 64, 34, AlignCenter, AlignTop, "OK: type a command");
        canvas_draw_str_aligned(c, 64, 46, AlignCenter, AlignTop, "cmd.exe in your pocket");
        return;
    }
    const int vis = 5, top = 21, step = 8;
    int total = cmd->count;
    int bottom = total - cmd->scroll;
    if(bottom > total) bottom = total;
    if(bottom < 1) bottom = 1;
    int start = bottom - vis;
    if(start < 0) start = 0;
    bool sb = total > vis;
    int right = sb ? 124 : 128;
    font_text(c);
    for(int r = 0; start + r < bottom; r++) {
        char buf[CMD_COLW];
        clean_copy(buf, sizeof(buf), cmd_line(cmd, start + r));
        utf8_fit(c, buf, right - 2);
        canvas_draw_str_aligned(c, 1, top + r * step, AlignLeft, AlignTop, buf);
    }
    canvas_set_font(c, FontSecondary);
    if(sb) {
        int track = vis * step;
        int h = track * vis / total;
        int y = top + (track - h) * start / (total - vis);
        canvas_draw_box(c, 126, y, 2, h);
    }
}

static void draw_alert(Canvas* c, App* app) {
    bg(c);
    canvas_draw_box(c, 5, 13, 118, 40);
    fg(c);
    canvas_draw_frame(c, 5, 13, 118, 40);
    canvas_draw_box(c, 7, 15, 114, 11);
    canvas_set_font(c, FontSecondary);
    if(app->alert_update) {
        char line[40];
        bg(c);
        canvas_draw_str_aligned(c, 64, 17, AlignCenter, AlignTop, ">> UPDATE AVAILABLE <<");
        fg(c);
        snprintf(line, sizeof(line), "%s -> %s", UPLINK_VERSION, app->ota.tag);
        canvas_draw_str_aligned(c, 64, 29, AlignCenter, AlignTop, line);
        canvas_draw_str_aligned(c, 64, 40, AlignCenter, AlignTop, "OK: install   Back: later");
        return;
    }
    bg(c);
    canvas_draw_str_aligned(
        c, 64, 17, AlignCenter, AlignTop,
        app->alert_state == 'A' ? "!! APPROVAL NEEDED !!" : ">> YOUR TURN <<");
    fg(c);
    canvas_draw_str_aligned(
        c, 64, 28, AlignCenter, AlignTop, app->alert_kind == KindCodex ? "CODEX" : "CLAUDE");
    font_text(c);
    char name[64];
    clean_copy(name, sizeof(name), app->alert_name);
    utf8_fit(c, name, 110);
    canvas_draw_str_aligned(c, 64, 38, AlignCenter, AlignTop, name);
}

static void draw_ota(Canvas* c, App* app) {
    Ota* o = &app->ota;
    char line[48];
    bg(c);
    canvas_draw_box(c, 4, 12, 120, 42);
    fg(c);
    canvas_draw_frame(c, 4, 12, 120, 42);
    canvas_draw_box(c, 6, 14, 116, 11);
    canvas_set_font(c, FontSecondary);
    bg(c);
    if(o->state == OtaDone) {
        canvas_draw_str_aligned(c, 64, 16, AlignCenter, AlignTop, "UPDATED - RESTARTING");
    } else if(o->state == OtaFailed) {
        canvas_draw_str_aligned(c, 64, 16, AlignCenter, AlignTop, "UPDATE FAILED");
    } else {
        canvas_draw_str_aligned(c, 64, 16, AlignCenter, AlignTop, "UPDATING");
    }
    fg(c);
    if(o->state == OtaFailed) {
        canvas_draw_str_aligned(c, 64, 28, AlignCenter, AlignTop, o->error[0] ? o->error : "error");
        canvas_draw_str_aligned(c, 64, 40, AlignCenter, AlignTop, "any key: close");
        return;
    }
    snprintf(line, sizeof(line), "%s -> %s", UPLINK_VERSION, o->tag);
    canvas_draw_str_aligned(c, 64, 27, AlignCenter, AlignTop, line);
    uint8_t pct = (o->state == OtaRequested) ? 0 : ota_percent(o);
    canvas_draw_frame(c, 10, 39, 108, 9);
    int w = 106 * pct / 100;
    if(w) canvas_draw_box(c, 11, 40, w, 7);
    if(o->state == OtaRequested) {
        snprintf(line, sizeof(line), "waiting for PC...");
    } else {
        snprintf(line, sizeof(line), "%u%%", pct);
    }
    canvas_set_color(c, ColorXOR); // readable over both the filled and the empty part
    canvas_draw_str_aligned(c, 64, 40, AlignCenter, AlignTop, line);
    fg(c);
}

static void draw_offline(Canvas* c, App* app) {
    canvas_draw_box(c, 0, 0, 128, 12);
    bg(c);
    canvas_set_font(c, FontPrimary);
    canvas_draw_str_aligned(c, 64, 2, AlignCenter, AlignTop, "DEDSEC // UPLINK");
    fg(c);
    canvas_draw_icon(c, 2, 16, &I_hood_26x30);
    canvas_set_font(c, FontSecondary);
    char name[32];
    snprintf(name, sizeof(name), "%s %s", UPLINK_NAME_PREFIX, furi_hal_version_get_name_ptr());
    canvas_draw_str_aligned(
        c, 32, 17, AlignLeft, AlignTop, app->host_closed ? "HOST WENT DARK" : "WAITING FOR HOST");
    canvas_draw_str_aligned(c, 32, 27, AlignLeft, AlignTop, "BLE name:");
    canvas_draw_str_aligned(c, 32, 36, AlignLeft, AlignTop, name);
    canvas_draw_str_aligned(c, 32, 46, AlignLeft, AlignTop, "run uplink on PC");
    int pos = (app->tick * 3) % 220;
    int x = pos < 110 ? pos : 219 - pos;
    canvas_draw_frame(c, 2, 58, 124, 5);
    canvas_draw_box(c, 3 + x, 59, 12, 3);
}

static void main_draw(Canvas* c, void* model) {
    MainModel* m = model;
    App* app = m->app;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    bool inv = app->settings.theme == ThemeInverted;
    // inverted theme = swap the palette and paint a dark background; every draw uses fg()/bg()
    g_fg = inv ? ColorWhite : ColorBlack;
    g_bg = inv ? ColorBlack : ColorWhite;
    canvas_clear(c);
    if(inv) {
        canvas_set_color(c, ColorBlack);
        canvas_draw_box(c, 0, 0, 128, 64);
    }
    fg(c);
    ScreenId screen = current_screen(app);
    bool offline = (!app->link || app->host_closed) && screen != ScreenCmd;
    if(offline) {
        draw_offline(c, app);
    } else {
        draw_header(c, app);
        if(screen == ScreenSys)
            draw_sys(c, app);
        else if(screen == ScreenCmd) {
            if(!app->link)
                canvas_draw_str_aligned(c, 64, 34, AlignCenter, AlignTop, "link down");
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
    text_input_set_header_text(app->keyboard, "Command (OK=save)");
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
        if(screen == ScreenCmd) {
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
        if(screen == ScreenCmd) {
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
        if(screen == ScreenCmd) {
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
        if(app->detail) {
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
static const char* const theme_vals[] = {"Normal", "Inverted"};
static const char* const font_vals[] = {"Normal", "Large"};
static const char* const tab_vals[] = {"SYS", "CDX", "CLD", "CMD", "Off"};
static const char* const update_vals[] = {"Notify", "Auto"};

enum {
    SetVibro,
    SetCmdVibro,
    SetLed,
    SetBacklight,
    SetIndicators,
    SetTheme,
    SetFont,
    SetTab0,
    SetTab1,
    SetTab2,
    SetTab3,
    SetAutoUpdate,
    SetVersion, // read-only; OK installs a pending update
};

static void setting_changed(VariableItem* item) {
    App* app = variable_item_get_context(item);
    uint8_t idx = variable_item_get_current_value_index(item);
    uint8_t sel = variable_item_list_get_selected_item_index(app->settings_view);
    const char* text;
    switch(sel) {
    case SetIndicators:
        text = ind_vals[idx];
        break;
    case SetTheme:
        text = theme_vals[idx];
        break;
    case SetFont:
        text = font_vals[idx];
        break;
    case SetTab0:
    case SetTab1:
    case SetTab2:
    case SetTab3:
        text = tab_vals[idx];
        break;
    case SetAutoUpdate:
        text = update_vals[idx];
        break;
    case SetVersion:
        return;
    default:
        text = on_off[idx];
        break;
    }
    variable_item_set_current_value_text(item, text);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    switch(sel) {
    case SetVibro:
        app->settings.vibro = idx;
        break;
    case SetCmdVibro:
        app->settings.cmd_vibro = idx;
        break;
    case SetLed:
        app->settings.led = idx;
        break;
    case SetBacklight:
        app->settings.backlight = idx;
        break;
    case SetIndicators:
        app->settings.indicators = idx;
        break;
    case SetTheme:
        app->settings.theme = idx;
        break;
    case SetFont:
        app->settings.font = idx;
        break;
    case SetTab0:
    case SetTab1:
    case SetTab2:
    case SetTab3:
        app->settings.tabs[sel - SetTab0] = idx;
        rebuild_tabs(app);
        break;
    case SetAutoUpdate:
        app->settings.auto_update = idx;
        break;
    default:
        break;
    }
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

static void add_toggle(
    App* app,
    const char* label,
    const char* const* vals,
    int count,
    uint8_t val) {
    VariableItem* it =
        variable_item_list_add(app->settings_view, label, count, setting_changed, app);
    if(val >= count) val = 0;
    variable_item_set_current_value_index(it, val);
    variable_item_set_current_value_text(it, vals[val]);
}

static void build_settings(App* app) {
    variable_item_list_reset(app->settings_view);
    add_toggle(app, "Vibration", on_off, 2, app->settings.vibro);
    add_toggle(app, "Vibrate on cmd reply", on_off, 2, app->settings.cmd_vibro);
    add_toggle(app, "LED alerts", on_off, 2, app->settings.led);
    add_toggle(app, "Wake screen on alert", on_off, 2, app->settings.backlight);
    add_toggle(app, "Indicators", ind_vals, 2, app->settings.indicators);
    add_toggle(app, "Theme", theme_vals, 2, app->settings.theme);
    add_toggle(app, "Font size", font_vals, 2, app->settings.font);
    add_toggle(app, "Tab 1", tab_vals, 5, app->settings.tabs[0]);
    add_toggle(app, "Tab 2", tab_vals, 5, app->settings.tabs[1]);
    add_toggle(app, "Tab 3", tab_vals, 5, app->settings.tabs[2]);
    add_toggle(app, "Tab 4", tab_vals, 5, app->settings.tabs[3]);
    add_toggle(app, "Updates", update_vals, 2, app->settings.auto_update);
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
        furi_mutex_release(app->mutex);
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

    app->timer = furi_timer_alloc(tick_callback, FuriTimerTypePeriodic, app);
    furi_timer_start(app->timer, furi_ms_to_ticks(250));

    go_view(app, ViewMain);
    view_dispatcher_run(app->views);

    furi_timer_stop(app->timer);
    furi_timer_free(app->timer);

    bt_set_status_changed_callback(app->bt, NULL, NULL);
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
