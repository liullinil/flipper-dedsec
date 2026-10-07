#pragma once
/* Host stand-in: only the receive-side Sub-GHz HAL.  No TX function is
 * declared, so an engine that tried to transmit would not build here. */
#include <stdbool.h>
#include <stdint.h>
typedef void (*FuriHalSubGhzCaptureCallback)(bool level, uint32_t duration, void* context);
void furi_hal_subghz_reset(void);
void furi_hal_subghz_idle(void);
void furi_hal_subghz_sleep(void);
void furi_hal_subghz_load_custom_preset(const uint8_t* preset_data);
bool furi_hal_subghz_is_frequency_valid(uint32_t value);
uint32_t furi_hal_subghz_set_frequency_and_path(uint32_t value);
void furi_hal_subghz_flush_rx(void);
float furi_hal_subghz_get_rssi(void);
void furi_hal_subghz_start_async_rx(FuriHalSubGhzCaptureCallback callback, void* context);
void furi_hal_subghz_stop_async_rx(void);
