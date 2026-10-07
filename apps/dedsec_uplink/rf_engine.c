/* Passive RF engine: Sub-GHz Scout/Capture/Follow and NFC field detection.
 *
 * Threads:
 *   - Callers (UI/BLE threads of uplink.c) only touch the mutex-protected
 *     control/snapshot fields and post to the queue; they never block on I/O.
 *   - The TIM2 capture ISR only appends to the RfCapture ring.
 *   - The engine thread does everything else: HAL calls, journal I/O,
 *     PC requests, feedback, status snapshots and callbacks.
 *
 * Passive only: no TX, no NFC poller/listener/field-on.  Sub-GHz runs the
 * CC1101 in async OOK RX; NFC only reads the ST25R3916 external-field flag.
 *
 * Radio sequence (all on the engine thread):
 *   start: reset -> idle -> load OOK 650 kHz async preset
 *   tune:  stop async RX (if on) -> idle -> set_frequency_and_path -> flush_rx
 *          -> start async RX (the HAL switches the chip to RX)
 *   stop:  stop async RX (if on) -> sleep
 * furi_hal_subghz_set_frequency() calibrates and furi_check()s that the chip
 * reaches IDLE, so it must never run while RX is active.  The NFC HAL keeps
 * SPI bus R (shared with the CC1101) while acquired, so the CC1101 is put to
 * sleep before NFC is acquired and only touched again after NFC release. */

#include "rf_engine.h"
#include "rf_capture.h"
#include "rf_proto.h"
#include "rf_record.h"
#include "rf_store.h"

#include <furi.h>
#include <furi_hal_nfc.h>
#include <furi_hal_power.h>
#include <furi_hal_random.h>
#include <furi_hal_rtc.h>
#include <furi_hal_speaker.h>
#include <furi_hal_subghz.h>
#include <datetime/datetime.h>
#include <notification/notification_messages.h>
#include <storage/storage.h>
#include <subghz/devices/cc1101_configs.h>
#include <string.h>

#define TAG "RfEngine"

#define RF_ENGINE_STACK      (4U * 1024U) /* ~1.2 KB measured + printf/storage frames */
#define RF_QUEUE_DEPTH       8U
#define RF_REQUEST_MAX       RF_PROTO_REQUEST_MAX /* an RW line with 180 record bytes */
#define RF_RECORD_SIZE       4096U /* one record incl. pulse timings */
#define RF_TIMINGS_MAX       512U /* timings kept per event (4 bytes each) */
#define RF_PRETRIGGER_MAX    64U
#define RF_PRETRIGGER_US     10000U /* context kept from before the trigger */
#define RF_RX_TICK_MS        5U /* RSSI sampling + ring draining while RX */
#define RF_NFC_TICK_MS       10U
#define RF_IDLE_TICK_MS      250U
/* No RSSI sample above threshold for this long ends a burst even while the
 * demodulator keeps producing noise edges.  OOK spaces read as weak RSSI, so
 * this must span many 5 ms samples of a low-duty burst. */
#define RF_QUIET_MIN_MS      150U
/* The trigger never sits closer than this to the noise floor of the frequency: next to a PC
 * the 315 MHz floor can be -77 dBm, and a -75 dBm trigger then fires on noise. */
#define RF_FLOOR_MARGIN_DB   8.0f
#define RF_FLOOR_SAMPLES     8U /* RSSI samples on a frequency before it may trigger */
/* What makes a burst a signal rather than a noise spike: OOK edges over a few RSSI samples,
 * or a carrier that stays up. Anything less is not recorded. */
#define RF_MIN_EDGES         16U
#define RF_MIN_STRONG_MS     10U
#define RF_MIN_CARRIER_MS    50U
#define RF_NFC_MERGE_MS      1000U /* field gaps shorter than this are one event */
#define RF_NFC_RETRY_MS      1000U
#define RF_FEEDBACK_MIN_MS   1000U
#define RF_ERROR_BLINK_MS    5000U
#define RF_CHANGED_MIN_MS    100U
#define RF_FREE_REFRESH_MS   10000U
#define RF_STORE_IDLE_MS     3000U
#define RF_FAMILY_SLOTS      64U
#define RF_FOLLOW_MATCH      0.70f
/* Geiger counter (Follow): clicks per second from the live RSSI above the noise floor. */
#define RF_GEIGER_BACKGROUND 0.6f /* clicks/s on a quiet channel */
#define RF_GEIGER_MAX_RATE   40.0f /* clicks/s at RF_GEIGER_SPAN_DB above the floor */
#define RF_GEIGER_START_DB   3.0f /* excess over the floor where the rate starts rising */
#define RF_GEIGER_SPAN_DB    45.0f
#define RF_GEIGER_CLICK_HZ   2600.0f
#define RF_GEIGER_VOLUME     0.8f
#define RF_PEAK_HOLD_MS      1000U
#define RF_PEAK_DECAY_MS     100U
#define RF_NFC_FINGERPRINT   0x4e464300UL
#define RF_NFC_FREQUENCY_HZ  13560000UL
#define RF_FREQ_315          0U
#define RF_FREQ_433          1U
#define RF_FREQ_868          2U
#define RF_FREQ_NONE         0xFFU

static const uint32_t rf_frequencies[] = {315000000UL, 433920000UL, 868350000UL};
#define RF_FREQ_COUNT (sizeof(rf_frequencies) / sizeof(rf_frequencies[0]))

typedef enum {
    RfMsgWake = 1,
    RfMsgRequest,
    RfMsgExit,
} RfMsgType;

typedef struct {
    uint8_t type;
    bool too_long;
    char line[RF_REQUEST_MAX];
} RfMsg;

struct RfEngine {
    RfReplyCallback reply;
    RfChangedCallback changed;
    void* context;
    FuriThread* thread;
    FuriMessageQueue* queue;
    FuriMutex* mutex;
    Storage* storage;
    NotificationApp* notifications;
    RfStore* store;
    RfCapture* ring;
    uint32_t* timings;
    char* record;
    char* line;

    /* control requested by callers (mutex) */
    RfConfig want_config;
    RfMode want_mode;
    bool want_running;
    bool control_dirty;
    bool wake_queued;
    bool exiting;

    /* published snapshot (mutex) */
    RfStatus status;
    uint32_t stored;

    /* engine thread only */
    RfConfig config;
    RfMode mode;
    bool running;
    bool radio_ready;
    bool rx_on;
    uint8_t freq_index;
    uint32_t hop_tick;
    uint32_t rssi_tick;
    float last_rssi;
    float floor[RF_FREQ_COUNT]; /* noise floor per frequency: the lower envelope of RSSI */
    uint8_t floor_samples[RF_FREQ_COUNT];
    float trigger; /* level of the burst being captured */
    bool signal; /* the burst being captured is a signal (feedback given) */

    bool capturing;
    uint32_t capture_tick;
    uint32_t strong_tick;
    uint32_t capture_rtc;
    uint32_t capture_mono;
    uint32_t timing_count; /* stored in timings[] */
    uint32_t pulse_count; /* observed, may exceed RF_TIMINGS_MAX */
    uint64_t capture_us;
    uint32_t last_timing_us;
    float rssi_min;
    float rssi_max;
    float rssi_sum;
    uint32_t rssi_count;
    uint32_t pre[RF_PRETRIGGER_MAX];
    uint8_t pre_count;
    uint8_t pre_head;

    bool nfc_on;
    bool nfc_field; /* an event is open (field seen within RF_NFC_MERGE_MS) */
    bool nfc_present; /* last raw detector reading */
    uint32_t nfc_start_tick;
    uint32_t nfc_seen_tick;
    uint32_t nfc_rtc;
    uint32_t nfc_mono;
    uint32_t nfc_field_count;
    uint32_t nfc_retry_tick;

    bool follow_valid;
    uint32_t follow_frequency;
    uint32_t follow_fingerprint;
    RfShape follow_shape;
    uint8_t follow_protocol; /* RfProtoId of the profile (RfProtoOok: shape only) */
    uint64_t follow_identity; /* rf_decode_identity of the profile, 0 = none */
    char follow_label[20];

    /* Geiger counter and live readings */
    float peak_rssi;
    uint32_t peak_tick;
    uint32_t peak_decay_tick;
    float geiger_rate;
    bool speaker_held;
    bool click_on;

    uint32_t families[RF_FAMILY_SLOTS];
    uint8_t family_used;
    uint8_t family_next;

    char device_id[17];
    char session_id[16];
    uint32_t sequence;
    uint32_t session_tick;

    uint32_t events;
    uint32_t family_count;
    uint32_t unseen_add;
    uint32_t errors;
    bool storage_full;
    uint32_t last_unix;
    uint32_t last_frequency;
    int16_t last_rssi_dbm;
    uint32_t last_duration;
    uint8_t last_similarity;
    char last_label[20];
    char last_info[RF_DECODE_INFO_MAX];
    bool last_rolling;

    uint32_t feedback_tick;
    uint32_t error_tick;
    uint32_t changed_tick;
    bool changed_pending;
    uint32_t free_tick;
    uint32_t request_tick;
    bool store_handle;
    uint32_t dropped_seen;
};

/* ------------------------------------------------------------------ feedback */

static const NotificationSequence rf_sequence_event = {
    &message_force_vibro_setting_on,
    &message_vibro_on,
    &message_green_255,
    &message_blue_255,
    &message_delay_50,
    &message_vibro_off,
    &message_green_0,
    &message_blue_0,
    &message_force_vibro_setting_off,
    NULL,
};

static const NotificationSequence rf_sequence_follow = {
    &message_force_vibro_setting_on,
    &message_vibro_on,
    &message_green_255,
    &message_blue_255,
    &message_delay_50,
    &message_vibro_off,
    &message_green_0,
    &message_blue_0,
    &message_delay_50,
    &message_vibro_on,
    &message_green_255,
    &message_blue_255,
    &message_delay_50,
    &message_vibro_off,
    &message_green_0,
    &message_blue_0,
    &message_force_vibro_setting_off,
    NULL,
};

static const NotificationSequence rf_sequence_error = {
    &message_red_255,
    &message_delay_50,
    &message_red_0,
    NULL,
};

static void rf_feedback(RfEngine* engine, bool follow_match) {
    if(!engine->config.feedback) return;
    uint32_t now = furi_get_tick();
    if(now - engine->feedback_tick < RF_FEEDBACK_MIN_MS) return;
    engine->feedback_tick = now;
    if(follow_match) {
        notification_message(engine->notifications, &rf_sequence_follow);
    } else {
        notification_message(engine->notifications, &rf_sequence_event);
    }
}

static void rf_error_blink(RfEngine* engine) {
    uint32_t now = furi_get_tick();
    if(now - engine->error_tick < RF_ERROR_BLINK_MS) return;
    engine->error_tick = now;
    notification_message(engine->notifications, &rf_sequence_error);
}

/* ------------------------------------------------------------------ helpers */

static void rf_config_defaults(RfConfig* config) {
    memset(config, 0, sizeof(*config));
    config->band = RfBandAll;
    config->rssi_threshold_dbm = -75;
    config->dwell_ms = 250;
    config->capture_ms = 1000;
    config->silence_us = 8000;
    config->feedback = true;
    config->geiger = true;
    config->keep_uploaded = false;
    config->tz_offset_minutes = 0;
}

static void rf_config_sanitize(RfConfig* config) {
    if(config->band >= RfBandCount) config->band = RfBandAll;
    if(config->rssi_threshold_dbm < -110) config->rssi_threshold_dbm = -110;
    if(config->rssi_threshold_dbm > -30) config->rssi_threshold_dbm = -30;
    if(config->dwell_ms < 50) config->dwell_ms = 50;
    if(config->dwell_ms > 10000) config->dwell_ms = 10000;
    if(config->capture_ms < 200) config->capture_ms = 200;
    if(config->capture_ms > 5000) config->capture_ms = 5000;
    if(config->silence_us < 1000) config->silence_us = 1000;
    if(config->silence_us > 30000) config->silence_us = 30000;
    if(config->tz_offset_minutes < -14 * 60) config->tz_offset_minutes = -14 * 60;
    if(config->tz_offset_minutes > 14 * 60) config->tz_offset_minutes = 14 * 60;
}

static bool rf_exiting(RfEngine* engine) {
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    bool exiting = engine->exiting;
    furi_mutex_release(engine->mutex);
    return exiting;
}

static uint32_t rf_rtc_now(void) {
    DateTime now;
    furi_hal_rtc_get_datetime(&now);
    return datetime_datetime_to_timestamp(&now);
}

static int16_t rf_round_dbm(float value) {
    if(value < -1000.0f) value = -1000.0f;
    if(value > 1000.0f) value = 1000.0f;
    return (int16_t)(value < 0.0f ? value - 0.5f : value + 0.5f);
}

static void rf_make_session_id(RfEngine* engine) {
    static const char hex[] = "0123456789abcdef";
    uint8_t bytes[7];
    furi_hal_random_fill_buf(bytes, sizeof(bytes));
    engine->session_id[0] = 's';
    for(size_t i = 0; i < sizeof(bytes); i++) {
        engine->session_id[1 + i * 2] = hex[bytes[i] >> 4];
        engine->session_id[2 + i * 2] = hex[bytes[i] & 15U];
    }
    engine->session_id[15] = '\0';
}

static const char* rf_mode_name(RfMode mode) {
    switch(mode) {
    case RfModeCapture:
        return "CAPTURE";
    case RfModeFollow:
        return "FOLLOW";
    case RfModeNfc:
        return "NFC";
    default:
        return "SCOUT";
    }
}

static bool rf_family_add(RfEngine* engine, uint32_t fingerprint) {
    for(uint8_t i = 0; i < engine->family_used; i++) {
        if(engine->families[i] == fingerprint) return false;
    }
    engine->families[engine->family_next] = fingerprint;
    engine->family_next = (uint8_t)((engine->family_next + 1U) % RF_FAMILY_SLOTS);
    if(engine->family_used < RF_FAMILY_SLOTS) engine->family_used++;
    return true;
}

/* Count the outcome of a journal write; failures are surfaced, never silent. */
static bool rf_journal_result(RfEngine* engine, RfStoreResult result) {
    if(result == RfStoreOk) {
        engine->storage_full = false;
        return true;
    }
    engine->errors++;
    if(result == RfStoreErrLowSpace) engine->storage_full = true;
    FURI_LOG_E(TAG, "journal write failed (%d)", (int)result);
    rf_error_blink(engine);
    return false;
}

static void rf_note_event(
    RfEngine* engine,
    uint32_t fingerprint,
    uint32_t rtc_unix,
    uint32_t frequency,
    int16_t rssi_dbm,
    uint32_t duration_us) {
    engine->events++;
    engine->unseen_add++;
    if(rf_family_add(engine, fingerprint)) engine->family_count++;
    engine->last_unix = rf_record_utc_unix(rtc_unix, engine->config.tz_offset_minutes);
    engine->last_frequency = frequency;
    engine->last_rssi_dbm = rssi_dbm;
    engine->last_duration = duration_us;
}

/* ------------------------------------------------------------------ frequencies */

static bool rf_band_allows(uint8_t band, uint8_t index) {
    switch(band) {
    case RfBand433:
        return index == RF_FREQ_433;
    case RfBand315:
        return index == RF_FREQ_315;
    case RfBand868:
        return index == RF_FREQ_868;
    default:
        return true;
    }
}

static bool rf_frequency_usable(uint8_t index) {
    return index < RF_FREQ_COUNT && furi_hal_subghz_is_frequency_valid(rf_frequencies[index]);
}

static uint8_t rf_index_of(uint32_t frequency) {
    for(uint8_t i = 0; i < RF_FREQ_COUNT; i++) {
        if(rf_frequencies[i] == frequency) return i;
    }
    return RF_FREQ_NONE;
}

/* Frequency for the current mode.  `keep` prefers the current tuning (config
 * updates); a mode change picks afresh: Follow -> profile frequency,
 * Capture/Follow -> frequency of the last event, else 433.92 MHz first. */
static bool rf_pick_index(RfEngine* engine, bool keep, uint8_t* index) {
    uint8_t band = engine->config.band;
    if(engine->mode == RfModeFollow && engine->follow_valid) {
        uint8_t follow = rf_index_of(engine->follow_frequency);
        if(rf_frequency_usable(follow)) {
            *index = follow;
            return true;
        }
    }
    if(keep && engine->rx_on && rf_band_allows(band, engine->freq_index)) {
        *index = engine->freq_index;
        return true;
    }
    if(engine->mode != RfModeScout && engine->last_frequency) {
        uint8_t last = rf_index_of(engine->last_frequency);
        if(rf_frequency_usable(last) && rf_band_allows(band, last)) {
            *index = last;
            return true;
        }
    }
    static const uint8_t order[] = {RF_FREQ_433, RF_FREQ_315, RF_FREQ_868};
    for(size_t i = 0; i < sizeof(order); i++) {
        if(rf_band_allows(band, order[i]) && rf_frequency_usable(order[i])) {
            *index = order[i];
            return true;
        }
    }
    return false;
}

static bool rf_next_index(RfEngine* engine, uint8_t* index) {
    for(uint8_t step = 1; step <= RF_FREQ_COUNT; step++) {
        uint8_t candidate = (uint8_t)((engine->freq_index + step) % RF_FREQ_COUNT);
        if(rf_band_allows(engine->config.band, candidate) && rf_frequency_usable(candidate)) {
            *index = candidate;
            return true;
        }
    }
    return false;
}

/* ------------------------------------------------------------------ Sub-GHz capture */

static void rf_pre_push(RfEngine* engine, uint32_t timing) {
    engine->pre[engine->pre_head] = timing;
    engine->pre_head = (uint8_t)((engine->pre_head + 1U) % RF_PRETRIGGER_MAX);
    if(engine->pre_count < RF_PRETRIGGER_MAX) engine->pre_count++;
}

static void rf_pre_clear(RfEngine* engine) {
    engine->pre_count = 0;
    engine->pre_head = 0;
}

/* The trigger: the configured level, but at least RF_FLOOR_MARGIN_DB above the noise. */
static float rf_trigger(const RfEngine* engine) {
    float trigger = (float)engine->config.rssi_threshold_dbm;
    float floor = engine->floor[engine->freq_index] + RF_FLOOR_MARGIN_DB;
    return floor > trigger ? floor : trigger;
}

/* The lower envelope: quiet stretches pull it down quickly, bursts lift it slowly. */
static void rf_floor_track(RfEngine* engine, float rssi) {
    uint8_t i = engine->freq_index;
    if(!engine->floor_samples[i]) {
        engine->floor[i] = rssi;
    } else {
        float k = rssi < engine->floor[i] ? 0.125f : 0.015625f;
        engine->floor[i] += (rssi - engine->floor[i]) * k;
    }
    if(engine->floor_samples[i] < 255U) engine->floor_samples[i]++;
}

/* A signal, not a noise spike: OOK edges over a few RSSI samples, or a carrier that stays up. */
static bool rf_capture_is_signal(const RfEngine* engine) {
    uint32_t strong_ms = engine->strong_tick - engine->capture_tick;
    return (engine->pulse_count >= RF_MIN_EDGES && strong_ms >= RF_MIN_STRONG_MS) ||
           strong_ms >= RF_MIN_CARRIER_MS;
}

static void rf_capture_reset(RfEngine* engine) {
    engine->capturing = false;
    engine->signal = false;
    engine->timing_count = 0;
    engine->pulse_count = 0;
    engine->capture_us = 0;
    engine->last_timing_us = 0;
    engine->rssi_min = engine->rssi_max = engine->rssi_sum = 0.0f;
    engine->rssi_count = 0;
}

static void rf_capture_open(RfEngine* engine, float rssi, uint32_t now) {
    /* Seed with the timings of the last RF_PRETRIGGER_US before the trigger:
     * the RSSI sample lags the first edges by up to one tick.  Older entries
     * (and the long silence before the burst) are not part of it. */
    uint32_t take = 0, span = 0;
    while(take < engine->pre_count) {
        uint32_t timing =
            engine->pre[(engine->pre_head + RF_PRETRIGGER_MAX - 1U - take) % RF_PRETRIGGER_MAX];
        uint32_t duration = rf_timing_duration(timing);
        if(span + duration > RF_PRETRIGGER_US) break;
        span += duration;
        take++;
    }
    for(uint32_t i = 0; i < take; i++) {
        engine->timings[i] =
            engine->pre[(engine->pre_head + RF_PRETRIGGER_MAX - take + i) % RF_PRETRIGGER_MAX];
    }
    /* The pre-trigger belongs to this event only. */
    rf_pre_clear(engine);
    engine->capturing = true;
    engine->timing_count = take;
    engine->pulse_count = take;
    engine->capture_us = 0;
    engine->last_timing_us = take ? rf_timing_duration(engine->timings[take - 1]) : 0;
    engine->capture_tick = now;
    engine->strong_tick = now;
    engine->rssi_min = engine->rssi_max = engine->rssi_sum = rssi;
    engine->rssi_count = 1;
    engine->capture_rtc = rf_rtc_now();
    engine->capture_mono = now - engine->session_tick;
    engine->signal = false; /* feedback once it proves to be a signal (rf_rx_service) */
}

static void rf_capture_close(RfEngine* engine) {
    if(!engine->capturing) return;
    if(!rf_capture_is_signal(engine)) {
        /* a noise spike above the trigger: nothing worth recording */
        rf_capture_reset(engine);
        return;
    }
    uint32_t frequency = rf_frequencies[engine->freq_index];
    RfShape shape;
    rf_shape_compute(&shape, engine->timings, engine->timing_count);
    uint32_t fingerprint = rf_shape_fingerprint(&shape, frequency);
    RfDecode decode;
    rf_decode(engine->timings, engine->timing_count, &decode);
    uint64_t identity = rf_decode_identity(&decode);
    bool named = decode.protocol != RfProtoNone && decode.protocol != RfProtoOok;
    bool following = engine->mode == RfModeFollow && engine->follow_valid;
    float similarity = 0.0f;
    if(following) {
        if(frequency != engine->follow_frequency) {
            similarity = 0.0f;
        } else if(named && engine->follow_identity) {
            /* both sides decoded: the same transmitter or not, no shape guessing */
            similarity = (decode.protocol == engine->follow_protocol &&
                          identity == engine->follow_identity) ?
                             1.0f :
                             0.0f;
        } else if(named != (engine->follow_protocol != RfProtoOok)) {
            similarity = 0.0f; /* one side is a known protocol, the other is not */
        } else {
            similarity = rf_shape_similarity(&shape, &engine->follow_shape);
        }
        if(similarity < RF_FOLLOW_MATCH) {
            /* Unrelated activity: Follow ignores it. */
            rf_capture_reset(engine);
            return;
        }
    }
    float rssi_avg = engine->rssi_count ? engine->rssi_sum / (float)engine->rssi_count : 0.0f;
    uint32_t duration = engine->capture_us > UINT32_MAX ? UINT32_MAX : (uint32_t)engine->capture_us;
    if(!duration) {
        /* A carrier without OOK edges: use the time it stayed above threshold. */
        duration = (engine->strong_tick - engine->capture_tick + RF_RX_TICK_MS) * 1000U;
    }

    engine->sequence++;
    char id[RF_STORE_ID_MAX + 1];
    snprintf(
        id,
        sizeof(id),
        "rf-%s-%s-%lu",
        engine->device_id,
        engine->session_id,
        (unsigned long)engine->sequence);
    RfSubGhzRecord record = {
        .common =
            {
                .event_id = id,
                .device_id = engine->device_id,
                .session_id = engine->session_id,
                .sequence = engine->sequence,
                .rtc_local_unix = engine->capture_rtc,
                .tz_offset_minutes = engine->config.tz_offset_minutes,
                .monotonic_ms = engine->capture_mono,
                .battery_pct = furi_hal_power_get_pct(),
            },
        .mode = rf_mode_name(engine->mode),
        .frequency_hz = frequency,
        .duration_us = duration,
        .fingerprint = fingerprint,
        .follow = engine->mode == RfModeFollow,
        .follow_fingerprint = following ? engine->follow_fingerprint : fingerprint,
        .follow_similarity = following ? similarity : (engine->mode == RfModeFollow ? 1.0f : 0.0f),
        .rssi_min_dbm = engine->rssi_min,
        .rssi_avg_dbm = rssi_avg,
        .rssi_max_dbm = engine->rssi_max,
        .pulse_count = engine->pulse_count,
        .last_duration_us = engine->last_timing_us,
        .timings = engine->timings,
        .timing_count = engine->timing_count,
        .decode = &decode,
    };
    size_t length = rf_record_subghz(engine->record, RF_RECORD_SIZE, &record, NULL);
    RfStoreResult result =
        length ? rf_store_save(engine->store, id, engine->record, length) : RfStoreErrInvalid;
    if(rf_journal_result(engine, result)) {
        rf_note_event(
            engine,
            fingerprint,
            engine->capture_rtc,
            frequency,
            rf_round_dbm(engine->rssi_max),
            duration);
        engine->last_similarity =
            following ? (uint8_t)(similarity * 100.0f + 0.5f) :
                        (engine->mode == RfModeFollow ? 100U : 0U);
        rf_decode_label(&decode, engine->last_label, sizeof(engine->last_label));
        memcpy(engine->last_info, decode.info, sizeof(engine->last_info));
        engine->last_rolling = decode.rolling;
        if(following) {
            rf_feedback(engine, true);
        } else {
            /* The last observed transmitter becomes the Follow profile. */
            engine->follow_valid = true;
            engine->follow_frequency = frequency;
            engine->follow_fingerprint = fingerprint;
            engine->follow_shape = shape;
            engine->follow_protocol = named ? decode.protocol : RfProtoOok;
            engine->follow_identity = named ? identity : 0;
            memcpy(engine->follow_label, engine->last_label, sizeof(engine->follow_label));
        }
    }
    rf_capture_reset(engine);
}

static void rf_on_timing(RfEngine* engine, uint32_t timing) {
    if(!engine->capturing) {
        rf_pre_push(engine, timing);
        return;
    }
    uint32_t duration = rf_timing_duration(timing);
    if(!rf_timing_level(timing) && duration > engine->config.silence_us) {
        /* A gap longer than silence_us closes the burst; it is context for
         * the next one, not part of this event. */
        rf_capture_close(engine);
        rf_pre_push(engine, timing);
        return;
    }
    if(engine->timing_count < RF_TIMINGS_MAX) engine->timings[engine->timing_count++] = timing;
    engine->pulse_count++;
    engine->capture_us += duration;
    engine->last_timing_us = duration;
}

static void rf_on_rssi(RfEngine* engine, float rssi, uint32_t now) {
    engine->last_rssi = rssi;
    if(!engine->capturing) {
        float trigger = rf_trigger(engine);
        if(engine->floor_samples[engine->freq_index] >= RF_FLOOR_SAMPLES && rssi >= trigger) {
            engine->trigger = trigger; /* fixed for the whole burst */
            rf_capture_open(engine, rssi, now);
        } else {
            rf_floor_track(engine, rssi);
        }
        return;
    }
    if(rssi < engine->rssi_min) engine->rssi_min = rssi;
    if(rssi > engine->rssi_max) engine->rssi_max = rssi;
    engine->rssi_sum += rssi;
    engine->rssi_count++;
    if(rssi >= engine->trigger) engine->strong_tick = now;
}

/* ------------------------------------------------------------------ Geiger counter
 * Follow mode: the live RSSI above the noise floor sets a click rate like a Geiger counter
 * near a source, from a slow background tick to a crackle.  Clicks come at random (Poisson)
 * so a steady signal crackles instead of buzzing; each click is one 5 ms tone burst. */
static void rf_speaker_release(RfEngine* engine) {
    if(engine->click_on) {
        furi_hal_speaker_stop();
        engine->click_on = false;
    }
    if(engine->speaker_held) {
        furi_hal_speaker_release();
        engine->speaker_held = false;
    }
    engine->geiger_rate = 0.0f;
}

static void rf_geiger_service(RfEngine* engine, float rssi, uint32_t now) {
    if(rssi >= engine->peak_rssi) {
        engine->peak_rssi = rssi;
        engine->peak_tick = now;
        engine->peak_decay_tick = now;
    } else if(
        now - engine->peak_tick >= RF_PEAK_HOLD_MS &&
        now - engine->peak_decay_tick >= RF_PEAK_DECAY_MS) {
        engine->peak_decay_tick = now;
        engine->peak_rssi -= 1.0f;
        if(engine->peak_rssi < rssi) engine->peak_rssi = rssi;
    }
    if(engine->mode != RfModeFollow) {
        if(engine->speaker_held) rf_speaker_release(engine);
        engine->geiger_rate = 0.0f;
        return;
    }
    uint8_t fi = engine->freq_index;
    float floor = engine->floor_samples[fi] ? engine->floor[fi] : rssi;
    float x = (rssi - floor - RF_GEIGER_START_DB) / RF_GEIGER_SPAN_DB;
    if(x < 0.0f) x = 0.0f;
    if(x > 1.0f) x = 1.0f;
    float rate = RF_GEIGER_BACKGROUND + RF_GEIGER_MAX_RATE * x * x;
    engine->geiger_rate = rate;
    if(!engine->config.geiger) {
        if(engine->speaker_held) rf_speaker_release(engine);
        engine->geiger_rate = rate;
        return;
    }
    if(!engine->speaker_held) {
        engine->speaker_held = furi_hal_speaker_acquire(5);
        if(!engine->speaker_held) return;
    }
    if(engine->click_on) {
        furi_hal_speaker_stop(); /* a click lasts one RX tick */
        engine->click_on = false;
    }
    uint32_t draw = furi_hal_random_get() % 100000U;
    if((float)draw < rate * ((float)RF_RX_TICK_MS / 1000.0f) * 100000.0f) {
        float hz = RF_GEIGER_CLICK_HZ + (float)(furi_hal_random_get() % 600U) - 300.0f;
        furi_hal_speaker_start(hz, RF_GEIGER_VOLUME);
        engine->click_on = true;
    }
}

static void rf_rx_drain(RfEngine* engine) {
    uint32_t timing;
    for(uint32_t budget = RF_CAPTURE_RING_SIZE; budget && rf_capture_pop(engine->ring, &timing);
        budget--) {
        rf_on_timing(engine, timing);
    }
}

/* ------------------------------------------------------------------ Sub-GHz radio */

static void rf_radio_prepare(RfEngine* engine) {
    if(engine->radio_ready) return;
    furi_hal_subghz_reset();
    furi_hal_subghz_idle();
    /* Async OOK, 650 kHz RX bandwidth: GDO0 carries the demodulated data. */
    furi_hal_subghz_load_custom_preset(subghz_device_cc1101_preset_ook_650khz_async_regs);
    engine->radio_ready = true;
}

static void rf_radio_tune(RfEngine* engine, uint8_t index) {
    rf_radio_prepare(engine);
    if(engine->rx_on) {
        rf_rx_drain(engine);
        rf_capture_close(engine); /* the burst belongs to the old frequency */
        furi_hal_subghz_stop_async_rx();
        engine->rx_on = false;
    }
    furi_hal_subghz_idle();
    furi_hal_subghz_set_frequency_and_path(rf_frequencies[index]);
    furi_hal_subghz_flush_rx();
    rf_capture_discard(engine->ring);
    rf_pre_clear(engine);
    engine->freq_index = index;
    furi_hal_subghz_start_async_rx(rf_capture_isr, engine->ring);
    engine->rx_on = true;
    uint32_t now = furi_get_tick();
    engine->hop_tick = now;
    engine->rssi_tick = now;
    engine->peak_rssi = -200.0f;
    engine->peak_tick = now;
}

static void rf_radio_off(RfEngine* engine) {
    rf_speaker_release(engine);
    if(engine->rx_on) {
        rf_rx_drain(engine);
        rf_capture_close(engine);
        furi_hal_subghz_stop_async_rx();
        engine->rx_on = false;
    }
    if(engine->radio_ready) {
        furi_hal_subghz_sleep();
        engine->radio_ready = false;
    }
    rf_capture_discard(engine->ring);
    rf_pre_clear(engine);
    memset(engine->floor_samples, 0, sizeof(engine->floor_samples));
}

static void rf_rx_service(RfEngine* engine, uint32_t now) {
    rf_rx_drain(engine);
    if(now - engine->rssi_tick >= RF_RX_TICK_MS) {
        engine->rssi_tick = now;
        float rssi = furi_hal_subghz_get_rssi();
        rf_on_rssi(engine, rssi, now);
        rf_geiger_service(engine, rssi, now);
    }
    if(engine->capturing) {
        if(!engine->signal && rf_capture_is_signal(engine)) {
            engine->signal = true;
            /* Feedback as soon as it is a signal; Follow waits until it matches its profile. */
            if(engine->mode != RfModeFollow || !engine->follow_valid) rf_feedback(engine, false);
        }
        uint32_t silence_ms = engine->config.silence_us / 1000U + 1U;
        uint32_t quiet_ms = silence_ms > RF_QUIET_MIN_MS ? silence_ms : RF_QUIET_MIN_MS;
        bool window = now - engine->capture_tick >= engine->config.capture_ms;
        bool edges_idle = now - engine->ring->last_edge_tick > silence_ms;
        bool weak_now = engine->last_rssi < engine->trigger;
        bool quiet = now - engine->strong_tick >= quiet_ms;
        if(window || (edges_idle && weak_now) || quiet) rf_capture_close(engine);
    }
    if(engine->mode == RfModeScout && !engine->capturing &&
       now - engine->hop_tick >= engine->config.dwell_ms) {
        uint8_t next = engine->freq_index;
        if(rf_next_index(engine, &next) && next != engine->freq_index) {
            rf_radio_tune(engine, next);
        } else {
            engine->hop_tick = now;
        }
    }
    if(engine->ring->dropped != engine->dropped_seen) {
        engine->dropped_seen = engine->ring->dropped;
        FURI_LOG_W(TAG, "capture ring overflow, %lu dropped", (unsigned long)engine->dropped_seen);
    }
}

/* ------------------------------------------------------------------ NFC field detector */

static bool rf_nfc_begin(RfEngine* engine) {
    if(engine->nfc_on) return true;
    if(furi_hal_nfc_acquire() != FuriHalNfcErrorNone) return false;
    if(furi_hal_nfc_low_power_mode_stop() != FuriHalNfcErrorNone ||
       furi_hal_nfc_field_detect_start() != FuriHalNfcErrorNone) {
        furi_hal_nfc_low_power_mode_start();
        furi_hal_nfc_release();
        return false;
    }
    engine->nfc_on = true;
    engine->nfc_field = false;
    engine->nfc_present = false;
    return true;
}

static void rf_nfc_close(RfEngine* engine) {
    if(!engine->nfc_field) return;
    engine->nfc_field = false;
    uint32_t duration_ms = engine->nfc_seen_tick - engine->nfc_start_tick + RF_NFC_TICK_MS;
    engine->sequence++;
    char id[RF_STORE_ID_MAX + 1];
    snprintf(
        id,
        sizeof(id),
        "rf-%s-%s-%lu",
        engine->device_id,
        engine->session_id,
        (unsigned long)engine->sequence);
    RfNfcRecord record = {
        .common =
            {
                .event_id = id,
                .device_id = engine->device_id,
                .session_id = engine->session_id,
                .sequence = engine->sequence,
                .rtc_local_unix = engine->nfc_rtc,
                .tz_offset_minutes = engine->config.tz_offset_minutes,
                .monotonic_ms = engine->nfc_mono,
                .battery_pct = furi_hal_power_get_pct(),
            },
        .duration_ms = duration_ms,
        .field_count = engine->nfc_field_count,
    };
    size_t length = rf_record_nfc(engine->record, RF_RECORD_SIZE, &record);
    RfStoreResult result =
        length ? rf_store_save(engine->store, id, engine->record, length) : RfStoreErrInvalid;
    if(rf_journal_result(engine, result)) {
        uint64_t duration_us = (uint64_t)duration_ms * 1000U;
        rf_note_event(
            engine,
            RF_NFC_FINGERPRINT,
            engine->nfc_rtc,
            RF_NFC_FREQUENCY_HZ,
            0,
            duration_us > UINT32_MAX ? UINT32_MAX : (uint32_t)duration_us);
        engine->last_similarity = 0;
    }
}

static void rf_nfc_end(RfEngine* engine) {
    if(!engine->nfc_on) return;
    rf_nfc_close(engine); /* a field still present is recorded with its duration so far */
    furi_hal_nfc_field_detect_stop();
    furi_hal_nfc_low_power_mode_start();
    furi_hal_nfc_release();
    engine->nfc_on = false;
}

static void rf_nfc_service(RfEngine* engine, uint32_t now) {
    bool present = furi_hal_nfc_field_is_present();
    /* Every field-on counts, also the ones merged into an open event. */
    if(present && !engine->nfc_present) engine->nfc_field_count++;
    engine->nfc_present = present;
    if(present) {
        if(!engine->nfc_field) {
            engine->nfc_field = true;
            engine->nfc_start_tick = now;
            engine->nfc_rtc = rf_rtc_now();
            engine->nfc_mono = now - engine->session_tick;
            rf_feedback(engine, false);
        }
        engine->nfc_seen_tick = now;
    } else if(engine->nfc_field && now - engine->nfc_seen_tick >= RF_NFC_MERGE_MS) {
        rf_nfc_close(engine);
    }
}

/* ------------------------------------------------------------------ control */

static void rf_receivers_off(RfEngine* engine) {
    /* Never both on; NFC first so SPI bus R is free before the CC1101 is touched. */
    rf_nfc_end(engine);
    rf_radio_off(engine);
}

static void rf_receivers_update(RfEngine* engine, bool keep) {
    if(!engine->running) {
        rf_receivers_off(engine);
        return;
    }
    if(engine->mode == RfModeNfc) {
        if(engine->nfc_on) return;
        rf_radio_off(engine); /* CC1101 asleep before NFC takes SPI bus R */
        engine->nfc_retry_tick = furi_get_tick();
        if(!rf_nfc_begin(engine)) FURI_LOG_W(TAG, "NFC HAL busy, retrying");
        return;
    }
    rf_nfc_end(engine); /* releases SPI bus R before the CC1101 is used */
    uint8_t index = RF_FREQ_433;
    if(!rf_pick_index(engine, keep, &index)) {
        rf_radio_off(engine);
        return;
    }
    if(!engine->rx_on || index != engine->freq_index) rf_radio_tune(engine, index);
}

static void rf_apply_control(RfEngine* engine) {
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    bool dirty = engine->control_dirty;
    engine->control_dirty = false;
    engine->wake_queued = false;
    RfConfig config = engine->want_config;
    RfMode mode = engine->want_mode;
    bool running = engine->want_running;
    furi_mutex_release(engine->mutex);
    if(!dirty) return;
    rf_config_sanitize(&config);
    bool mode_changed = mode != engine->mode;
    if(mode_changed) {
        bool was_nfc = engine->mode == RfModeNfc;
        bool is_nfc = mode == RfModeNfc;
        if(was_nfc != is_nfc) {
            rf_receivers_off(engine);
        } else if(engine->rx_on) {
            /* Close the burst under the mode it was recorded in. */
            rf_rx_drain(engine);
            rf_capture_close(engine);
        }
        engine->mode = mode;
        if(mode != RfModeFollow) rf_speaker_release(engine);
    }
    engine->config = config;
    engine->running = running;
    rf_receivers_update(engine, !mode_changed);
}

/* ------------------------------------------------------------------ status */

static void rf_publish(RfEngine* engine, uint32_t now, bool force) {
    RfStatus status;
    memset(&status, 0, sizeof(status));
    status.mode = engine->mode;
    status.running = engine->rx_on || engine->nfc_on;
    status.frequency_hz = engine->mode == RfModeNfc ? 0 : rf_frequencies[engine->freq_index];
    status.events = engine->events;
    status.families = engine->family_count;
    status.pending = rf_store_pending(engine->store);
    status.carry = rf_store_carry(engine->store);
    status.listed = rf_store_listed(engine->store);
    status.errors = engine->errors;
    status.free_kb = rf_store_free_kb(engine->store);
    status.storage_full = engine->storage_full;
    status.last_unix = engine->last_unix;
    status.last_frequency_hz = engine->last_frequency;
    status.last_rssi_dbm = engine->last_rssi_dbm;
    status.last_duration_us = engine->last_duration;
    status.follow_valid = engine->follow_valid;
    status.last_similarity = engine->last_similarity;
    status.nfc_field = engine->nfc_field;
    memcpy(status.last_label, engine->last_label, sizeof(status.last_label));
    memcpy(status.last_info, engine->last_info, sizeof(status.last_info));
    status.last_rolling = engine->last_rolling;
    memcpy(status.follow_label, engine->follow_label, sizeof(status.follow_label));
    if(engine->rx_on) {
        status.live_rssi_dbm = rf_round_dbm(engine->last_rssi);
        status.peak_rssi_dbm = rf_round_dbm(engine->peak_rssi > -150.0f ? engine->peak_rssi : engine->last_rssi);
        uint8_t fi = engine->freq_index;
        status.floor_dbm = engine->floor_samples[fi] ? rf_round_dbm(engine->floor[fi]) : 0;
    }
    status.geiger_rate = (uint8_t)(engine->geiger_rate + 0.5f);
    status.geiger_sound = engine->speaker_held;
    uint32_t stored = rf_store_stored(engine->store);

    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    status.unseen = engine->status.unseen + engine->unseen_add;
    engine->unseen_add = 0;
    bool differs = memcmp(&status, &engine->status, sizeof(status)) != 0 ||
                   stored != engine->stored;
    engine->status = status;
    engine->stored = stored;
    bool exiting = engine->exiting;
    furi_mutex_release(engine->mutex);

    if(differs) engine->changed_pending = true;
    if(engine->changed_pending && !exiting &&
       (force || now - engine->changed_tick >= RF_CHANGED_MIN_MS)) {
        engine->changed_pending = false;
        engine->changed_tick = now;
        if(engine->changed) engine->changed(engine->context);
    }
}

/* ------------------------------------------------------------------ worker */

static void rf_reply(const char* line, void* context) {
    RfEngine* engine = context;
    if(engine->reply && !rf_exiting(engine)) engine->reply(line, engine->context);
}

static void rf_handle_request(RfEngine* engine, RfMsg* message) {
    if(message->too_long) {
        rf_reply("RX|bad|line too long", engine);
    } else {
        rf_proto_handle(
            engine->store,
            message->line,
            engine->config.keep_uploaded,
            engine->line,
            rf_reply,
            engine);
    }
    engine->request_tick = furi_get_tick();
    engine->store_handle = true;
}

static void rf_service(RfEngine* engine, uint32_t now) {
    if(engine->rx_on) rf_rx_service(engine, now);
    if(engine->nfc_on) {
        rf_nfc_service(engine, now);
    } else if(
        engine->running && engine->mode == RfModeNfc &&
        now - engine->nfc_retry_tick >= RF_NFC_RETRY_MS) {
        engine->nfc_retry_tick = now;
        rf_nfc_begin(engine);
    }
    if(now - engine->free_tick >= RF_FREE_REFRESH_MS) {
        engine->free_tick = now;
        if(rf_store_refresh_free(engine->store)) engine->storage_full = false;
    }
    if(engine->store_handle && now - engine->request_tick >= RF_STORE_IDLE_MS) {
        rf_store_idle(engine->store);
        engine->store_handle = false;
    }
}

static uint32_t rf_wait_ticks(RfEngine* engine, uint32_t now) {
    uint32_t wait = RF_IDLE_TICK_MS;
    if(engine->rx_on) {
        wait = RF_RX_TICK_MS;
    } else if(engine->nfc_on) {
        wait = RF_NFC_TICK_MS;
    }
    if(engine->changed_pending) {
        uint32_t elapsed = now - engine->changed_tick;
        uint32_t left = elapsed >= RF_CHANGED_MIN_MS ? 1U : RF_CHANGED_MIN_MS - elapsed;
        if(left < wait) wait = left;
    }
    return furi_ms_to_ticks(wait);
}

static int32_t rf_worker(void* context) {
    RfEngine* engine = context;
    uint32_t now = furi_get_tick();
    engine->session_tick = now;
    engine->feedback_tick = now - RF_FEEDBACK_MIN_MS;
    engine->error_tick = now - RF_ERROR_BLINK_MS;
    engine->free_tick = now;
    rf_store_open(engine->store);
    memcpy(engine->device_id, rf_store_device_id(engine->store), sizeof(engine->device_id));
    engine->device_id[sizeof(engine->device_id) - 1] = '\0';
    rf_make_session_id(engine);
    engine->changed_pending = true;
    rf_publish(engine, furi_get_tick(), true);

    RfMsg message;
    while(true) {
        now = furi_get_tick();
        if(furi_message_queue_get(engine->queue, &message, rf_wait_ticks(engine, now)) ==
           FuriStatusOk) {
            if(message.type == RfMsgExit) break;
            if(message.type == RfMsgRequest) rf_handle_request(engine, &message);
        }
        rf_apply_control(engine);
        now = furi_get_tick();
        rf_service(engine, now);
        rf_publish(engine, now, false);
    }

    /* Record what is still open, then leave the radio asleep and NFC released. */
    rf_receivers_off(engine);
    rf_store_idle(engine->store);
    return 0;
}

/* ------------------------------------------------------------------ public API */

/* Called with the mutex held after a control field changed: marks the change
 * and posts at most one wake message until the worker has taken it.  A full
 * queue is fine: the worker applies control state on every loop turn. */
static void rf_control_commit(RfEngine* engine) {
    engine->control_dirty = true;
    bool post = !engine->wake_queued;
    engine->wake_queued = true;
    furi_mutex_release(engine->mutex);
    if(post) {
        RfMsg message;
        memset(&message, 0, sizeof(message));
        message.type = RfMsgWake;
        furi_message_queue_put(engine->queue, &message, 0);
    }
}

RfEngine* rf_engine_alloc(RfReplyCallback reply, RfChangedCallback changed, void* context) {
    RfEngine* engine = malloc(sizeof(RfEngine));
    memset(engine, 0, sizeof(RfEngine));
    engine->reply = reply;
    engine->changed = changed;
    engine->context = context;
    rf_config_defaults(&engine->want_config);
    engine->config = engine->want_config;
    engine->want_mode = RfModeScout;
    engine->mode = RfModeScout;
    engine->freq_index = RF_FREQ_433;
    engine->status.mode = RfModeScout;
    engine->status.frequency_hz = rf_frequencies[RF_FREQ_433];

    engine->ring = malloc(sizeof(RfCapture));
    rf_capture_init(engine->ring);
    engine->timings = malloc(sizeof(uint32_t) * RF_TIMINGS_MAX);
    engine->record = malloc(RF_RECORD_SIZE);
    engine->line = malloc(RF_PROTO_LINE_MAX);
    engine->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    engine->queue = furi_message_queue_alloc(RF_QUEUE_DEPTH, sizeof(RfMsg));
    engine->storage = furi_record_open(RECORD_STORAGE);
    engine->notifications = furi_record_open(RECORD_NOTIFICATION);
    engine->store = rf_store_alloc(engine->storage);

    engine->thread = furi_thread_alloc_ex("RfEngine", RF_ENGINE_STACK, rf_worker, engine);
    furi_thread_start(engine->thread);
    return engine;
}

void rf_engine_free(RfEngine* engine) {
    if(!engine) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    engine->exiting = true;
    furi_mutex_release(engine->mutex);
    RfMsg message;
    memset(&message, 0, sizeof(message));
    message.type = RfMsgExit;
    furi_message_queue_put(engine->queue, &message, FuriWaitForever);
    furi_thread_join(engine->thread);
    furi_thread_free(engine->thread);

    rf_store_free(engine->store);
    furi_record_close(RECORD_NOTIFICATION);
    furi_record_close(RECORD_STORAGE);
    furi_message_queue_free(engine->queue);
    furi_mutex_free(engine->mutex);
    free(engine->line);
    free(engine->record);
    free(engine->timings);
    free(engine->ring);
    free(engine);
}

void rf_engine_configure(RfEngine* engine, const RfConfig* config) {
    if(!engine || !config) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    engine->want_config = *config;
    rf_control_commit(engine);
}

void rf_engine_set_mode(RfEngine* engine, RfMode mode) {
    if(!engine || mode >= RfModeCount) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    engine->want_mode = mode;
    rf_control_commit(engine);
}

void rf_engine_start(RfEngine* engine) {
    if(!engine) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    engine->want_running = true;
    rf_control_commit(engine);
}

void rf_engine_stop(RfEngine* engine) {
    if(!engine) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    engine->want_running = false;
    rf_control_commit(engine);
}

void rf_engine_mark_seen(RfEngine* engine) {
    if(!engine) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    engine->status.unseen = 0;
    furi_mutex_release(engine->mutex);
}

void rf_engine_get_status(RfEngine* engine, RfStatus* out) {
    if(!engine || !out) return;
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    *out = engine->status;
    furi_mutex_release(engine->mutex);
}

void rf_engine_request(RfEngine* engine, const char* line) {
    if(!engine || !line) return;
    RfMsg message;
    memset(&message, 0, sizeof(message));
    message.type = RfMsgRequest;
    size_t length = strlen(line);
    while(length && (line[length - 1] == '\n' || line[length - 1] == '\r')) length--;
    if(length >= sizeof(message.line)) {
        message.too_long = true;
    } else {
        memcpy(message.line, line, length);
        message.line[length] = '\0';
    }
    if(furi_message_queue_put(engine->queue, &message, 0) != FuriStatusOk) {
        FURI_LOG_W(TAG, "request queue full, line dropped");
    }
}

void rf_engine_status_line(RfEngine* engine, char* out, size_t size) {
    if(!out || !size) return;
    if(!engine) {
        out[0] = '\0';
        return;
    }
    furi_mutex_acquire(engine->mutex, FuriWaitForever);
    unsigned state = !engine->status.running ? 0U : (engine->status.mode == RfModeNfc ? 2U : 1U);
    snprintf(
        out,
        size,
        "R|%lu|%lu|%lu|%u|%lu|%lu",
        (unsigned long)engine->status.listed,
        (unsigned long)engine->stored,
        (unsigned long)engine->status.free_kb,
        state,
        (unsigned long)engine->status.errors,
        (unsigned long)engine->status.carry);
    furi_mutex_release(engine->mutex);
}
