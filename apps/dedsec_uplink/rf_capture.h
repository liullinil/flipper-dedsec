#pragma once

/* Lock-free single-producer/single-consumer ring between the Sub-GHz capture
 * ISR (producer) and the RF engine thread (consumer).  Each entry packs one
 * level duration into 32 bits: bit 31 is the level (1 = carrier/mark) and
 * bits 0..30 the duration in microseconds, so 1024 entries cost 4 KiB. */

#include <stdbool.h>
#include <stdint.h>

#define RF_CAPTURE_RING_SIZE   1024U /* power of two */
#define RF_TIMING_LEVEL_BIT    0x80000000UL
#define RF_TIMING_DURATION_MAX 0x7FFFFFFFUL

typedef struct {
    volatile uint32_t ring[RF_CAPTURE_RING_SIZE];
    volatile uint32_t head; /* written by the ISR only */
    volatile uint32_t tail; /* written by the engine thread only */
    volatile uint32_t dropped; /* entries lost because the ring was full */
    volatile uint32_t last_edge_tick; /* furi tick of the newest entry */
} RfCapture;

static inline uint32_t rf_timing_pack(bool level, uint32_t duration_us) {
    if(duration_us > RF_TIMING_DURATION_MAX) duration_us = RF_TIMING_DURATION_MAX;
    return (level ? RF_TIMING_LEVEL_BIT : 0U) | duration_us;
}

static inline bool rf_timing_level(uint32_t timing) {
    return (timing & RF_TIMING_LEVEL_BIT) != 0U;
}

static inline uint32_t rf_timing_duration(uint32_t timing) {
    return timing & RF_TIMING_DURATION_MAX;
}

void rf_capture_init(RfCapture* capture);

/* FuriHalSubGhzCaptureCallback: runs in the TIM2 interrupt. */
void rf_capture_isr(bool level, uint32_t duration, void* context);

/* Engine thread: take the oldest entry. */
bool rf_capture_pop(RfCapture* capture, uint32_t* timing);

/* Engine thread: drop everything queued so far (e.g. after a retune). */
void rf_capture_discard(RfCapture* capture);
