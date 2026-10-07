/* Minimal stand-ins for the Flipper SDK so uplink.c's drawing code runs on a PC.
 * Canvas calls are real (u8g2, same semantics as the firmware's canvas.c);
 * everything else is a no-op. */
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "u8g2.h"

#define UNUSED(x) (void)(x)
#define COUNT_OF(x) (sizeof(x) / sizeof(x[0]))
#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#endif
#define FURI_LOG_E(tag, ...) ((void)0)
#define FURI_LOG_W(tag, ...) ((void)0)
#define FURI_LOG_I(tag, ...) ((void)0)
#define FURI_LOG_D(tag, ...) ((void)0)
#define furi_assert(x)       ((void)0)
#define furi_check(x)        ((void)0)
#define APP_DATA_PATH(p)     "/ext/apps_data/dedsec_uplink/" p
#define FuriWaitForever      0xFFFFFFFFu
#define RECORD_GUI           "gui"
#define RECORD_NOTIFICATION  "notification"
#define RECORD_BT            "bt"
#define RECORD_LOADER        "loader"
#define RECORD_STORAGE       "storage"

size_t strlcpy(char* dst, const char* src, size_t size);

/* ---- furi */
typedef struct FuriMutex FuriMutex;
typedef struct FuriTimer FuriTimer;
typedef struct FuriStreamBuffer FuriStreamBuffer;
typedef enum { FuriMutexTypeNormal } FuriMutexType;
typedef enum { FuriTimerTypeOnce, FuriTimerTypePeriodic } FuriTimerType;
typedef void (*FuriTimerCallback)(void* context);
FuriMutex* furi_mutex_alloc(FuriMutexType type);
void furi_mutex_free(FuriMutex* m);
int furi_mutex_acquire(FuriMutex* m, uint32_t timeout);
int furi_mutex_release(FuriMutex* m);
FuriStreamBuffer* furi_stream_buffer_alloc(size_t size, size_t trigger);
void furi_stream_buffer_free(FuriStreamBuffer* b);
size_t furi_stream_buffer_send(FuriStreamBuffer* b, const void* data, size_t len, uint32_t timeout);
size_t furi_stream_buffer_receive(FuriStreamBuffer* b, void* data, size_t len, uint32_t timeout);
size_t furi_stream_buffer_spaces_available(FuriStreamBuffer* b);
void* furi_record_open(const char* name);
void furi_record_close(const char* name);
FuriTimer* furi_timer_alloc(FuriTimerCallback cb, FuriTimerType type, void* ctx);
void furi_timer_free(FuriTimer* t);
int furi_timer_start(FuriTimer* t, uint32_t ticks);
int furi_timer_stop(FuriTimer* t);
uint32_t furi_ms_to_ticks(uint32_t ms);
void furi_delay_ms(uint32_t ms);
const char* furi_hal_version_get_name_ptr(void);
bool furi_hal_bt_start_advertising(void);

/* ---- rtc / datetime */
typedef struct {
    uint8_t hour, minute, second, day, month;
    uint16_t year;
    uint8_t weekday;
} DateTime;
uint32_t furi_hal_rtc_get_timestamp(void);
void datetime_timestamp_to_datetime(uint32_t ts, DateTime* dt);
uint32_t datetime_datetime_to_timestamp(DateTime* dt);
#define CLAMP(x, upper, lower) (MIN(upper, MAX(x, lower)))

/* ---- canvas */
typedef enum { ColorWhite = 0, ColorBlack = 1, ColorXOR = 2 } Color;
typedef enum {
    FontPrimary,
    FontSecondary,
    FontKeyboard,
    FontBigNumbers,
    FontBatteryPercent,
    FontTotalNumber,
} Font;
typedef enum { AlignLeft, AlignRight, AlignTop, AlignBottom, AlignCenter } Align;
typedef enum {
    CanvasOrientationHorizontal,
    CanvasOrientationHorizontalFlip,
    CanvasOrientationVertical,
    CanvasOrientationVerticalFlip,
} CanvasOrientation;
typedef struct {
    u8g2_t fb;
    size_t width, height;
    CanvasOrientation orientation;
} Canvas;
typedef struct {
    uint16_t width, height;
    const uint8_t* xbm; /* host only: 1 bit per pixel, LSB first, rows padded to bytes */
} Icon;

void canvas_init_host(Canvas* c);
void canvas_set_orientation(Canvas* c, CanvasOrientation o);
size_t canvas_width(const Canvas* c);
size_t canvas_height(const Canvas* c);
size_t canvas_current_font_height(const Canvas* c);
void canvas_clear(Canvas* c);
void canvas_set_color(Canvas* c, Color color);
void canvas_set_font(Canvas* c, Font font);
void canvas_set_custom_u8g2_font(Canvas* c, const uint8_t* font);
void canvas_draw_str(Canvas* c, int32_t x, int32_t y, const char* s);
void canvas_draw_str_aligned(Canvas* c, int32_t x, int32_t y, Align h, Align v, const char* s);
uint16_t canvas_string_width(Canvas* c, const char* s);
size_t canvas_glyph_width(Canvas* c, uint16_t symbol);
void canvas_draw_box(Canvas* c, int32_t x, int32_t y, size_t w, size_t h);
void canvas_draw_frame(Canvas* c, int32_t x, int32_t y, size_t w, size_t h);
void canvas_draw_rbox(Canvas* c, int32_t x, int32_t y, size_t w, size_t h, size_t r);
void canvas_draw_rframe(Canvas* c, int32_t x, int32_t y, size_t w, size_t h, size_t r);
void canvas_draw_line(Canvas* c, int32_t x1, int32_t y1, int32_t x2, int32_t y2);
void canvas_draw_dot(Canvas* c, int32_t x, int32_t y);
void canvas_draw_icon(Canvas* c, int32_t x, int32_t y, const Icon* icon);
void canvas_draw_disc(Canvas* c, int32_t x, int32_t y, size_t r);
void canvas_draw_circle(Canvas* c, int32_t x, int32_t y, size_t r);

/* ---- input */
typedef enum { InputTypePress, InputTypeRelease, InputTypeShort, InputTypeLong, InputTypeRepeat } InputType;
typedef enum { InputKeyUp, InputKeyDown, InputKeyRight, InputKeyLeft, InputKeyOk, InputKeyBack, InputKeyMAX } InputKey;
typedef struct {
    uint32_t sequence;
    InputKey key;
    InputType type;
} InputEvent;

/* ---- views */
typedef struct View View;
typedef struct ViewDispatcher ViewDispatcher;
typedef struct Gui Gui;
typedef enum {
    ViewOrientationHorizontal,
    ViewOrientationHorizontalFlip,
    ViewOrientationVertical,
    ViewOrientationVerticalFlip,
} ViewOrientation;
typedef enum { ViewModelTypeNone, ViewModelTypeLockFree, ViewModelTypeLocking } ViewModelType;
typedef enum { ViewDispatcherTypeDesktop, ViewDispatcherTypeWindow, ViewDispatcherTypeFullscreen } ViewDispatcherType;
typedef void (*ViewDrawCallback)(Canvas* canvas, void* model);
typedef bool (*ViewInputCallback)(InputEvent* event, void* context);
typedef bool (*ViewDispatcherCustomEventCallback)(void* context, uint32_t event);
typedef bool (*ViewDispatcherNavigationEventCallback)(void* context);
View* view_alloc(void);
void view_free(View* v);
void view_set_context(View* v, void* ctx);
void view_set_draw_callback(View* v, ViewDrawCallback cb);
void view_set_input_callback(View* v, ViewInputCallback cb);
void view_allocate_model(View* v, ViewModelType type, size_t size);
void* view_get_model(View* v);
void view_commit_model(View* v, bool update);
void view_set_orientation(View* v, ViewOrientation o);
#define with_view_model(view, type, code, update) \
    {                                             \
        type = view_get_model(view);              \
        {code};                                   \
        view_commit_model(view, update);          \
    }
ViewDispatcher* view_dispatcher_alloc(void);
void view_dispatcher_free(ViewDispatcher* d);
void view_dispatcher_set_event_callback_context(ViewDispatcher* d, void* ctx);
void view_dispatcher_set_custom_event_callback(ViewDispatcher* d, ViewDispatcherCustomEventCallback cb);
void view_dispatcher_set_navigation_event_callback(ViewDispatcher* d, ViewDispatcherNavigationEventCallback cb);
void view_dispatcher_add_view(ViewDispatcher* d, uint32_t id, View* v);
void view_dispatcher_remove_view(ViewDispatcher* d, uint32_t id);
void view_dispatcher_attach_to_gui(ViewDispatcher* d, Gui* gui, ViewDispatcherType type);
void view_dispatcher_switch_to_view(ViewDispatcher* d, uint32_t id);
void view_dispatcher_run(ViewDispatcher* d);
void view_dispatcher_stop(ViewDispatcher* d);
void view_dispatcher_send_custom_event(ViewDispatcher* d, uint32_t event);

typedef struct TextInput TextInput;
typedef void (*TextInputCallback)(void* context);
TextInput* text_input_alloc(void);
void text_input_free(TextInput* t);
View* text_input_get_view(TextInput* t);
void text_input_reset(TextInput* t);
void text_input_set_header_text(TextInput* t, const char* text);
void text_input_set_result_callback(
    TextInput* t, TextInputCallback cb, void* ctx, char* buf, size_t size, bool clear);

typedef struct VariableItemList VariableItemList;
typedef struct VariableItem VariableItem;
typedef void (*VariableItemChangeCallback)(VariableItem* item);
typedef void (*VariableItemListEnterCallback)(void* context, uint32_t index);
VariableItemList* variable_item_list_alloc(void);
void variable_item_list_free(VariableItemList* l);
View* variable_item_list_get_view(VariableItemList* l);
void variable_item_list_reset(VariableItemList* l);
VariableItem* variable_item_list_add(
    VariableItemList* l, const char* label, uint8_t count, VariableItemChangeCallback cb, void* ctx);
void variable_item_list_set_enter_callback(VariableItemList* l, VariableItemListEnterCallback cb, void* ctx);
void variable_item_list_set_selected_item(VariableItemList* l, uint8_t index);
uint8_t variable_item_list_get_selected_item_index(VariableItemList* l);
void* variable_item_get_context(VariableItem* item);
uint8_t variable_item_get_current_value_index(VariableItem* item);
void variable_item_set_current_value_index(VariableItem* item, uint8_t index);
void variable_item_set_current_value_text(VariableItem* item, const char* text);

/* ---- notification */
typedef struct NotificationApp NotificationApp;
typedef struct {
    int type;
} NotificationMessage;
typedef const NotificationMessage* NotificationSequence[];
void notification_message_block(NotificationApp* app, const NotificationSequence* seq);
void notification_message(NotificationApp* app, const NotificationSequence* seq);
extern const NotificationMessage message_red_255, message_red_0, message_green_255, message_green_0,
    message_blue_255, message_blue_0, message_display_backlight_on, message_force_vibro_setting_on,
    message_force_vibro_setting_off, message_vibro_on, message_vibro_off, message_delay_25,
    message_delay_100, message_delay_250;

/* ---- bt / ble */
typedef struct Bt Bt;
typedef enum { BtStatusOff, BtStatusAdvertising, BtStatusConnected } BtStatus;
typedef void (*BtStatusChangedCallback)(BtStatus status, void* context);
typedef struct FuriHalBleProfileBase FuriHalBleProfileBase;
typedef struct FuriHalBleProfileTemplate FuriHalBleProfileTemplate;
void bt_disconnect(Bt* bt);
void bt_keys_storage_set_storage_path(Bt* bt, const char* path);
void bt_keys_storage_set_default_path(Bt* bt);
FuriHalBleProfileBase* bt_profile_start(Bt* bt, const FuriHalBleProfileTemplate* t, void* params);
bool bt_profile_restore_default(Bt* bt);
void bt_set_status_changed_callback(Bt* bt, BtStatusChangedCallback cb, void* ctx);

/* ---- loader / storage */
typedef struct Loader Loader;
typedef enum { LoaderDeferredLaunchFlagNone = 0 } LoaderDeferredLaunchFlag;
void loader_enqueue_launch(Loader* l, const char* name, const char* args, LoaderDeferredLaunchFlag f);
typedef struct Storage Storage;
typedef struct File File;

/* ---- generated icons */
extern const Icon I_hood_26x30;
