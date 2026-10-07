#include "rf_capture.h"

#include <furi.h>
#include <string.h>

#define RF_CAPTURE_MASK (RF_CAPTURE_RING_SIZE - 1U)

void rf_capture_init(RfCapture* capture) {
    memset((void*)capture, 0, sizeof(*capture));
}

void rf_capture_isr(bool level, uint32_t duration, void* context) {
    RfCapture* capture = context;
    uint32_t head = capture->head;
    if(head - capture->tail >= RF_CAPTURE_RING_SIZE) {
        capture->dropped++;
    } else {
        capture->ring[head & RF_CAPTURE_MASK] = rf_timing_pack(level, duration);
        /* Publish the slot only after it has been written. */
        capture->head = head + 1U;
    }
    capture->last_edge_tick = furi_get_tick();
}

bool rf_capture_pop(RfCapture* capture, uint32_t* timing) {
    uint32_t tail = capture->tail;
    if(tail == capture->head) return false;
    *timing = capture->ring[tail & RF_CAPTURE_MASK];
    capture->tail = tail + 1U;
    return true;
}

void rf_capture_discard(RfCapture* capture) {
    capture->tail = capture->head;
}
