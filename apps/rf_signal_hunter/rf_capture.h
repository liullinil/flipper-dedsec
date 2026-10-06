#pragma once

#include <furi.h>
#include <stdbool.h>
#include <stdint.h>

#define RF_CAPTURE_MAX_TIMINGS 2048U
#define RF_CAPTURE_PRETRIGGER 64U

typedef struct {
    bool level;
    uint32_t duration_us;
} RfCaptureTiming;

typedef struct {
    volatile RfCaptureTiming ring[RF_CAPTURE_MAX_TIMINGS];
    volatile uint32_t write_index;
    volatile uint32_t read_index;
    volatile uint32_t dropped;
    volatile uint32_t last_tick;
    volatile bool burst_open;
} RfCaptureEngine;

void rf_capture_init(RfCaptureEngine* engine);
void rf_capture_isr(RfCaptureEngine* engine, bool level, uint32_t duration_us);
bool rf_capture_pop(RfCaptureEngine* engine, RfCaptureTiming* timing);
uint32_t rf_capture_pending(const RfCaptureEngine* engine);

