#include "rf_proto.h"

#include <stdio.h>
#include <string.h>

#define RF_PROTO_FIELDS 5U

static bool rf_proto_u32(const char* text, uint32_t* value) {
    if(!text || !text[0]) return false;
    uint64_t result = 0;
    for(size_t i = 0; text[i]; i++) {
        if(i >= 10 || text[i] < '0' || text[i] > '9') return false;
        result = result * 10U + (uint64_t)(text[i] - '0');
    }
    if(result > UINT32_MAX) return false;
    *value = (uint32_t)result;
    return true;
}

/* Split on '|'; returns RF_PROTO_FIELDS + 1 when there are too many fields. */
static size_t rf_proto_split(char* line, char** fields) {
    size_t count = 0;
    char* cursor = line;
    while(true) {
        if(count == RF_PROTO_FIELDS) return RF_PROTO_FIELDS + 1U;
        fields[count++] = cursor;
        char* bar = strchr(cursor, '|');
        if(!bar) return count;
        *bar = '\0';
        cursor = bar + 1;
    }
}

static size_t rf_proto_base64(const uint8_t* data, size_t length, char* out) {
    static const char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t used = 0;
    for(size_t i = 0; i < length; i += 3) {
        uint32_t chunk = (uint32_t)data[i] << 16;
        if(i + 1 < length) chunk |= (uint32_t)data[i + 1] << 8;
        if(i + 2 < length) chunk |= data[i + 2];
        out[used++] = alphabet[(chunk >> 18) & 63U];
        out[used++] = alphabet[(chunk >> 12) & 63U];
        out[used++] = i + 1 < length ? alphabet[(chunk >> 6) & 63U] : '=';
        out[used++] = i + 2 < length ? alphabet[chunk & 63U] : '=';
    }
    out[used] = '\0';
    return used;
}

static const char* rf_proto_code(RfStoreResult result) {
    switch(result) {
    case RfStoreErrNotFound:
        return "nf";
    case RfStoreErrIo:
    case RfStoreErrLowSpace:
        return "io";
    default:
        return "bad";
    }
}

static void rf_proto_fail(
    char* out,
    const char* code,
    const char* id,
    const char* text,
    RfProtoReply reply,
    void* context) {
    if(id) {
        snprintf(out, RF_PROTO_LINE_MAX, "RX|%s|%s %s", code, id, text);
    } else {
        snprintf(out, RF_PROTO_LINE_MAX, "RX|%s|%s", code, text);
    }
    reply(out, context);
}

static void rf_proto_list(
    RfStore* store,
    char** fields,
    size_t count,
    char* out,
    RfProtoReply reply,
    void* context) {
    uint32_t cursor = 0;
    if(count != 2 || !rf_proto_u32(fields[1], &cursor) || cursor == UINT32_MAX) {
        rf_proto_fail(out, "bad", NULL, "RL needs a cursor", reply, context);
        return;
    }
    char id[RF_STORE_ID_MAX + 1];
    uint32_t size = 0, crc = 0;
    RfStoreResult result = rf_store_list(store, cursor, id, sizeof(id), &size, &crc);
    if(result == RfStoreErrNotFound) {
        reply("RE", context);
    } else if(result != RfStoreOk) {
        rf_proto_fail(out, rf_proto_code(result), NULL, "list failed", reply, context);
    } else {
        snprintf(
            out,
            RF_PROTO_LINE_MAX,
            "RI|%lu|%s|%lu|%lu",
            (unsigned long)(cursor + 1U),
            id,
            (unsigned long)size,
            (unsigned long)crc);
        reply(out, context);
    }
}

static void rf_proto_read(
    RfStore* store,
    char** fields,
    size_t count,
    char* out,
    RfProtoReply reply,
    void* context) {
    uint32_t offset = 0;
    if(count != 3 || !rf_store_valid_id(fields[1]) || !rf_proto_u32(fields[2], &offset)) {
        rf_proto_fail(out, "bad", NULL, "RR needs event_id and offset", reply, context);
        return;
    }
    const char* id = fields[1];
    uint8_t data[RF_PROTO_CHUNK];
    size_t got = 0;
    uint32_t size = 0;
    RfStoreResult result = rf_store_read(store, id, offset, data, sizeof(data), &got, &size);
    if(result == RfStoreErrNotFound) {
        rf_proto_fail(out, "nf", id, "not pending", reply, context);
    } else if(result == RfStoreErrInvalid) {
        rf_proto_fail(out, "bad", id, "offset beyond end", reply, context);
    } else if(result != RfStoreOk) {
        rf_proto_fail(out, rf_proto_code(result), id, "read failed", reply, context);
    } else {
        int head =
            snprintf(out, RF_PROTO_LINE_MAX, "RD|%s|%lu|", id, (unsigned long)offset);
        /* 3 + 48 + 1 + 10 + 1 + 160 base64 chars < 243 */
        if(head > 0 && (size_t)head + ((got + 2U) / 3U) * 4U < RF_PROTO_LINE_MAX) {
            rf_proto_base64(data, got, out + head);
            reply(out, context);
        } else {
            rf_proto_fail(out, "bad", id, "line overflow", reply, context);
        }
    }
}

static void rf_proto_ack(
    RfStore* store,
    char** fields,
    size_t count,
    bool keep_uploaded,
    char* out,
    RfProtoReply reply,
    void* context) {
    uint32_t size = 0, crc = 0;
    if(count != 4 || !rf_store_valid_id(fields[1]) || !rf_proto_u32(fields[2], &size) ||
       !rf_proto_u32(fields[3], &crc)) {
        rf_proto_fail(out, "bad", NULL, "RA needs event_id, size and crc32", reply, context);
        return;
    }
    const char* id = fields[1];
    RfStoreResult result = rf_store_ack(store, id, size, crc, keep_uploaded);
    if(result == RfStoreOk) {
        snprintf(out, RF_PROTO_LINE_MAX, "RK|%s", id);
        reply(out, context);
    } else if(result == RfStoreErrMismatch) {
        rf_proto_fail(out, "bad", id, "size/crc32 mismatch", reply, context);
    } else if(result == RfStoreErrNotFound) {
        rf_proto_fail(out, "nf", id, "not pending", reply, context);
    } else {
        rf_proto_fail(out, rf_proto_code(result), id, "ack failed", reply, context);
    }
}

void rf_proto_handle(
    RfStore* store,
    char* line,
    bool keep_uploaded,
    char* scratch,
    RfProtoReply reply,
    void* context) {
    size_t length = strlen(line);
    while(length && (line[length - 1] == '\n' || line[length - 1] == '\r')) {
        line[--length] = '\0';
    }
    char* fields[RF_PROTO_FIELDS];
    size_t count = rf_proto_split(line, fields);
    if(count > RF_PROTO_FIELDS) {
        rf_proto_fail(scratch, "bad", NULL, "too many fields", reply, context);
    } else if(strcmp(fields[0], "RL") == 0) {
        rf_proto_list(store, fields, count, scratch, reply, context);
    } else if(strcmp(fields[0], "RR") == 0) {
        rf_proto_read(store, fields, count, scratch, reply, context);
    } else if(strcmp(fields[0], "RA") == 0) {
        rf_proto_ack(store, fields, count, keep_uploaded, scratch, reply, context);
    } else {
        rf_proto_fail(scratch, "bad", NULL, "unknown request", reply, context);
    }
}
