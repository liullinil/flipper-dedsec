#pragma once

#include <furi.h>
#include <storage/storage.h>

/* Self-update over BLE. The PC companion streams the new .fap as base64 chunks; we write it
 * next to ourselves (<path>.new), check size + CRC-32, swap it in and relaunch via the loader.
 *
 *   PC -> Flipper:  N|tag|size            a newer release exists
 *                   UB|tag|size|crc32     transfer begins
 *                   UD|offset|base64      one chunk (sent only within the ack window)
 *                   UE|tag                transfer finished
 *   Flipper -> PC:  V|version             our version (on link up and every 30 s)
 *                   U|tag                 please send this release
 *                   UA|written            ack: bytes written so far (also used to resync)
 */

#define UPLINK_VERSION "1.3.0"

typedef enum {
    OtaIdle,
    OtaRequested,
    OtaReceiving,
    OtaDone,
    OtaFailed,
} OtaState;

typedef struct {
    OtaState state;
    char self_path[128];
    char tmp_path[136];
    char tag[16];       // newest version the PC told us about
    bool available;     // tag is newer than UPLINK_VERSION
    bool notified;      // user already alerted for this tag
    uint32_t size;
    uint32_t written;
    uint32_t crc;
    uint32_t crc_expect;
    Storage* storage;
    File* file;
    char error[32];
} Ota;

void ota_init(Ota* ota);
void ota_free(Ota* ota);

/* true if `tag` (e.g. "v1.2.0") is newer than `mine` (e.g. "1.1.0") */
bool ota_newer(const char* tag, const char* mine);

bool ota_begin(Ota* ota, const char* tag, uint32_t size, uint32_t crc);
/* returns bytes written after this chunk (send it back as the ack), or -1 on a write error.
 * A chunk with an unexpected offset is ignored and the current count is returned (resync). */
int32_t ota_chunk(Ota* ota, uint32_t offset, const char* b64);
/* verify and swap the new file in; true on success (state OtaDone), false (OtaFailed) */
bool ota_finish(Ota* ota);
void ota_abort(Ota* ota, const char* why);
uint8_t ota_percent(const Ota* ota);
