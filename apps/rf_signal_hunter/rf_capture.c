#include "rf_capture.h"

void rf_capture_init(RfCaptureEngine* engine) {
    memset(engine, 0, sizeof(*engine));
}

void rf_capture_isr(RfCaptureEngine* engine, bool level, uint32_t duration_us) {
    uint32_t write = engine->write_index;
    uint32_t next = (write + 1U) % RF_CAPTURE_MAX_TIMINGS;
    if(next == engine->read_index) {
        engine->dropped++;
        return;
    }
    engine->ring[write].level = level;
    engine->ring[write].duration_us = duration_us;
    engine->write_index = next;
    engine->last_tick = furi_get_tick();
}

bool rf_capture_pop(RfCaptureEngine* engine, RfCaptureTiming* timing) {
    uint32_t read = engine->read_index;
    if(read == engine->write_index) return false;
    *timing = engine->ring[read];
    engine->read_index = (read + 1U) % RF_CAPTURE_MAX_TIMINGS;
    return true;
}

uint32_t rf_capture_pending(const RfCaptureEngine* engine) {
    uint32_t write = engine->write_index;
    uint32_t read = engine->read_index;
    return write >= read ? write - read : RF_CAPTURE_MAX_TIMINGS - read + write;
}

