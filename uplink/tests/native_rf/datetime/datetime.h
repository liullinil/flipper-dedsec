#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {
    uint8_t hour;
    uint8_t minute;
    uint8_t second;
    uint8_t day;
    uint8_t month;
    uint16_t year;
    uint8_t weekday;
} DateTime;
uint32_t datetime_datetime_to_timestamp(DateTime* datetime);
