#pragma once

/* PC <-> Flipper RF sync requests over the Uplink link (contract section 2):
 *
 *   RL|cursor               -> RI|next|event_id|size|crc32  or  RE
 *   RR|event_id|offset      -> RD|event_id|offset|base64     (<= 120 bytes)
 *   RA|event_id|size|crc32  -> RK|event_id
 *   any failure             -> RX|code|text  (nf, bad, io; text starts with the id when known)
 *
 * crc32 is binascii.crc32 of the whole record as an unsigned decimal. */

#include "rf_store.h"

#define RF_PROTO_LINE_MAX 243U /* one BLE notification including '\n' */
#define RF_PROTO_CHUNK    120U /* record bytes per RD line */

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
