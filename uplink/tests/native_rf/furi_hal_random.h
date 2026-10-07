#pragma once
#include <stddef.h>
#include <stdint.h>
void furi_hal_random_fill_buf(void* data, size_t size);
uint32_t furi_hal_random_get(void);
