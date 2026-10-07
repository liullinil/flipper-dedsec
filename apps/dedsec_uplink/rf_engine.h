#pragma once

/* Passive RF engine of DedSec Uplink (integration contract v1, section 1).
 *
 * The engine owns a worker thread: every Sub-GHz/NFC HAL call and every
 * journal write happens there.  The functions below may be called from any
 * app thread; they post to the worker's queue or copy a mutex-protected
 * snapshot.  The callbacks run on the engine thread and are never invoked
 * after rf_engine_free() has been entered.  Never call rf_engine_free()
 * while holding a lock that the callbacks take.
 *
 * Passive only: no Sub-GHz TX, no NFC poller/listener/field-on. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { RfModeScout, RfModeCapture, RfModeFollow, RfModeNfc, RfModeCount } RfMode;
typedef enum { RfBandAll, RfBand433, RfBand315, RfBand868, RfBandCount } RfBand;

typedef struct {
    uint8_t band; // RfBand
    int8_t rssi_threshold_dbm; // e.g. -75; bursts below are ignored
    uint16_t dwell_ms; // Scout hop interval per frequency
    uint16_t capture_ms; // longest capture window
    uint16_t silence_us; // gap that closes a burst
    bool feedback; // short vibro + LED blink on events (rate limited)
    bool keep_uploaded; // after an ACK move the record to uploaded/ instead of deleting it
    int16_t tz_offset_minutes; // RTC local time minus UTC (sent by the PC, see Z| below)
} RfConfig;

typedef struct {
    RfMode mode;
    bool running; // receiver / field detector active
    uint32_t frequency_hz; // current tuning (0 in NFC mode)
    uint32_t events; // events recorded since the engine started
    uint32_t families; // distinct local fingerprints since start
    uint32_t unseen; // events since the last rf_engine_mark_seen()
    uint32_t pending; // records waiting for upload (files in events/)
    uint32_t errors; // failed journal writes since start
    uint32_t free_kb; // SD free space
    bool storage_full;
    uint32_t last_unix; // UTC time of the last event (0 = none)
    uint32_t last_frequency_hz;
    int16_t last_rssi_dbm;
    uint32_t last_duration_us;
    bool follow_valid; // Follow has a profile (from the last event)
    uint8_t last_similarity; // Follow match of the last event, 0..100
    bool nfc_field; // NFC field present right now
} RfStatus;

typedef struct RfEngine RfEngine;
typedef void (*RfReplyCallback)(const char* line, void* context); // one protocol line, no '\n'
typedef void (*RfChangedCallback)(void* context); // status changed / new event

RfEngine* rf_engine_alloc(RfReplyCallback reply, RfChangedCallback changed, void* context);
void rf_engine_free(RfEngine* engine); // stops the radio, releases HAL, joins the thread
void rf_engine_configure(RfEngine* engine, const RfConfig* config);
void rf_engine_set_mode(RfEngine* engine, RfMode mode);
void rf_engine_start(RfEngine* engine); // receiver/detector on (current mode)
void rf_engine_stop(RfEngine* engine);
void rf_engine_mark_seen(RfEngine* engine); // user looked at the RF tab: unseen = 0
void rf_engine_get_status(RfEngine* engine, RfStatus* out);
void rf_engine_request(RfEngine* engine, const char* line); // a PC line starting with "R" (see §2)
void rf_engine_status_line(RfEngine* engine, char* out, size_t size); // formats the R| line
