#pragma once

/* Recognises what a recorded OOK burst is: the common fixed-code and rolling-code remote
 * protocols (Princeton/EV1527, CAME, Nice FLO, Nice FloR-S, KeeLoq, Starline, Linear,
 * Hormann HSM, GateTX, FAAC SLH, Holtek, Nexus-TH sensors) from the pulse timings alone,
 * or, failing that, describes the burst as a generic OOK frame (base pulse, symbol count,
 * how often the frame repeats and whether the repeats are identical) or as a bare carrier.
 *
 * Pure functions over the packed rf_capture timings: no HAL, no heap, small stack frames
 * (the engine thread has 4 KiB).  The PC companion carries a line-by-line Python port
 * (uplink/uplink/rf_decode.py); keep the two in step. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RF_DECODE_NAME_MAX 16
#define RF_DECODE_INFO_MAX 32

typedef enum {
    RfProtoNone = 0, /* fewer than a handful of edges: a carrier without OOK data */
    RfProtoOok, /* OOK frames of an unknown protocol (the generic description) */
    RfProtoPrinceton,
    RfProtoCame, /* 12/18/24/25 bits; the 12-bit frame is the same as Holtek HT12E */
    RfProtoNiceFlo,
    RfProtoNiceFlorS,
    RfProtoKeeloq,
    RfProtoStarline,
    RfProtoLinear,
    RfProtoHormann,
    RfProtoGateTx,
    RfProtoFaacSlh,
    RfProtoHoltek,
    RfProtoNexus,
    RfProtoCount,
} RfProtoId;

typedef struct {
    uint8_t protocol; /* RfProtoId */
    char name[RF_DECODE_NAME_MAX]; /* "KeeLoq", "Princeton", "OOK", "carrier" */
    char info[RF_DECODE_INFO_MAX]; /* "sn 0ABCDEF btn 2", "23.4C 45% ch1", "te 420us 25sym x4" */
    uint8_t bits; /* bits of one frame (named protocols) or symbols (generic OOK) */
    uint64_t key; /* the frame's bits, first received bit = most significant */
    uint8_t frames; /* frames found in the burst */
    uint8_t identical; /* frames equal to the first one (frames - 1 = a fixed code) */
    uint32_t te_us; /* base pulse width */
    bool rolling; /* the code changes with every press (KeeLoq, Starline, FloR-S, FAAC) */
    uint8_t confidence; /* 0..100 */
} RfDecode;

/* Decode the packed timings (bit 31 = level, bits 0..30 = microseconds). */
void rf_decode(const uint32_t* timings, uint32_t count, RfDecode* out);

/* What identifies the transmitter across presses: the serial of a rolling remote, the
 * whole key of a fixed one, the id of a sensor; 0 when the protocol has no stable part. */
uint64_t rf_decode_identity(const RfDecode* decode);

/* "KeeLoq 66b" style label for the Flipper screen (at most `size` - 1 characters). */
void rf_decode_label(const RfDecode* decode, char* out, size_t size);
