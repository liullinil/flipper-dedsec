/* Runs the production rf_decode.c on timings read from stdin, one burst per line:
 * "<first_level> <us> <us> ..." (levels alternate).  Prints one JSON object per line. */
#include "rf_capture.h"
#include "rf_decode.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_TIMINGS 2048

int main(void) {
    static uint32_t timings[MAX_TIMINGS];
    static char line[65536];
    while(fgets(line, sizeof(line), stdin)) {
        char* p = line;
        char* end;
        long first = strtol(p, &end, 10);
        if(end == p) continue;
        p = end;
        uint32_t n = 0;
        bool level = first != 0;
        while(n < MAX_TIMINGS) {
            long us = strtol(p, &end, 10);
            if(end == p) break;
            p = end;
            timings[n++] = rf_timing_pack(level, (uint32_t)us);
            level = !level;
        }
        RfDecode d;
        rf_decode(timings, n, &d);
        char label[32];
        rf_decode_label(&d, label, sizeof(label));
        printf(
            "{\"protocol\":%u,\"name\":\"%s\",\"info\":\"%s\",\"bits\":%u,\"key\":%llu,"
            "\"frames\":%u,\"identical\":%u,\"te_us\":%lu,\"rolling\":%s,\"confidence\":%u,"
            "\"identity\":%llu,\"label\":\"%s\"}\n",
            d.protocol,
            d.name,
            d.info,
            d.bits,
            (unsigned long long)d.key,
            d.frames,
            d.identical,
            (unsigned long)d.te_us,
            d.rolling ? "true" : "false",
            d.confidence,
            (unsigned long long)rf_decode_identity(&d),
            label);
        fflush(stdout);
    }
    return 0;
}
