#pragma once

/* PC <-> Flipper RF sync requests over the Uplink link (contract section 2):
 *
 *   RO|pc_id                   -> RO|listed|carried   who the PC is (before RL and RP)
 *   RL|cursor                  -> RI|next|event_id|size|crc32  or  RE
 *   RR|event_id|offset         -> RD|event_id|offset|base64     (<= 120 bytes)
 *   RA|event_id|size|crc32     -> RK|event_id
 *   RP|event_id|size|crc32     -> RG|event_id|0 (send it)  or  RH|event_id (already here)
 *   RW|event_id|offset|base64  -> RG|event_id|received   (<= 180 bytes, in order)
 *   any failure                -> RX|code|text  (nf, bad, io; text starts with the id when known)
 *
 * RP/RW carry a record of the PC to another PC: it is stored in carry/<pc_id>/ and listed to
 * every other identified PC. crc32 is binascii.crc32 of the whole record as an unsigned
 * decimal. */

#include "rf_store.h"

#define RF_PROTO_LINE_MAX 243U /* one BLE notification including '\n' */
#define RF_PROTO_CHUNK    120U /* record bytes per RD line */
#define RF_PROTO_PUT_MAX  180U /* record bytes per RW line */
#define RF_PROTO_REQUEST_MAX \
    (3U + RF_STORE_ID_MAX + 12U + (RF_PROTO_PUT_MAX / 3U) * 4U + 1U) /* longest request + NUL */

typedef void (*RfProtoReply)(const char* line, void* context);

/* Handle one request line (modified in place).  Every request produces exactly
 * one reply through `reply` (no trailing '\n').  `scratch` must hold
 * RF_PROTO_LINE_MAX bytes. */
void rf_proto_handle(
    RfStore* store,
    char* line,
    bool keep_uploaded,
    char* scratch,
    RfProtoReply reply,
    void* context);
