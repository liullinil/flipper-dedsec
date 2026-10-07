#include "rf_store.h"

#include <furi_hal_random.h>
#include <string.h>

/* Storage notes for this firmware (Unleashed 093, API 88.9):
 * - storage_common_rename() is "remove destination, copy, remove source", so
 *   it is neither atomic nor cheap.  The journal therefore never renames a
 *   record: a record is written once under its final name, and an intent
 *   marker ("inflight") names the record being written or moved so that a
 *   reset in the middle can be repaired at the next start.
 * - FAT directory lookups are linear and event file names are long, so every
 *   open/stat costs a directory scan.  Reads of the record being synced reuse
 *   one cached handle instead of reopening it for every chunk. */

#define RF_STORE_IO_SIZE     512U
#define RF_STORE_PATH_SIZE   128U
#define RF_STORE_NAME_SIZE   128U
#define RF_STORE_RECENT      8U
#define RF_STORE_BATCH       8U
#define RF_STORE_MARKER_SIZE 64U
#define RF_STORE_LOW_PRUNE   16U

typedef struct {
    char id[RF_STORE_ID_MAX + 1];
    uint32_t size;
    uint32_t crc;
    bool used;
} RfStoreRecent;

struct RfStore {
    Storage* storage;
    File* cache; /* read handle of cache_id in events/ */
    bool cache_open;
    char cache_id[RF_STORE_ID_MAX + 1];
    uint32_t cache_size;
    uint32_t pending;
    uint32_t uploaded;
    uint32_t free_kb;
    bool free_known;
    char device_id[17];
    uint8_t io[RF_STORE_IO_SIZE];
    char path[RF_STORE_PATH_SIZE];
    char path2[RF_STORE_PATH_SIZE];
    char name[RF_STORE_NAME_SIZE];
    char batch[RF_STORE_BATCH][RF_STORE_ID_MAX + 1];
    RfStoreRecent recent[RF_STORE_RECENT];
    uint8_t recent_next;
};

/* ------------------------------------------------------------------ helpers */

static const uint32_t rf_crc_nibble[16] = {
    0x00000000U,
    0x1DB71064U,
    0x3B6E20C8U,
    0x26D930ACU,
    0x76DC4190U,
    0x6B6B51F4U,
    0x4DB26158U,
    0x5005713CU,
    0xEDB88320U,
    0xF00F9344U,
    0xD6D6A3E8U,
    0xCB61B38CU,
    0x9B64C2B0U,
    0x86D3D2D4U,
    0xA00AE278U,
    0xBDBDF21CU,
};

uint32_t rf_store_crc32(uint32_t crc, const void* data, size_t length) {
    const uint8_t* p = data;
    crc = ~crc;
    while(length--) {
        crc ^= *p++;
        crc = (crc >> 4) ^ rf_crc_nibble[crc & 15U];
        crc = (crc >> 4) ^ rf_crc_nibble[crc & 15U];
    }
    return ~crc;
}

bool rf_store_valid_id(const char* id) {
    if(!id) return false;
    size_t n = 0;
    for(; id[n]; n++) {
        if(n >= RF_STORE_ID_MAX) return false;
        char c = id[n];
        bool alnum = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
        if(!alnum && (n == 0 || (c != '_' && c != '.' && c != '-'))) return false;
    }
    return n > 0;
}

static bool rf_store_path(char* out, const char* dir, const char* id, const char* suffix) {
    int n = snprintf(out, RF_STORE_PATH_SIZE, "%s/%s%s", dir, id, suffix);
    return n > 0 && (size_t)n < RF_STORE_PATH_SIZE;
}

/* "<id>.json" -> id; anything else (".bad", temporary names, folders) is not a record. */
static bool rf_store_name_to_id(const char* name, char* id) {
    size_t n = strlen(name);
    if(n <= 5 || n - 5 > RF_STORE_ID_MAX || n >= RF_STORE_NAME_SIZE - 1) return false;
    if(strcmp(name + n - 5, ".json") != 0) return false;
    memcpy(id, name, n - 5);
    id[n - 5] = '\0';
    return rf_store_valid_id(id);
}

static void rf_store_file_done(File* file) {
    if(storage_file_is_open(file)) storage_file_close(file);
    storage_file_free(file);
}

static void rf_store_cache_close(RfStore* store) {
    if(store->cache_open) storage_file_close(store->cache);
    store->cache_open = false;
    store->cache_id[0] = '\0';
    store->cache_size = 0;
}

static RfStoreResult rf_store_cache_open(RfStore* store, const char* id) {
    if(store->cache_open && strcmp(store->cache_id, id) == 0) return RfStoreOk;
    rf_store_cache_close(store);
    if(!rf_store_path(store->path, RF_STORE_EVENTS_DIR, id, ".json")) return RfStoreErrInvalid;
    if(!storage_file_open(store->cache, store->path, FSAM_READ, FSOM_OPEN_EXISTING)) {
        FS_Error error = storage_file_get_error(store->cache);
        if(storage_file_is_open(store->cache)) storage_file_close(store->cache);
        return error == FSE_NOT_EXIST ? RfStoreErrNotFound : RfStoreErrIo;
    }
    uint64_t size = storage_file_size(store->cache);
    store->cache_open = true;
    store->cache_size = size > UINT32_MAX ? UINT32_MAX : (uint32_t)size;
    strncpy(store->cache_id, id, RF_STORE_ID_MAX);
    store->cache_id[RF_STORE_ID_MAX] = '\0';
    return RfStoreOk;
}

/* Stream a whole open file through CRC-32 and check that it looks like one
 * complete record: '{' ... "}\n". */
static RfStoreResult rf_store_digest(
    RfStore* store,
    File* file,
    uint32_t size,
    uint32_t* crc,
    bool* complete) {
    *crc = 0;
    *complete = false;
    if(size > RF_STORE_RECORD_MAX) return RfStoreOk;
    if(!storage_file_seek(file, 0, true)) return RfStoreErrIo;
    uint32_t value = 0, remaining = size;
    uint8_t first = 0, tail[2] = {0, 0};
    bool start = true;
    while(remaining) {
        size_t chunk = remaining < RF_STORE_IO_SIZE ? remaining : RF_STORE_IO_SIZE;
        if(storage_file_read(file, store->io, chunk) != chunk) return RfStoreErrIo;
        if(start) first = store->io[0];
        start = false;
        if(chunk >= 2) {
            tail[0] = store->io[chunk - 2];
            tail[1] = store->io[chunk - 1];
        } else {
            tail[0] = tail[1];
            tail[1] = store->io[0];
        }
        value = rf_store_crc32(value, store->io, chunk);
        remaining -= (uint32_t)chunk;
    }
    *crc = value;
    *complete = size >= 2 && first == '{' && tail[0] == '}' && tail[1] == '\n';
    return RfStoreOk;
}

static void rf_store_remember(RfStore* store, const char* id, uint32_t size, uint32_t crc) {
    RfStoreRecent* slot = &store->recent[store->recent_next];
    store->recent_next = (uint8_t)((store->recent_next + 1U) % RF_STORE_RECENT);
    strncpy(slot->id, id, RF_STORE_ID_MAX);
    slot->id[RF_STORE_ID_MAX] = '\0';
    slot->size = size;
    slot->crc = crc;
    slot->used = true;
}

/* Fixed-size marker overwritten in place: "<op> <id>\n" padded with spaces.
 * A torn marker has no newline (or names an intact record) and is ignored. */
static bool rf_store_mark(RfStore* store, char op, const char* id) {
    char* text = store->name;
    int n = snprintf(text, RF_STORE_MARKER_SIZE + 1, "%c %s\n", op, id);
    if(n <= 0 || n > (int)RF_STORE_MARKER_SIZE) return false;
    memset(text + n, ' ', RF_STORE_MARKER_SIZE - (size_t)n);
    File* file = storage_file_alloc(store->storage);
    bool ok = storage_file_open(file, RF_STORE_INFLIGHT, FSAM_WRITE, FSOM_OPEN_ALWAYS) &&
              storage_file_seek(file, 0, true) &&
              storage_file_write(file, text, RF_STORE_MARKER_SIZE) == RF_STORE_MARKER_SIZE &&
              storage_file_sync(file);
    rf_store_file_done(file);
    return ok;
}

static uint32_t rf_store_count(RfStore* store, const char* dir_path) {
    uint32_t count = 0;
    char id[RF_STORE_ID_MAX + 1];
    File* dir = storage_file_alloc(store->storage);
    if(storage_dir_open(dir, dir_path)) {
        FileInfo info;
        while(storage_dir_read(dir, &info, store->name, sizeof(store->name))) {
            if(info.flags & FSF_DIRECTORY) continue;
            if(rf_store_name_to_id(store->name, id)) count++;
        }
        storage_dir_close(dir);
    }
    storage_file_free(dir);
    return count;
}

/* Delete uploaded/ copies (directory order, roughly oldest first) until at
 * most `target` remain.  They are already durable on the PC. */
static void rf_store_prune_uploaded(RfStore* store, uint32_t target) {
    for(uint32_t round = 0; store->uploaded > target && round < 256U; round++) {
        uint32_t taken = 0;
        File* dir = storage_file_alloc(store->storage);
        if(storage_dir_open(dir, RF_STORE_UPLOADED_DIR)) {
            FileInfo info;
            while(taken < RF_STORE_BATCH && taken < store->uploaded - target &&
                  storage_dir_read(dir, &info, store->name, sizeof(store->name))) {
                if(info.flags & FSF_DIRECTORY) continue;
                if(rf_store_name_to_id(store->name, store->batch[taken])) taken++;
            }
            storage_dir_close(dir);
        }
        storage_file_free(dir);
        if(!taken) {
            store->uploaded = rf_store_count(store, RF_STORE_UPLOADED_DIR);
            return;
        }
        for(uint32_t i = 0; i < taken; i++) {
            if(rf_store_path(store->path2, RF_STORE_UPLOADED_DIR, store->batch[i], ".json") &&
               storage_common_remove(store->storage, store->path2) == FSE_OK && store->uploaded) {
                store->uploaded--;
            }
        }
    }
}

static bool rf_store_space_ok(RfStore* store, size_t length) {
    /* Re-measure only near the reserve; f_getfree is cached by FatFS but
     * still costs a storage round trip. */
    uint64_t needed = (uint64_t)RF_STORE_RESERVE_BYTES + length;
    if(store->free_known && (uint64_t)store->free_kb * 1024U >= needed * 4U) return true;
    rf_store_refresh_free(store);
    return store->free_known && (uint64_t)store->free_kb * 1024U >= needed;
}

/* A reset while writing record W leaves a short file; while moving record M
 * it leaves a partial copy in uploaded/ next to the intact pending record. */
static void rf_store_recover(RfStore* store) {
    File* file = storage_file_alloc(store->storage);
    size_t n = 0;
    if(storage_file_open(file, RF_STORE_INFLIGHT, FSAM_READ, FSOM_OPEN_EXISTING)) {
        n = storage_file_read(file, store->name, RF_STORE_MARKER_SIZE);
    }
    rf_store_file_done(file);
    store->name[n < RF_STORE_NAME_SIZE ? n : RF_STORE_NAME_SIZE - 1] = '\0';
    char* newline = strchr(store->name, '\n');
    if(n < 4 || !newline || store->name[1] != ' ') return;
    *newline = '\0';
    char op = store->name[0];
    char id[RF_STORE_ID_MAX + 1];
    if(strlen(store->name + 2) > RF_STORE_ID_MAX) return;
    strcpy(id, store->name + 2);
    if(!rf_store_valid_id(id) || !rf_store_path(store->path, RF_STORE_EVENTS_DIR, id, ".json")) {
        return;
    }
    if(op == 'W') {
        File* record = storage_file_alloc(store->storage);
        bool exists = storage_file_open(record, store->path, FSAM_READ, FSOM_OPEN_EXISTING);
        bool complete = false;
        if(exists) {
            uint64_t size = storage_file_size(record);
            uint8_t edge[2] = {0, 0};
            if(size >= 2 && size <= RF_STORE_RECORD_MAX &&
               storage_file_read(record, edge, 1) == 1 && edge[0] == '{' &&
               storage_file_seek(record, (uint32_t)size - 2U, true) &&
               storage_file_read(record, edge, 2) == 2) {
                complete = edge[0] == '}' && edge[1] == '\n';
            }
        }
        rf_store_file_done(record);
        if(exists && !complete) {
            FURI_LOG_W("RfStore", "dropping torn record %s", id);
            storage_common_remove(store->storage, store->path);
        }
    } else if(op == 'M') {
        if(storage_common_exists(store->storage, store->path) &&
           rf_store_path(store->path2, RF_STORE_UPLOADED_DIR, id, ".json")) {
            storage_common_remove(store->storage, store->path2);
        }
    }
}

static void rf_store_load_device_id(RfStore* store) {
    char text[17] = {0};
    File* file = storage_file_alloc(store->storage);
    size_t n = 0;
    if(storage_file_open(file, RF_STORE_DEVICE_ID, FSAM_READ, FSOM_OPEN_EXISTING)) {
        n = storage_file_read(file, text, 16);
    }
    rf_store_file_done(file);
    bool valid = n == 16;
    for(size_t i = 0; valid && i < 16; i++) {
        char c = text[i];
        valid = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    }
    if(valid) {
        memcpy(store->device_id, text, 17);
        return;
    }
    static const char hex[] = "0123456789abcdef";
    uint8_t bytes[8];
    furi_hal_random_fill_buf(bytes, sizeof(bytes));
    for(size_t i = 0; i < 8; i++) {
        store->device_id[i * 2] = hex[bytes[i] >> 4];
        store->device_id[i * 2 + 1] = hex[bytes[i] & 15U];
    }
    store->device_id[16] = '\0';
    file = storage_file_alloc(store->storage);
    if(storage_file_open(file, RF_STORE_DEVICE_ID, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        storage_file_write(file, store->device_id, 16);
        storage_file_sync(file);
    }
    rf_store_file_done(file);
}

/* Move a malformed record out of events/ so it cannot block every sync round. */
static bool rf_store_quarantine(RfStore* store, const char* id) {
    rf_store_cache_close(store);
    if(!rf_store_path(store->path, RF_STORE_EVENTS_DIR, id, ".json") ||
       !rf_store_path(store->path2, RF_STORE_EVENTS_DIR, id, ".bad")) {
        return false;
    }
    FURI_LOG_W("RfStore", "quarantining malformed record %s", id);
    if(storage_common_rename(store->storage, store->path, store->path2) != FSE_OK) return false;
    if(store->pending) store->pending--;
    return true;
}

static RfStoreResult rf_store_copy_to_uploaded(
    RfStore* store,
    const char* source,
    const char* id,
    uint32_t size,
    bool* fresh) {
    *fresh = true;
    if(!rf_store_path(store->path2, RF_STORE_UPLOADED_DIR, id, ".json")) return RfStoreErrInvalid;
    File* src = storage_file_alloc(store->storage);
    File* dst = storage_file_alloc(store->storage);
    bool ok = storage_file_open(src, source, FSAM_READ, FSOM_OPEN_EXISTING);
    if(ok && !storage_file_open(dst, store->path2, FSAM_WRITE, FSOM_CREATE_NEW)) {
        /* A copy from an interrupted earlier ACK: replace it. */
        *fresh = false;
        if(storage_file_is_open(dst)) storage_file_close(dst);
        ok = storage_file_open(dst, store->path2, FSAM_WRITE, FSOM_CREATE_ALWAYS);
    }
    uint32_t copied = 0;
    while(ok && copied < size) {
        size_t chunk = size - copied < RF_STORE_IO_SIZE ? size - copied : RF_STORE_IO_SIZE;
        ok = storage_file_read(src, store->io, chunk) == chunk &&
             storage_file_write(dst, store->io, chunk) == chunk;
        copied += (uint32_t)chunk;
    }
    if(ok) ok = storage_file_sync(dst);
    bool dst_open = storage_file_is_open(dst);
    rf_store_file_done(src);
    rf_store_file_done(dst);
    if(!ok) {
        if(dst_open) storage_common_remove(store->storage, store->path2);
        return RfStoreErrIo;
    }
    return RfStoreOk;
}

/* An ACK for a record that is no longer pending: accept it when it matches
 * a recent ACK or a kept copy, so a lost RK reply can simply be retried. */
static RfStoreResult
    rf_store_ack_repeat(RfStore* store, const char* id, uint32_t size, uint32_t crc) {
    for(size_t i = 0; i < RF_STORE_RECENT; i++) {
        const RfStoreRecent* slot = &store->recent[i];
        if(slot->used && strcmp(slot->id, id) == 0) {
            return slot->size == size && slot->crc == crc ? RfStoreOk : RfStoreErrMismatch;
        }
    }
    if(!rf_store_path(store->path2, RF_STORE_UPLOADED_DIR, id, ".json")) return RfStoreErrInvalid;
    File* file = storage_file_alloc(store->storage);
    RfStoreResult result = RfStoreErrNotFound;
    if(storage_file_open(file, store->path2, FSAM_READ, FSOM_OPEN_EXISTING)) {
        uint64_t length = storage_file_size(file);
        uint32_t actual = 0;
        bool complete = false;
        result = length > RF_STORE_RECORD_MAX ?
                     RfStoreErrMismatch :
                     rf_store_digest(store, file, (uint32_t)length, &actual, &complete);
        if(result == RfStoreOk && (length != size || actual != crc)) result = RfStoreErrMismatch;
    }
    rf_store_file_done(file);
    return result;
}

/* ------------------------------------------------------------------ public */

RfStore* rf_store_alloc(Storage* storage) {
    RfStore* store = malloc(sizeof(RfStore));
    memset(store, 0, sizeof(RfStore));
    store->storage = storage;
    store->cache = storage_file_alloc(storage);
    return store;
}

void rf_store_free(RfStore* store) {
    if(!store) return;
    rf_store_cache_close(store);
    storage_file_free(store->cache);
    free(store);
}

void rf_store_open(RfStore* store) {
    storage_common_mkdir(store->storage, EXT_PATH("apps_data"));
    storage_common_mkdir(store->storage, EXT_PATH("apps_data/dedsec_uplink"));
    storage_common_mkdir(store->storage, RF_STORE_ROOT);
    storage_common_mkdir(store->storage, RF_STORE_EVENTS_DIR);
    storage_common_mkdir(store->storage, RF_STORE_UPLOADED_DIR);
    rf_store_load_device_id(store);
    rf_store_recover(store);
    store->pending = rf_store_count(store, RF_STORE_EVENTS_DIR);
    store->uploaded = rf_store_count(store, RF_STORE_UPLOADED_DIR);
    if(store->uploaded > RF_STORE_UPLOADED_MAX) {
        rf_store_prune_uploaded(store, RF_STORE_UPLOADED_MAX);
    }
    rf_store_refresh_free(store);
}

void rf_store_idle(RfStore* store) {
    rf_store_cache_close(store);
}

const char* rf_store_device_id(const RfStore* store) {
    return store->device_id;
}

uint32_t rf_store_pending(const RfStore* store) {
    return store->pending;
}

uint32_t rf_store_stored(const RfStore* store) {
    return store->pending + store->uploaded;
}

uint32_t rf_store_free_kb(const RfStore* store) {
    return store->free_kb;
}

bool rf_store_refresh_free(RfStore* store) {
    uint64_t total = 0, free_bytes = 0;
    if(storage_common_fs_info(store->storage, EXT_PATH(""), &total, &free_bytes) != FSE_OK) {
        store->free_kb = 0;
        store->free_known = false;
        return false;
    }
    uint64_t kb = free_bytes / 1024U;
    store->free_kb = kb > UINT32_MAX ? UINT32_MAX : (uint32_t)kb;
    store->free_known = true;
    return free_bytes >= RF_STORE_RESERVE_BYTES;
}

RfStoreResult rf_store_save(RfStore* store, const char* id, const char* data, size_t length) {
    if(!rf_store_valid_id(id) || !data || length < 2 || length > RF_STORE_RECORD_MAX ||
       data[0] != '{' || data[length - 2] != '}' || data[length - 1] != '\n') {
        return RfStoreErrInvalid;
    }
    if(!rf_store_space_ok(store, length)) {
        /* Uploaded copies are already safe on the PC: they go first. */
        if(store->uploaded) {
            rf_store_prune_uploaded(
                store, store->uploaded > RF_STORE_LOW_PRUNE ? store->uploaded - RF_STORE_LOW_PRUNE : 0);
        }
        if(!rf_store_space_ok(store, length)) return RfStoreErrLowSpace;
    }
    if(!rf_store_path(store->path, RF_STORE_EVENTS_DIR, id, ".json")) return RfStoreErrInvalid;
    if(!rf_store_mark(store, 'W', id)) return RfStoreErrIo;
    File* file = storage_file_alloc(store->storage);
    /* CREATE_NEW: an event id is immutable and never replaces evidence. */
    bool created = storage_file_open(file, store->path, FSAM_WRITE, FSOM_CREATE_NEW);
    bool exists = !created && storage_file_get_error(file) == FSE_EXIST;
    bool ok = created && storage_file_write(file, data, length) == length &&
              storage_file_sync(file);
    rf_store_file_done(file);
    if(exists) return RfStoreErrExists;
    if(!ok) {
        if(created) storage_common_remove(store->storage, store->path);
        return RfStoreErrIo;
    }
    store->pending++;
    uint32_t used_kb = (uint32_t)((length + 1023U) / 1024U);
    store->free_kb = store->free_kb > used_kb ? store->free_kb - used_kb : 0;
    return RfStoreOk;
}

RfStoreResult rf_store_list(
    RfStore* store,
    uint32_t cursor,
    char* id,
    size_t id_size,
    uint32_t* size,
    uint32_t* crc32) {
    if(!id || id_size < RF_STORE_ID_MAX + 1) return RfStoreErrInvalid;
    for(uint8_t attempt = 0; attempt < 8; attempt++) {
        rf_store_cache_close(store);
        bool found = false;
        File* dir = storage_file_alloc(store->storage);
        bool opened = storage_dir_open(dir, RF_STORE_EVENTS_DIR);
        if(opened) {
            FileInfo info;
            uint32_t index = 0;
            while(storage_dir_read(dir, &info, store->name, sizeof(store->name))) {
                if(info.flags & FSF_DIRECTORY) continue;
                if(!rf_store_name_to_id(store->name, id)) continue;
                if(index++ == cursor) {
                    found = true;
                    break;
                }
            }
            storage_dir_close(dir);
        }
        storage_file_free(dir);
        if(!opened) return RfStoreErrIo;
        if(!found) return RfStoreErrNotFound;
        RfStoreResult result = rf_store_cache_open(store, id);
        if(result != RfStoreOk) return result;
        bool complete = false;
        result = rf_store_digest(store, store->cache, store->cache_size, crc32, &complete);
        if(result != RfStoreOk) {
            rf_store_cache_close(store);
            return result;
        }
        if(complete) {
            *size = store->cache_size;
            return RfStoreOk;
        }
        if(!rf_store_quarantine(store, id)) return RfStoreErrIo;
        /* The next record now has this directory index. */
    }
    return RfStoreErrIo;
}

RfStoreResult rf_store_read(
    RfStore* store,
    const char* id,
    uint32_t offset,
    uint8_t* out,
    size_t capacity,
    size_t* got,
    uint32_t* size) {
    *got = 0;
    if(!rf_store_valid_id(id) || !out) return RfStoreErrInvalid;
    RfStoreResult result = rf_store_cache_open(store, id);
    if(result != RfStoreOk) return result;
    if(size) *size = store->cache_size;
    if(store->cache_size > RF_STORE_RECORD_MAX) return RfStoreErrIo;
    if(offset > store->cache_size) return RfStoreErrInvalid;
    size_t want = store->cache_size - offset;
    if(want > capacity) want = capacity;
    if(!want) return RfStoreOk;
    if(!storage_file_seek(store->cache, offset, true) ||
       storage_file_read(store->cache, out, want) != want) {
        rf_store_cache_close(store);
        return RfStoreErrIo;
    }
    *got = want;
    return RfStoreOk;
}

RfStoreResult
    rf_store_ack(RfStore* store, const char* id, uint32_t size, uint32_t crc32, bool keep) {
    if(!rf_store_valid_id(id)) return RfStoreErrInvalid;
    RfStoreResult result = rf_store_cache_open(store, id);
    if(result == RfStoreErrNotFound) return rf_store_ack_repeat(store, id, size, crc32);
    if(result != RfStoreOk) return result;
    uint32_t actual_size = store->cache_size, actual_crc = 0;
    bool complete = false;
    result = rf_store_digest(store, store->cache, actual_size, &actual_crc, &complete);
    rf_store_cache_close(store); /* a file cannot be removed while open */
    if(result != RfStoreOk) return result;
    if(actual_size != size || actual_crc != crc32 || actual_size > RF_STORE_RECORD_MAX) {
        return RfStoreErrMismatch;
    }
    if(!rf_store_path(store->path, RF_STORE_EVENTS_DIR, id, ".json")) return RfStoreErrInvalid;
    bool fresh = false;
    if(keep) {
        if(!rf_store_mark(store, 'M', id)) return RfStoreErrIo;
        result = rf_store_copy_to_uploaded(store, store->path, id, actual_size, &fresh);
        if(result != RfStoreOk) return result;
        if(fresh) store->uploaded++;
    }
    if(storage_common_remove(store->storage, store->path) != FSE_OK) {
        /* Still pending; a kept copy is replaced by the next ACK attempt. */
        return RfStoreErrIo;
    }
    if(store->pending) store->pending--;
    rf_store_remember(store, id, size, crc32);
    if(store->uploaded > RF_STORE_UPLOADED_MAX) {
        rf_store_prune_uploaded(store, RF_STORE_UPLOADED_MAX);
    }
    return RfStoreOk;
}
