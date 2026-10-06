#include "rf_store.h"

#include <furi_hal_random.h>
#include <string.h>

struct RfStore {
    Storage* storage;
    char device_id[33];
};

static void hex_random(char* out) {
    uint8_t bytes[16];
    static const char hex[] = "0123456789abcdef";
    furi_hal_random_fill_buf(bytes, sizeof(bytes));
    for(size_t i = 0; i < sizeof(bytes); i++) {
        out[i * 2] = hex[bytes[i] >> 4];
        out[i * 2 + 1] = hex[bytes[i] & 15];
    }
    out[32] = 0;
}

static bool read_small(Storage* storage, const char* path, char* out, size_t cap) {
    File* file = storage_file_alloc(storage);
    bool ok = false;
    if(storage_file_open(file, path, FSAM_READ, FSOM_OPEN_EXISTING)) {
        size_t n = storage_file_read(file, out, cap - 1);
        out[n] = 0;
        ok = n > 0;
        storage_file_close(file);
    }
    storage_file_free(file);
    return ok;
}

static bool write_small(Storage* storage, const char* path, const char* text) {
    File* file = storage_file_alloc(storage);
    bool ok = false;
    if(storage_file_open(file, path, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        ok = storage_file_write(file, text, strlen(text)) == strlen(text) && storage_file_sync(file);
        storage_file_close(file);
    }
    storage_file_free(file);
    return ok;
}

static void event_path(char* out, size_t cap, const char* dir, const char* event_id, const char* suffix) {
    snprintf(out, cap, "%s/%s%s", dir, event_id, suffix);
}

RfStore* rf_store_alloc(Storage* storage) {
    RfStore* store = malloc(sizeof(RfStore));
    memset(store, 0, sizeof(RfStore));
    store->storage = storage;
    storage_common_mkdir(storage, RF_STORE_DIR);
    storage_common_mkdir(storage, RF_STORE_EVENTS_DIR);
    storage_common_mkdir(storage, RF_STORE_RECEIPTS_DIR);
    if(!read_small(storage, RF_STORE_DEVICE_ID, store->device_id, sizeof(store->device_id))) {
        hex_random(store->device_id);
        write_small(storage, RF_STORE_DEVICE_ID, store->device_id);
    }
    return store;
}

void rf_store_free(RfStore* store) {
    free(store);
}

const char* rf_store_device_id(const RfStore* store) {
    return store ? store->device_id : "";
}

uint64_t rf_store_free_bytes(RfStore* store) {
    uint64_t total = 0, free_bytes = 0;
    if(store) storage_common_fs_info(store->storage, EXT_PATH(""), &total, &free_bytes);
    return free_bytes;
}

static bool is_json_file(const char* name) {
    size_t n = strlen(name);
    return n > 5 && !strcmp(name + n - 5, ".json");
}

uint32_t rf_store_pending_count(RfStore* store) {
    if(!store) return 0;
    uint32_t count = 0;
    // The storage API exposes a directory iterator; count files ending in .json.
    File* dir = storage_file_alloc(store->storage);
    if(storage_dir_open(dir, RF_STORE_EVENTS_DIR)) {
        char name[128];
        FileInfo info;
        while(storage_dir_read(dir, &info, name, sizeof(name))) if(is_json_file(name)) count++;
        storage_dir_close(dir);
    }
    storage_file_free(dir);
    return count;
}

bool rf_store_save(RfStore* store, const char* event_id, const char* json, size_t len) {
    if(!store || !event_id || !json || !len) return false;
    char tmp[160], final[160];
    event_path(tmp, sizeof(tmp), RF_STORE_EVENTS_DIR, event_id, ".json.part");
    event_path(final, sizeof(final), RF_STORE_EVENTS_DIR, event_id, ".json");
    File* file = storage_file_alloc(store->storage);
    bool ok = false;
    if(storage_file_open(file, tmp, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        ok = storage_file_write(file, json, len) == len && storage_file_sync(file);
        storage_file_close(file);
    }
    storage_file_free(file);
    if(!ok) return false;
    storage_common_remove(store->storage, final);
    return storage_common_rename(store->storage, tmp, final) == FSE_OK;
}

bool rf_store_read(RfStore* store, const char* event_id, char* out, size_t cap, size_t* used) {
    if(used) *used = 0;
    if(!store || !event_id || !out || cap < 2) return false;
    char path[160];
    event_path(path, sizeof(path), RF_STORE_EVENTS_DIR, event_id, ".json");
    File* file = storage_file_alloc(store->storage);
    bool ok = false;
    if(storage_file_open(file, path, FSAM_READ, FSOM_OPEN_EXISTING)) {
        size_t n = storage_file_read(file, out, cap - 1);
        out[n] = 0;
        if(used) *used = n;
        ok = n > 0;
        storage_file_close(file);
    }
    storage_file_free(file);
    return ok;
}

bool rf_store_is_acked(RfStore* store, const char* event_id) {
    if(!store || !event_id) return false;
    char path[160];
    event_path(path, sizeof(path), RF_STORE_RECEIPTS_DIR, event_id, ".ack");
    FileInfo info;
    return storage_common_stat(store->storage, path, &info) == FSE_OK;
}

bool rf_store_ack(RfStore* store, const char* event_id) {
    if(!store || !event_id) return false;
    char path[160], receipt[160];
    event_path(path, sizeof(path), RF_STORE_EVENTS_DIR, event_id, ".json");
    event_path(receipt, sizeof(receipt), RF_STORE_RECEIPTS_DIR, event_id, ".ack");
    if(rf_store_is_acked(store, event_id)) return true;
    File* file = storage_file_alloc(store->storage);
    bool ok = storage_file_open(file, receipt, FSAM_WRITE, FSOM_CREATE_ALWAYS);
    if(ok) {
        storage_file_sync(file);
        storage_file_close(file);
    }
    storage_file_free(file);
    if(!ok) return false;
    storage_common_remove(store->storage, path);
    return true;
}
