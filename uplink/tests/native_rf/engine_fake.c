#include "engine_fake.h"
#include "storage_fake.h"

#include <datetime/datetime.h>
#include <furi_hal_nfc.h>
#include <furi_hal_power.h>
#include <furi_hal_rtc.h>
#include <furi_hal_subghz.h>
#include <notification/notification_messages.h>
#include <subghz/devices/cc1101_configs.h>

#define WORLD_FAIL(message)                                                                  \
    do {                                                                                     \
        fprintf(stderr, "WORLD VIOLATION at %lu ms: %s\n", (unsigned long)now_ms, message); \
        exit(2);                                                                             \
    } while(0)

#define MAX_TX     32
#define MAX_FIELDS 16
#define MAX_STEPS  64
#define MAX_TUNES  512

typedef struct {
    uint32_t start_ms, end_ms, frequency;
    float rssi;
    uint32_t mark_us, space_us;
} WorldTx;

typedef struct {
    uint32_t start_ms, end_ms;
} WorldSpan;

typedef struct {
    uint32_t at_ms;
    WorldStep step;
    bool done;
} WorldEvent;

typedef enum { RadioAsleep, RadioIdle, RadioRx } RadioState;

static uint32_t now_ms;
static WorldTx txs[MAX_TX];
static uint32_t tx_count;
static WorldSpan noise;
static WorldSpan fields[MAX_FIELDS];
static uint32_t field_count;
static WorldEvent steps[MAX_STEPS];
static uint32_t step_count;
static uint32_t end_ms;
static bool in_worker, in_script;
static uint32_t queue_drops;

static RadioState radio;
static bool async_rx, preset, tuned;
static uint32_t frequency;
static FuriHalSubGhzCaptureCallback capture;
static void* capture_context;
static bool level;
static uint64_t last_edge_us;
static WorldTune tunes[MAX_TUNES];
static uint32_t tune_count;

static bool nfc_acquired, nfc_osc, nfc_detect;
static uint32_t nfc_busy;

static uint32_t feedback_counts[4];
static uint32_t red_blinks;

void world_reset(void) {
    now_ms = 0;
    tx_count = field_count = step_count = 0;
    noise.start_ms = noise.end_ms = 0;
    end_ms = 0;
    in_worker = in_script = false;
    queue_drops = 0;
    radio = RadioAsleep;
    async_rx = preset = tuned = false;
    frequency = 0;
    capture = NULL;
    capture_context = NULL;
    level = false;
    last_edge_us = 0;
    tune_count = 0;
    nfc_acquired = nfc_osc = nfc_detect = false;
    nfc_busy = 0;
    memset(feedback_counts, 0, sizeof(feedback_counts));
    red_blinks = 0;
}

void world_add_tx(
    uint32_t start_ms,
    uint32_t end,
    uint32_t tx_frequency,
    float rssi_dbm,
    uint32_t mark_us,
    uint32_t space_us) {
    if(tx_count == MAX_TX) WORLD_FAIL("too many transmissions");
    WorldTx tx = {start_ms, end, tx_frequency, rssi_dbm, mark_us, space_us};
    txs[tx_count++] = tx;
}

void world_set_noise(uint32_t start_ms, uint32_t end) {
    noise.start_ms = start_ms;
    noise.end_ms = end;
}

void world_add_nfc_field(uint32_t start_ms, uint32_t end) {
    if(field_count == MAX_FIELDS) WORLD_FAIL("too many fields");
    fields[field_count].start_ms = start_ms;
    fields[field_count].end_ms = end;
    field_count++;
}

void world_nfc_busy(uint32_t attempts) { nfc_busy = attempts; }

void world_at(uint32_t at_ms, WorldStep step) {
    if(step_count == MAX_STEPS) WORLD_FAIL("too many steps");
    if(step_count && steps[step_count - 1].at_ms > at_ms) WORLD_FAIL("steps out of order");
    steps[step_count].at_ms = at_ms;
    steps[step_count].step = step;
    steps[step_count].done = false;
    step_count++;
}

void world_end_at(uint32_t at_ms) { end_ms = at_ms; }
uint32_t world_now(void) { return now_ms; }

uint32_t world_tunes(const WorldTune** list) {
    *list = tunes;
    return tune_count;
}

uint32_t world_feedback(uint32_t vibro_pulses) {
    return vibro_pulses < 4 ? feedback_counts[vibro_pulses] : 0;
}

uint32_t world_red_blinks(void) { return red_blinks; }
bool world_radio_asleep(void) { return radio == RadioAsleep && !async_rx; }
bool world_nfc_released(void) { return !nfc_acquired && !nfc_detect; }
uint32_t world_queue_drops(void) { return queue_drops; }

/* ------------------------------------------------------------------ RF model */

static const WorldTx* active_tx(uint64_t t_us) {
    for(uint32_t i = 0; i < tx_count; i++) {
        const WorldTx* tx = &txs[i];
        if(tx->frequency == frequency && t_us >= tx->start_ms * 1000ULL &&
           t_us < tx->end_ms * 1000ULL) {
            return tx;
        }
    }
    return NULL;
}

static bool noise_level(uint64_t t_us) {
    if(t_us < noise.start_ms * 1000ULL || t_us >= noise.end_ms * 1000ULL) return false;
    uint32_t h = (uint32_t)(t_us / 150U) * 2654435761U;
    h ^= h >> 15;
    h *= 2246822519U;
    h ^= h >> 13;
    return (h & 3U) == 0;
}

static bool world_level(uint64_t t_us) {
    const WorldTx* tx = active_tx(t_us);
    if(tx) return (t_us - tx->start_ms * 1000ULL) % (tx->mark_us + tx->space_us) < tx->mark_us;
    return noise_level(t_us);
}

static bool span_has_activity(uint64_t from_us, uint64_t to_us) {
    if(level) return true;
    if(from_us < noise.end_ms * 1000ULL && to_us >= noise.start_ms * 1000ULL) return true;
    for(uint32_t i = 0; i < tx_count; i++) {
        if(txs[i].frequency == frequency && from_us < txs[i].end_ms * 1000ULL &&
           to_us >= txs[i].start_ms * 1000ULL) {
            return true;
        }
    }
    return false;
}

/* The capture ISR reports the level that just ended and its duration. */
static void generate_edges(uint64_t from_us, uint64_t to_us) {
    if(!async_rx || !capture || !span_has_activity(from_us, to_us)) return;
    uint32_t saved = now_ms;
    for(uint64_t t = from_us + 1; t <= to_us; t++) {
        bool next = world_level(t);
        if(next == level) continue;
        now_ms = (uint32_t)(t / 1000U);
        capture(level, (uint32_t)(t - last_edge_us), capture_context);
        last_edge_us = t;
        level = next;
    }
    now_ms = saved;
}

static bool world_finished(void) {
    for(uint32_t i = 0; i < step_count; i++) {
        if(!steps[i].done) return false;
    }
    return now_ms >= end_ms;
}

/* Advance time while the engine thread waits; run due script steps. */
static void world_idle(uint32_t timeout) {
    uint32_t target = now_ms + timeout;
    while(now_ms < target) {
        WorldEvent* due = NULL;
        for(uint32_t i = 0; i < step_count; i++) {
            if(!steps[i].done) {
                due = &steps[i];
                break;
            }
        }
        uint32_t next = target;
        if(due && due->at_ms < next) next = due->at_ms > now_ms ? due->at_ms : now_ms;
        generate_edges(now_ms * 1000ULL, next * 1000ULL);
        now_ms = next;
        if(due && due->at_ms <= now_ms) {
            due->done = true;
            in_script = true;
            due->step();
            in_script = false;
            return; /* let the engine react to what the step changed */
        }
    }
}

/* ------------------------------------------------------------------ furi core */

uint32_t furi_get_tick(void) { return now_ms; }
uint32_t furi_ms_to_ticks(uint32_t milliseconds) { return milliseconds; }

struct FuriMutex {
    bool locked;
};

FuriMutex* furi_mutex_alloc(FuriMutexType type) {
    (void)type;
    return calloc(1, sizeof(FuriMutex));
}

void furi_mutex_free(FuriMutex* mutex) {
    if(mutex->locked) WORLD_FAIL("mutex freed while locked");
    free(mutex);
}

FuriStatus furi_mutex_acquire(FuriMutex* mutex, uint32_t timeout) {
    (void)timeout;
    if(mutex->locked) WORLD_FAIL("mutex acquired twice: deadlock on the device");
    mutex->locked = true;
    return FuriStatusOk;
}

FuriStatus furi_mutex_release(FuriMutex* mutex) {
    if(!mutex->locked) WORLD_FAIL("mutex released while not held");
    mutex->locked = false;
    return FuriStatusOk;
}

struct FuriMessageQueue {
    uint32_t capacity, size, head, used;
    unsigned char* data;
    unsigned char* deferred;
    bool has_deferred;
};

FuriMessageQueue* furi_message_queue_alloc(uint32_t msg_count, uint32_t msg_size) {
    FuriMessageQueue* queue = calloc(1, sizeof(FuriMessageQueue));
    queue->capacity = msg_count;
    queue->size = msg_size;
    queue->data = calloc(msg_count, msg_size);
    queue->deferred = calloc(1, msg_size);
    return queue;
}

void furi_message_queue_free(FuriMessageQueue* queue) {
    free(queue->data);
    free(queue->deferred);
    free(queue);
}

/* A blocking put (the exit request) is delivered once the scripted world ends. */
FuriStatus furi_message_queue_put(FuriMessageQueue* queue, const void* msg, uint32_t timeout) {
    if(timeout == FuriWaitForever) {
        if(queue->has_deferred) WORLD_FAIL("second blocking put");
        memcpy(queue->deferred, msg, queue->size);
        queue->has_deferred = true;
        return FuriStatusOk;
    }
    if(queue->used == queue->capacity) {
        queue_drops++;
        return FuriStatusErrorResource;
    }
    memcpy(
        queue->data + ((queue->head + queue->used) % queue->capacity) * queue->size,
        msg,
        queue->size);
    queue->used++;
    return FuriStatusOk;
}

FuriStatus furi_message_queue_get(FuriMessageQueue* queue, void* msg, uint32_t timeout) {
    if(!in_worker) WORLD_FAIL("queue read outside the engine thread");
    if(!queue->used && timeout && !world_finished()) world_idle(timeout);
    if(queue->used) {
        memcpy(msg, queue->data + queue->head * queue->size, queue->size);
        queue->head = (queue->head + 1) % queue->capacity;
        queue->used--;
        return FuriStatusOk;
    }
    if(world_finished()) {
        if(!queue->has_deferred) WORLD_FAIL("engine still waiting after the world ended");
        memcpy(msg, queue->deferred, queue->size);
        queue->has_deferred = false;
        return FuriStatusOk;
    }
    return FuriStatusErrorTimeout;
}

struct FuriThread {
    FuriThreadCallback callback;
    void* context;
    bool started, ran;
};

FuriThread* furi_thread_alloc_ex(
    const char* name,
    uint32_t stack_size,
    FuriThreadCallback callback,
    void* context) {
    (void)name;
    if(stack_size > 4096) WORLD_FAIL("engine stack larger than 4 KiB");
    FuriThread* thread = calloc(1, sizeof(FuriThread));
    thread->callback = callback;
    thread->context = context;
    return thread;
}

void furi_thread_start(FuriThread* thread) { thread->started = true; }

/* The engine thread runs here, after the app requested exit; the scripted
 * world plays out while it runs. */
bool furi_thread_join(FuriThread* thread) {
    if(thread->started && !thread->ran) {
        thread->ran = true;
        in_worker = true;
        thread->callback(thread->context);
        in_worker = false;
    }
    return true;
}

void furi_thread_free(FuriThread* thread) { free(thread); }

static int notification_record;

void* furi_record_open(const char* name) {
    if(strcmp(name, "storage") == 0) return &fake_storage;
    if(strcmp(name, RECORD_NOTIFICATION) != 0) WORLD_FAIL("unknown record");
    return &notification_record;
}

void furi_record_close(const char* name) { (void)name; }

/* ------------------------------------------------------------------ notifications */

const NotificationMessage message_force_vibro_setting_on = {1};
const NotificationMessage message_force_vibro_setting_off = {2};
const NotificationMessage message_vibro_on = {3};
const NotificationMessage message_vibro_off = {4};
const NotificationMessage message_red_255 = {5};
const NotificationMessage message_red_0 = {6};
const NotificationMessage message_green_255 = {7};
const NotificationMessage message_green_0 = {8};
const NotificationMessage message_blue_255 = {9};
const NotificationMessage message_blue_0 = {10};
const NotificationMessage message_delay_25 = {11};
const NotificationMessage message_delay_50 = {12};
const NotificationMessage message_delay_100 = {13};

void notification_message(NotificationApp* app, const NotificationSequence* sequence) {
    (void)app;
    if(!in_worker || in_script) WORLD_FAIL("notification outside the engine thread");
    uint32_t vibro = 0;
    bool red = false;
    for(const NotificationMessage* const* message = *sequence; *message; message++) {
        if(*message == &message_vibro_on) vibro++;
        if(*message == &message_red_255) red = true;
    }
    if(red) red_blinks++;
    if(vibro < 4) feedback_counts[vibro]++;
}

/* ------------------------------------------------------------------ HAL */

static void hal_enter(const char* what) {
    if(!in_worker) {
        fprintf(stderr, "%s: ", what);
        WORLD_FAIL("HAL call outside the engine thread");
    }
    if(in_script) {
        fprintf(stderr, "%s: ", what);
        WORLD_FAIL("HAL call from a caller thread");
    }
}

static void cc1101_bus(const char* what) {
    hal_enter(what);
    if(nfc_acquired) {
        fprintf(stderr, "%s: ", what);
        WORLD_FAIL("CC1101 access while NFC holds SPI bus R");
    }
}

const uint8_t subghz_device_cc1101_preset_ook_650khz_async_regs[] = {0x02, 0x0D, 0x00, 0x00};

void furi_hal_subghz_reset(void) {
    cc1101_bus("reset");
    if(async_rx) WORLD_FAIL("reset during async RX");
    radio = RadioIdle;
    preset = tuned = false;
}

void furi_hal_subghz_idle(void) {
    cc1101_bus("idle");
    if(async_rx) WORLD_FAIL("idle() during async RX");
    radio = RadioIdle;
}

void furi_hal_subghz_sleep(void) {
    cc1101_bus("sleep");
    if(async_rx) WORLD_FAIL("furi_check: sleep during async RX");
    radio = RadioAsleep;
    preset = tuned = false;
}

void furi_hal_subghz_load_custom_preset(const uint8_t* preset_data) {
    cc1101_bus("load_custom_preset");
    if(async_rx) WORLD_FAIL("preset load during async RX");
    if(preset_data != subghz_device_cc1101_preset_ook_650khz_async_regs) {
        WORLD_FAIL("unexpected preset");
    }
    radio = RadioIdle;
    preset = true;
    tuned = false;
}

bool furi_hal_subghz_is_frequency_valid(uint32_t value) {
    return (value >= 281000000U && value <= 361000000U) ||
           (value >= 378000000U && value <= 481000000U) ||
           (value >= 749000000U && value <= 962000000U);
}

uint32_t furi_hal_subghz_set_frequency_and_path(uint32_t value) {
    cc1101_bus("set_frequency_and_path");
    if(async_rx) WORLD_FAIL("furi_check: calibration during async RX");
    if(radio != RadioIdle) WORLD_FAIL("set_frequency outside IDLE");
    if(!preset) WORLD_FAIL("tuned without a preset");
    if(!furi_hal_subghz_is_frequency_valid(value)) WORLD_FAIL("furi_crash: invalid frequency");
    frequency = value;
    tuned = true;
    if(tune_count < MAX_TUNES) {
        tunes[tune_count].at_ms = now_ms;
        tunes[tune_count].frequency = value;
        tune_count++;
    }
    return value;
}

void furi_hal_subghz_flush_rx(void) {
    cc1101_bus("flush_rx");
    if(radio != RadioIdle || async_rx) WORLD_FAIL("flush_rx outside IDLE");
}

float furi_hal_subghz_get_rssi(void) {
    cc1101_bus("get_rssi");
    if(radio != RadioRx) WORLD_FAIL("RSSI read outside RX");
    uint64_t t_us = now_ms * 1000ULL;
    const WorldTx* tx = active_tx(t_us);
    if(tx && world_level(t_us)) return tx->rssi;
    return -100.0f + (float)(now_ms % 5U);
}

void furi_hal_subghz_start_async_rx(FuriHalSubGhzCaptureCallback callback, void* context) {
    cc1101_bus("start_async_rx");
    if(async_rx) WORLD_FAIL("furi_check: start_async_rx while not idle");
    if(!preset || !tuned) WORLD_FAIL("RX without preset and frequency");
    if(radio != RadioIdle) WORLD_FAIL("start_async_rx outside IDLE");
    async_rx = true;
    radio = RadioRx;
    capture = callback;
    capture_context = context;
    level = world_level(now_ms * 1000ULL);
    last_edge_us = now_ms * 1000ULL;
}

void furi_hal_subghz_stop_async_rx(void) {
    cc1101_bus("stop_async_rx");
    if(!async_rx) WORLD_FAIL("furi_check: stop_async_rx without async RX");
    async_rx = false;
    radio = RadioIdle;
    capture = NULL;
}

FuriHalNfcError furi_hal_nfc_acquire(void) {
    hal_enter("nfc_acquire");
    if(nfc_acquired) WORLD_FAIL("NFC acquired twice");
    if(radio != RadioAsleep || async_rx) WORLD_FAIL("NFC acquired while the CC1101 is awake");
    if(nfc_busy) {
        nfc_busy--;
        return FuriHalNfcErrorBusy;
    }
    nfc_acquired = true;
    return FuriHalNfcErrorNone;
}

FuriHalNfcError furi_hal_nfc_release(void) {
    hal_enter("nfc_release");
    if(!nfc_acquired) WORLD_FAIL("NFC released without acquire");
    if(nfc_detect) WORLD_FAIL("NFC released with the field detector on");
    nfc_acquired = nfc_osc = false;
    return FuriHalNfcErrorNone;
}

FuriHalNfcError furi_hal_nfc_low_power_mode_stop(void) {
    hal_enter("nfc_low_power_mode_stop");
    if(!nfc_acquired) WORLD_FAIL("NFC not acquired");
    nfc_osc = true;
    return FuriHalNfcErrorNone;
}

FuriHalNfcError furi_hal_nfc_low_power_mode_start(void) {
    hal_enter("nfc_low_power_mode_start");
    if(!nfc_acquired) WORLD_FAIL("NFC not acquired");
    nfc_osc = nfc_detect = false;
    return FuriHalNfcErrorNone;
}

FuriHalNfcError furi_hal_nfc_field_detect_start(void) {
    hal_enter("nfc_field_detect_start");
    if(!nfc_acquired || !nfc_osc) WORLD_FAIL("field detector without oscillator");
    nfc_detect = true;
    return FuriHalNfcErrorNone;
}

FuriHalNfcError furi_hal_nfc_field_detect_stop(void) {
    hal_enter("nfc_field_detect_stop");
    if(!nfc_acquired) WORLD_FAIL("NFC not acquired");
    nfc_detect = false;
    return FuriHalNfcErrorNone;
}

bool furi_hal_nfc_field_is_present(void) {
    hal_enter("nfc_field_is_present");
    if(!nfc_acquired || !nfc_detect) WORLD_FAIL("field read without detector");
    for(uint32_t i = 0; i < field_count; i++) {
        if(now_ms >= fields[i].start_ms && now_ms < fields[i].end_ms) return true;
    }
    return false;
}

uint8_t furi_hal_power_get_pct(void) { return 80; }

/* RTC calendar ("local time") = 2026-10-06 08:00:00 + elapsed seconds. */
static uint32_t days_from_civil(uint32_t year, uint32_t month, uint32_t day) {
    year -= month <= 2 ? 1U : 0U;
    uint32_t era = year / 400U;
    uint32_t yoe = year - era * 400U;
    uint32_t doy = (153U * (month > 2 ? month - 3U : month + 9U) + 2U) / 5U + day - 1U;
    uint32_t doe = yoe * 365U + yoe / 4U - yoe / 100U + doy;
    return era * 146097U + doe - 719468U;
}

uint32_t datetime_datetime_to_timestamp(DateTime* datetime) {
    return days_from_civil(datetime->year, datetime->month, datetime->day) * 86400U +
           datetime->hour * 3600U + datetime->minute * 60U + datetime->second;
}

void furi_hal_rtc_get_datetime(DateTime* datetime) {
    uint32_t seconds = 8U * 3600U + now_ms / 1000U;
    memset(datetime, 0, sizeof(*datetime));
    datetime->year = 2026;
    datetime->month = 10;
    datetime->day = 6;
    datetime->hour = (uint8_t)(seconds / 3600U);
    datetime->minute = (uint8_t)(seconds / 60U % 60U);
    datetime->second = (uint8_t)(seconds % 60U);
}
