#include "fake_sdk.h"

size_t strlcpy(char* dst, const char* src, size_t size) {
    size_t n = strlen(src);
    if(size) {
        size_t m = n < size - 1 ? n : size - 1;
        memcpy(dst, src, m);
        dst[m] = 0;
    }
    return n;
}

/* ---------------------------------------------------------------- canvas over u8g2 */
static const u8x8_display_info_t host_info = {
    .chip_enable_level = 0,
    .chip_disable_level = 1,
    .tile_width = 16,
    .tile_height = 8,
    .pixel_width = 128,
    .pixel_height = 64,
};

static uint8_t host_display_cb(u8x8_t* u8x8, uint8_t msg, uint8_t arg_int, void* arg_ptr) {
    (void)arg_int;
    (void)arg_ptr;
    if(msg == U8X8_MSG_DISPLAY_SETUP_MEMORY) u8x8_d_helper_display_setup_memory(u8x8, &host_info);
    return 1;
}

static uint8_t g_fb[128 * 64 / 8];

void canvas_init_host(Canvas* c) {
    memset(c, 0, sizeof(*c));
    u8g2_SetupDisplay(&c->fb, host_display_cb, u8x8_cad_empty, u8x8_byte_empty, u8x8_dummy_cb);
    u8g2_SetupBuffer(&c->fb, g_fb, 8, u8g2_ll_hvline_vertical_top_lsb, U8G2_R0);
    u8g2_InitDisplay(&c->fb);
    c->width = 128;
    c->height = 64;
    c->orientation = CanvasOrientationHorizontal;
    u8g2_ClearBuffer(&c->fb);
    u8g2_SetDrawColor(&c->fb, ColorBlack);
    canvas_set_font(c, FontSecondary);
}

const uint8_t* canvas_host_buffer(void) {
    return g_fb;
}

void canvas_set_orientation(Canvas* c, CanvasOrientation o) {
    const u8g2_cb_t* rot = U8G2_R0;
    bool vertical_now = c->orientation == CanvasOrientationVertical ||
                        c->orientation == CanvasOrientationVerticalFlip;
    bool vertical_next = o == CanvasOrientationVertical || o == CanvasOrientationVerticalFlip;
    switch(o) {
    case CanvasOrientationHorizontal:
        rot = U8G2_R0;
        break;
    case CanvasOrientationHorizontalFlip:
        rot = U8G2_R2;
        break;
    case CanvasOrientationVertical:
        rot = U8G2_R3;
        break;
    case CanvasOrientationVerticalFlip:
        rot = U8G2_R1;
        break;
    }
    if(vertical_now != vertical_next) {
        size_t t = c->width;
        c->width = c->height;
        c->height = t;
    }
    u8g2_SetDisplayRotation(&c->fb, rot);
    c->orientation = o;
}

size_t canvas_width(const Canvas* c) {
    return c->width;
}
size_t canvas_height(const Canvas* c) {
    return c->height;
}
size_t canvas_current_font_height(const Canvas* c) {
    size_t h = u8g2_GetMaxCharHeight((u8g2_t*)&c->fb);
    if(c->fb.font == u8g2_font_haxrcorp4089_tr) h += 1;
    return h;
}
void canvas_clear(Canvas* c) {
    u8g2_ClearBuffer(&c->fb);
}
void canvas_set_color(Canvas* c, Color color) {
    u8g2_SetDrawColor(&c->fb, (uint8_t)color);
}
void canvas_set_font(Canvas* c, Font font) {
    u8g2_SetFontMode(&c->fb, 1);
    switch(font) {
    case FontPrimary:
        u8g2_SetFont(&c->fb, u8g2_font_helvB08_tr);
        break;
    case FontSecondary:
        u8g2_SetFont(&c->fb, u8g2_font_haxrcorp4089_tr);
        break;
    case FontKeyboard:
        u8g2_SetFont(&c->fb, u8g2_font_profont11_mr);
        break;
    case FontBigNumbers:
        u8g2_SetFont(&c->fb, u8g2_font_profont22_tn);
        break;
    default:
        u8g2_SetFont(&c->fb, u8g2_font_5x7_tr);
        break;
    }
}
void canvas_set_custom_u8g2_font(Canvas* c, const uint8_t* font) {
    u8g2_SetFontMode(&c->fb, 1);
    u8g2_SetFont(&c->fb, font);
}
void canvas_draw_str(Canvas* c, int32_t x, int32_t y, const char* s) {
    if(s) u8g2_DrawUTF8(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, s);
}
void canvas_draw_str_aligned(Canvas* c, int32_t x, int32_t y, Align h, Align v, const char* s) {
    if(!s) return;
    if(h == AlignRight) x -= u8g2_GetUTF8Width(&c->fb, s);
    if(h == AlignCenter) x -= u8g2_GetUTF8Width(&c->fb, s) / 2;
    if(v == AlignTop) y += u8g2_GetAscent(&c->fb);
    if(v == AlignCenter) y += u8g2_GetAscent(&c->fb) / 2;
    u8g2_DrawUTF8(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, s);
}
uint16_t canvas_string_width(Canvas* c, const char* s) {
    return s ? u8g2_GetUTF8Width(&c->fb, s) : 0;
}
void canvas_draw_box(Canvas* c, int32_t x, int32_t y, size_t w, size_t h) {
    u8g2_DrawBox(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, (u8g2_uint_t)w, (u8g2_uint_t)h);
}
void canvas_draw_frame(Canvas* c, int32_t x, int32_t y, size_t w, size_t h) {
    u8g2_DrawFrame(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, (u8g2_uint_t)w, (u8g2_uint_t)h);
}
void canvas_draw_rbox(Canvas* c, int32_t x, int32_t y, size_t w, size_t h, size_t r) {
    u8g2_DrawRBox(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, (u8g2_uint_t)w, (u8g2_uint_t)h, (u8g2_uint_t)r);
}
void canvas_draw_rframe(Canvas* c, int32_t x, int32_t y, size_t w, size_t h, size_t r) {
    u8g2_DrawRFrame(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, (u8g2_uint_t)w, (u8g2_uint_t)h, (u8g2_uint_t)r);
}
void canvas_draw_line(Canvas* c, int32_t x1, int32_t y1, int32_t x2, int32_t y2) {
    u8g2_DrawLine(&c->fb, (u8g2_uint_t)x1, (u8g2_uint_t)y1, (u8g2_uint_t)x2, (u8g2_uint_t)y2);
}
void canvas_draw_dot(Canvas* c, int32_t x, int32_t y) {
    u8g2_DrawPixel(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y);
}
void canvas_draw_icon(Canvas* c, int32_t x, int32_t y, const Icon* icon) {
    u8g2_DrawXBM(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, icon->width, icon->height, icon->xbm);
}

/* ---------------------------------------------------------------- no-op furi */
FuriMutex* furi_mutex_alloc(FuriMutexType type) {
    (void)type;
    return (FuriMutex*)1;
}
void furi_mutex_free(FuriMutex* m) {
    (void)m;
}
int furi_mutex_acquire(FuriMutex* m, uint32_t timeout) {
    (void)m;
    (void)timeout;
    return 0;
}
int furi_mutex_release(FuriMutex* m) {
    (void)m;
    return 0;
}
FuriStreamBuffer* furi_stream_buffer_alloc(size_t size, size_t trigger) {
    (void)size;
    (void)trigger;
    return (FuriStreamBuffer*)1;
}
void furi_stream_buffer_free(FuriStreamBuffer* b) {
    (void)b;
}
size_t furi_stream_buffer_send(FuriStreamBuffer* b, const void* d, size_t n, uint32_t t) {
    (void)b;
    (void)d;
    (void)t;
    return n;
}
size_t furi_stream_buffer_receive(FuriStreamBuffer* b, void* d, size_t n, uint32_t t) {
    (void)b;
    (void)d;
    (void)n;
    (void)t;
    return 0;
}
void* furi_record_open(const char* name) {
    (void)name;
    return NULL;
}
void furi_record_close(const char* name) {
    (void)name;
}
FuriTimer* furi_timer_alloc(FuriTimerCallback cb, FuriTimerType type, void* ctx) {
    (void)cb;
    (void)type;
    (void)ctx;
    return NULL;
}
void furi_timer_free(FuriTimer* t) {
    (void)t;
}
int furi_timer_start(FuriTimer* t, uint32_t ticks) {
    (void)t;
    (void)ticks;
    return 0;
}
int furi_timer_stop(FuriTimer* t) {
    (void)t;
    return 0;
}
uint32_t furi_ms_to_ticks(uint32_t ms) {
    return ms;
}
void furi_delay_ms(uint32_t ms) {
    (void)ms;
}
const char* furi_hal_version_get_name_ptr(void) {
    return "Dolphy";
}
bool furi_hal_bt_start_advertising(void) {
    return true;
}

/* ---------------------------------------------------------------- views */
static uint8_t g_model[64];
View* view_alloc(void) {
    return (View*)1;
}
void view_free(View* v) {
    (void)v;
}
void view_set_context(View* v, void* ctx) {
    (void)v;
    (void)ctx;
}
void view_set_draw_callback(View* v, ViewDrawCallback cb) {
    (void)v;
    (void)cb;
}
void view_set_input_callback(View* v, ViewInputCallback cb) {
    (void)v;
    (void)cb;
}
void view_allocate_model(View* v, ViewModelType t, size_t s) {
    (void)v;
    (void)t;
    (void)s;
}
void* view_get_model(View* v) {
    (void)v;
    return g_model;
}
void view_commit_model(View* v, bool u) {
    (void)v;
    (void)u;
}
void view_set_orientation(View* v, ViewOrientation o) {
    (void)v;
    (void)o;
}
ViewDispatcher* view_dispatcher_alloc(void) {
    return NULL;
}
void view_dispatcher_free(ViewDispatcher* d) {
    (void)d;
}
void view_dispatcher_set_event_callback_context(ViewDispatcher* d, void* c) {
    (void)d;
    (void)c;
}
void view_dispatcher_set_custom_event_callback(ViewDispatcher* d, ViewDispatcherCustomEventCallback cb) {
    (void)d;
    (void)cb;
}
void view_dispatcher_set_navigation_event_callback(
    ViewDispatcher* d,
    ViewDispatcherNavigationEventCallback cb) {
    (void)d;
    (void)cb;
}
void view_dispatcher_add_view(ViewDispatcher* d, uint32_t id, View* v) {
    (void)d;
    (void)id;
    (void)v;
}
void view_dispatcher_remove_view(ViewDispatcher* d, uint32_t id) {
    (void)d;
    (void)id;
}
void view_dispatcher_attach_to_gui(ViewDispatcher* d, Gui* g, ViewDispatcherType t) {
    (void)d;
    (void)g;
    (void)t;
}
void view_dispatcher_switch_to_view(ViewDispatcher* d, uint32_t id) {
    (void)d;
    (void)id;
}
void view_dispatcher_run(ViewDispatcher* d) {
    (void)d;
}
void view_dispatcher_stop(ViewDispatcher* d) {
    (void)d;
}
void view_dispatcher_send_custom_event(ViewDispatcher* d, uint32_t e) {
    (void)d;
    (void)e;
}
TextInput* text_input_alloc(void) {
    return NULL;
}
void text_input_free(TextInput* t) {
    (void)t;
}
View* text_input_get_view(TextInput* t) {
    (void)t;
    return NULL;
}
void text_input_reset(TextInput* t) {
    (void)t;
}
void text_input_set_header_text(TextInput* t, const char* s) {
    (void)t;
    (void)s;
}
void text_input_set_result_callback(
    TextInput* t,
    TextInputCallback cb,
    void* ctx,
    char* buf,
    size_t size,
    bool clear) {
    (void)t;
    (void)cb;
    (void)ctx;
    (void)buf;
    (void)size;
    (void)clear;
}
VariableItemList* variable_item_list_alloc(void) {
    return NULL;
}
void variable_item_list_free(VariableItemList* l) {
    (void)l;
}
View* variable_item_list_get_view(VariableItemList* l) {
    (void)l;
    return NULL;
}
void variable_item_list_reset(VariableItemList* l) {
    (void)l;
}
VariableItem* variable_item_list_add(
    VariableItemList* l,
    const char* label,
    uint8_t count,
    VariableItemChangeCallback cb,
    void* ctx) {
    (void)l;
    (void)label;
    (void)count;
    (void)cb;
    (void)ctx;
    return NULL;
}
void variable_item_list_set_enter_callback(VariableItemList* l, VariableItemListEnterCallback cb, void* c) {
    (void)l;
    (void)cb;
    (void)c;
}
void variable_item_list_set_selected_item(VariableItemList* l, uint8_t i) {
    (void)l;
    (void)i;
}
uint8_t variable_item_list_get_selected_item_index(VariableItemList* l) {
    (void)l;
    return 0;
}
void* variable_item_get_context(VariableItem* item) {
    (void)item;
    return NULL;
}
uint8_t variable_item_get_current_value_index(VariableItem* item) {
    (void)item;
    return 0;
}
void variable_item_set_current_value_index(VariableItem* item, uint8_t i) {
    (void)item;
    (void)i;
}
void variable_item_set_current_value_text(VariableItem* item, const char* s) {
    (void)item;
    (void)s;
}

/* ---------------------------------------------------------------- notification, bt, loader */
void notification_message_block(NotificationApp* app, const NotificationSequence* seq) {
    (void)app;
    (void)seq;
}
const NotificationMessage message_red_255, message_red_0, message_green_255, message_green_0,
    message_blue_255, message_blue_0, message_display_backlight_on, message_force_vibro_setting_on,
    message_force_vibro_setting_off, message_vibro_on, message_vibro_off, message_delay_100,
    message_delay_250;
void bt_disconnect(Bt* bt) {
    (void)bt;
}
void bt_keys_storage_set_storage_path(Bt* bt, const char* p) {
    (void)bt;
    (void)p;
}
void bt_keys_storage_set_default_path(Bt* bt) {
    (void)bt;
}
FuriHalBleProfileBase* bt_profile_start(Bt* bt, const FuriHalBleProfileTemplate* t, void* p) {
    (void)bt;
    (void)t;
    (void)p;
    return NULL;
}
bool bt_profile_restore_default(Bt* bt) {
    (void)bt;
    return true;
}
void bt_set_status_changed_callback(Bt* bt, BtStatusChangedCallback cb, void* c) {
    (void)bt;
    (void)cb;
    (void)c;
}
void loader_enqueue_launch(Loader* l, const char* n, const char* a, LoaderDeferredLaunchFlag f) {
    (void)l;
    (void)n;
    (void)a;
    (void)f;
}

size_t canvas_glyph_width(Canvas* c, uint16_t symbol) {
    return u8g2_GetGlyphWidth(&c->fb, symbol);
}

void canvas_draw_disc(Canvas* c, int32_t x, int32_t y, size_t r) {
    u8g2_DrawDisc(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, (u8g2_uint_t)r, U8G2_DRAW_ALL);
}
void canvas_draw_circle(Canvas* c, int32_t x, int32_t y, size_t r) {
    u8g2_DrawCircle(&c->fb, (u8g2_uint_t)x, (u8g2_uint_t)y, (u8g2_uint_t)r, U8G2_DRAW_ALL);
}
size_t furi_stream_buffer_spaces_available(FuriStreamBuffer* b) {
    (void)b;
    return 4096;
}
uint32_t furi_hal_rtc_get_timestamp(void) {
    return 1791366000u; // 2026-10-07 10:20 local
}
void datetime_timestamp_to_datetime(uint32_t ts, DateTime* dt) {
    uint32_t days = ts / 86400, rem = ts % 86400;
    dt->hour = rem / 3600;
    dt->minute = rem % 3600 / 60;
    dt->second = rem % 60;
    dt->year = 1970 + days / 365; // display only
    dt->month = 1;
    dt->day = 1;
    dt->weekday = 0;
}
uint32_t datetime_datetime_to_timestamp(DateTime* dt) {
    return dt->hour * 3600u + dt->minute * 60u + dt->second;
}
