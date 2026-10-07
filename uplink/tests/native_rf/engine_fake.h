#pragma once
/* Deterministic single-threaded world for running the real rf_engine.c on a
 * host.  Time only moves while the engine thread waits on its queue; scripted
 * steps then act as the "other threads" (UI calls) and as the RF/NFC
 * environment.  The fake HAL aborts (exit 2) on every call order the firmware
 * would furi_check() or deadlock on:
 *   - set_frequency / flush / idle / reset during async RX, RX without preset;
 *   - stop_async_rx without RX, sleep during RX;
 *   - any CC1101 call while the NFC HAL (SPI bus R) is acquired, NFC acquire
 *     while the CC1101 is awake;
 *   - any HAL call outside the engine thread or from a caller ("script") step;
 *   - re-acquiring the (non-recursive) mutex. */
#include <furi.h>

typedef void (*WorldStep)(void);

typedef struct {
    uint32_t at_ms;
    uint32_t frequency;
} WorldTune;

void world_reset(void);
/* OOK transmission: carrier `rssi_dbm` during marks, nothing during spaces. */
void world_add_tx(
    uint32_t start_ms,
    uint32_t end_ms,
    uint32_t frequency,
    float rssi_dbm,
    uint32_t mark_us,
    uint32_t space_us);
/* Random demodulator noise edges (RSSI stays at the noise floor). */
void world_set_noise(uint32_t start_ms, uint32_t end_ms);
/* A raised noise floor on one frequency (315 MHz next to a PC) with a single-sample RSSI spike
 * every `spike_every_ms` (no demodulator edges); 0 Hz turns it off. */
void world_set_floor(uint32_t frequency, float floor_dbm, float spike_dbm, uint32_t spike_every_ms);
void world_add_nfc_field(uint32_t start_ms, uint32_t end_ms);
void world_nfc_busy(uint32_t attempts);
void world_at(uint32_t at_ms, WorldStep step); /* in time order */
void world_end_at(uint32_t at_ms);
uint32_t world_now(void);

uint32_t world_tunes(const WorldTune** tunes);
uint32_t world_feedback(uint32_t vibro_pulses); /* sequences with that many vibro pulses */
uint32_t world_red_blinks(void);
bool world_radio_asleep(void);
bool world_nfc_released(void);
uint32_t world_queue_drops(void);
