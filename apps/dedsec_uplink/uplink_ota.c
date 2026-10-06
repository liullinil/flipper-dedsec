#include "uplink_ota.h"

#include <loader/loader.h>
#include <toolbox/crc32_calc.h>
#include <stdlib.h>
#include <string.h>

#define TAG          "UplinkOta"
#define DEFAULT_PATH EXT_PATH("apps/Bluetooth/dedsec_uplink.fap")

void ota_init(Ota* ota) {
    memset(ota, 0, sizeof(Ota));
    ota->storage = furi_record_open(RECORD_STORAGE);
    FuriString* path = furi_string_alloc();
    Loader* loader = furi_record_open(RECORD_LOADER);
    bool ok = loader_get_application_launch_path(loader, path) &&
              furi_string_end_with_str(path, ".fap");
    furi_record_close(RECORD_LOADER);
    strlcpy(ota->self_path, ok ? furi_string_get_cstr(path) : DEFAULT_PATH, sizeof(ota->self_path));
    snprintf(ota->tmp_path, sizeof(ota->tmp_path), "%s.new", ota->self_path);
    furi_string_free(path);
    FURI_LOG_I(TAG, "self %s", ota->self_path);
}

static void ota_close(Ota* ota) {
    if(ota->file) {
        storage_file_close(ota->file);
        storage_file_free(ota->file);
        ota->file = NULL;
    }
}

void ota_abort(Ota* ota, const char* why) {
    ota_close(ota);
    storage_common_remove(ota->storage, ota->tmp_path);
    strlcpy(ota->error, why, sizeof(ota->error));
    ota->state = OtaFailed;
    FURI_LOG_W(TAG, "abort: %s", why);
}

void ota_free(Ota* ota) {
    if(ota->state == OtaReceiving) ota_abort(ota, "app closed");
    ota_close(ota);
    furi_record_close(RECORD_STORAGE);
}

static void parse_version(const char* s, int v[3]) {
    v[0] = v[1] = v[2] = 0;
    while(*s && (*s < '0' || *s > '9'))
        s++; // skip a leading "v"
    for(int i = 0; i < 3 && *s; i++) {
        v[i] = atoi(s);
        while(*s >= '0' && *s <= '9')
            s++;
        if(*s == '.') s++;
    }
}

bool ota_newer(const char* tag, const char* mine) {
    int a[3], b[3];
    parse_version(tag, a);
    parse_version(mine, b);
    for(int i = 0; i < 3; i++) {
        if(a[i] != b[i]) return a[i] > b[i];
    }
    return false;
}

bool ota_begin(Ota* ota, const char* tag, uint32_t size, uint32_t crc) {
    ota_close(ota);
    if(size == 0 || size > 512 * 1024) {
        ota_abort(ota, "bad size");
        return false;
    }
    ota->file = storage_file_alloc(ota->storage);
    if(!storage_file_open(ota->file, ota->tmp_path, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        ota_abort(ota, "cannot write SD");
        return false;
    }
    strlcpy(ota->tag, tag, sizeof(ota->tag));
    ota->size = size;
    ota->crc_expect = crc;
    ota->crc = 0;
    ota->written = 0;
    ota->error[0] = 0;
    ota->state = OtaReceiving;
    return true;
}

static int b64_value(char ch) {
    if(ch >= 'A' && ch <= 'Z') return ch - 'A';
    if(ch >= 'a' && ch <= 'z') return ch - 'a' + 26;
    if(ch >= '0' && ch <= '9') return ch - '0' + 52;
    if(ch == '+') return 62;
    if(ch == '/') return 63;
    return -1;
}

/* decodes into out (capacity cap); returns byte count or -1 */
static int b64_decode(const char* in, uint8_t* out, size_t cap) {
    uint32_t acc = 0;
    int bits = 0;
    size_t n = 0;
    for(; *in; in++) {
        if(*in == '=') break;
        int v = b64_value(*in);
        if(v < 0) return -1;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if(bits >= 8) {
            bits -= 8;
            if(n >= cap) return -1;
            out[n++] = (uint8_t)(acc >> bits);
        }
    }
    return (int)n;
}

int32_t ota_chunk(Ota* ota, uint32_t offset, const char* b64) {
    if(ota->state != OtaReceiving || !ota->file) return -1;
    if(offset != ota->written) return (int32_t)ota->written; // duplicate or gap: resync
    uint8_t buf[320];
    int n = b64_decode(b64, buf, sizeof(buf));
    if(n <= 0 || ota->written + (uint32_t)n > ota->size) {
        ota_abort(ota, "bad chunk");
        return -1;
    }
    if(storage_file_write(ota->file, buf, n) != (size_t)n) {
        ota_abort(ota, "SD write failed");
        return -1;
    }
    ota->crc = crc32_calc_buffer(ota->crc, buf, n);
    ota->written += n;
    return (int32_t)ota->written;
}

bool ota_finish(Ota* ota) {
    if(ota->state != OtaReceiving) return false;
    storage_file_sync(ota->file);
    ota_close(ota);
    if(ota->written != ota->size) {
        ota_abort(ota, "size mismatch");
        return false;
    }
    if(ota->crc != ota->crc_expect) {
        ota_abort(ota, "CRC mismatch");
        return false;
    }
    // swap: the running copy is already in RAM, so replacing the file is safe
    storage_common_remove(ota->storage, ota->self_path);
    if(storage_common_rename(ota->storage, ota->tmp_path, ota->self_path) != FSE_OK) {
        ota_abort(ota, "rename failed");
        return false;
    }
    ota->state = OtaDone;
    FURI_LOG_I(TAG, "updated to %s", ota->tag);
    return true;
}

uint8_t ota_percent(const Ota* ota) {
    if(!ota->size) return 0;
    return (uint8_t)((uint64_t)ota->written * 100 / ota->size);
}
