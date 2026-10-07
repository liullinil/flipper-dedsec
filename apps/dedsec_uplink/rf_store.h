#pragma once

/* RF journal on the SD card (engine thread only).
 *
 *   apps_data/dedsec_uplink/rf/events/<id>.json    pending records, one JSON object each
 *   apps_data/dedsec_uplink/rf/uploaded/<id>.json  records kept after an ACK (keep_uploaded)
 *   apps_data/dedsec_uplink/rf/device_id           16 hex chars, stable per SD card
 *   apps_data/dedsec_uplink/rf/inflight            intent marker of the last write/move
 *
 * Paths are absolute: /data would resolve against the calling thread's app id. */

#include <furi.h>
#include <storage/storage.h>

#define RF_STORE_ROOT          EXT_PATH("apps_data/dedsec_uplink/rf")
#define RF_STORE_EVENTS_DIR    RF_STORE_ROOT "/events"
#define RF_STORE_UPLOADED_DIR  RF_STORE_ROOT "/uploaded"
#define RF_STORE_DEVICE_ID     RF_STORE_ROOT "/device_id"
#define RF_STORE_INFLIGHT      RF_STORE_ROOT "/inflight"
#define RF_STORE_ID_MAX        48U /* contract: [A-Za-z0-9][A-Za-z0-9_.-]{0,47} */
#define RF_STORE_RECORD_MAX    (64U * 1024U) /* larger files are never served */
#define RF_STORE_RESERVE_BYTES (1024U * 1024U) /* SD space left for everything else */
#define RF_STORE_UPLOADED_MAX  512U /* uploaded/ copies kept at most */

typedef enum {
    RfStoreOk = 0,
    RfStoreErrInvalid, /* malformed id, argument or offset */
    RfStoreErrNotFound,
    RfStoreErrExists,
    RfStoreErrLowSpace,
    RfStoreErrIo,
    RfStoreErrMismatch, /* ACK size/crc32 differ from the record */
} RfStoreResult;

typedef struct RfStore RfStore;

/* Allocation does no I/O; rf_store_open() does (call it on the engine thread). */
RfStore* rf_store_alloc(Storage* storage);
void rf_store_free(RfStore* store);

/* Creates the folders, loads/creates the device id, finishes an interrupted
 * write or move and counts the records. */
void rf_store_open(RfStore* store);

/* Releases the cached read handle (call when the link is quiet). */
void rf_store_idle(RfStore* store);

const char* rf_store_device_id(const RfStore* store);
bool rf_store_valid_id(const char* id);
uint32_t rf_store_pending(const RfStore* store); /* files in events/ */
uint32_t rf_store_stored(const RfStore* store); /* events/ + uploaded/ */
uint32_t rf_store_free_kb(const RfStore* store); /* last measured SD free space */
bool rf_store_refresh_free(RfStore* store); /* measure; false when below the reserve */

/* Commit one record (must be a JSON object ending in "}\n"). */
RfStoreResult rf_store_save(RfStore* store, const char* id, const char* data, size_t length);

/* The pending record at directory index `cursor`; id needs RF_STORE_ID_MAX + 1 bytes. */
RfStoreResult rf_store_list(
    RfStore* store,
    uint32_t cursor,
    char* id,
    size_t id_size,
    uint32_t* size,
    uint32_t* crc32);

/* Bytes [offset, offset + *got) of a pending record; *got == 0 at the end. */
RfStoreResult rf_store_read(
    RfStore* store,
    const char* id,
    uint32_t offset,
    uint8_t* out,
    size_t capacity,
    size_t* got,
    uint32_t* size);

/* Verify size + crc32, then move the record to uploaded/ (keep) or delete it.
 * Repeating an ACK that already succeeded returns RfStoreOk. */
RfStoreResult
    rf_store_ack(RfStore* store, const char* id, uint32_t size, uint32_t crc32, bool keep);

/* zlib/binascii compatible CRC-32: start with 0, feed chunks. */
uint32_t rf_store_crc32(uint32_t crc, const void* data, size_t length);
