#include "rf_store.h"

#include <furi_hal_random.h>
#include <string.h>

struct RfStore {
    Storage* storage;
    char device_id[33];
    uint8_t retention_policy;
    uint32_t min_free_bytes;
    RfStoreError last_error;
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

/* Event IDs are generated locally, but the BLE pull profile accepts an ID
 * supplied by the connected desktop.  Keep that input a filename component;
 * allowing separators here would let an unauthenticated central traverse the
 * app-data directory while asking for a read or ACK. */
static bool rf_store_valid_event_id(const char* event_id) {
    if(!event_id || !event_id[0]) return false;
    size_t len = strlen(event_id);
    if(len >= 96) return false;
    for(size_t i = 0; i < len; i++) {
        char c = event_id[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '-' || c == '_';
        if(!ok) return false;
    }
    return true;
}

static bool is_json_file(const char* name);

static bool write_exact(File* file, const char* text, size_t length) {
    return storage_file_write(file, text, length) == length;
}

static bool receipt_field(File* file, const char* record, const char* key) {
    char needle[64];
    snprintf(needle, sizeof(needle), "\"%s\":", key);
    const char* start = strstr(record, needle);
    if(!start) return true; /* Fields vary between Sub-GHz and NFC records. */
    const char* value = start + strlen(needle);
    while(*value == ' ') value++;
    const char* end = value;
    if(*value == '\"') {
        end++;
        while(*end && *end != '\"') {
            if(*end == '\\' && end[1]) end++;
            end++;
        }
        if(!*end) return true; /* A partial value is never committed. */
        end++;
    } else {
        while(*end && *end != ',' && *end != '}' && *end != '\n') end++;
        if(!*end || end == value || *value == '[' || *value == '{') return true;
    }
    return write_exact(file, ",", 1) && write_exact(file, start, (size_t)(end - start));
}

static bool write_receipt(RfStore* store, File* file, const char* event_id, const char* event_path) {
    /* Capture metadata is at the beginning of the version-1 record.  Copy
       only scalar identity/time/fingerprint fields, never pulse arrays. */
    char* record = malloc(4096);
    if(!record) return false;
    File* source = storage_file_alloc(store->storage);
    bool ok = storage_file_open(source, event_path, FSAM_READ, FSOM_OPEN_EXISTING);
    if(ok) {
        size_t length = storage_file_read(source, record, 4095);
        record[length] = 0;
        ok = length > 0;
        storage_file_close(source);
    }
    storage_file_free(source);
    char header[160];
    int header_len = snprintf(header, sizeof(header), "{\"event_id\":\"%s\",\"upload_state\":\"uploaded\",\"schema_version\":1", event_id);
    ok = ok && header_len > 0 && (size_t)header_len < sizeof(header) && write_exact(file, header, (size_t)header_len);
    static const char* fields[] = {
        "battery_pct",
        "device_uuid", "device_id", "session_id", "sequence_number", "captured_at_utc",
        "captured_at_unix", "timezone_offset_minutes", "rtc_local_unix", "monotonic_ms",
        "source_type", "frequency_hz", "modulation", "bandwidth_hz", "duration_us", "repeat_count", "fingerprint_id",
        "family_id", "profile_id", "follow_profile_id", "follow_similarity",
        "rssi_min_dbm", "rssi_avg_dbm", "rssi_max_dbm", "last_duration_us",
        "nfc_technology", "nfc_protocol", "nfc_identifier", "nfc_field_duration_ms", "nfc_field_count", "nfc_confidence",
        "classification", "classification_confidence"
    };
    for(size_t i = 0; ok && i < sizeof(fields) / sizeof(fields[0]); i++) {
        ok = receipt_field(file, record, fields[i]);
    }
    if(ok) ok = write_exact(file, "}\n", 2);
    free(record);
    return ok;
}

static void rf_store_set_error(RfStore* store, RfStoreError error) {
    if(store) store->last_error = error;
}

static void rf_store_recover(RfStore* store) {
    if(!store) return;
    /* A .part file is never a committed event.  Removing it on every launch
       makes a power loss between write and rename safe and deterministic. */
    File* dir = storage_file_alloc(store->storage);
    if(storage_dir_open(dir, RF_STORE_EVENTS_DIR)) {
        char name[128];
        FileInfo info;
        while(storage_dir_read(dir, &info, name, sizeof(name))) {
            size_t n = strlen(name);
            if(n > 5 && !strcmp(name + n - 5, ".part")) {
                char path[192];
                snprintf(path, sizeof(path), "%s/%s", RF_STORE_EVENTS_DIR, name);
                storage_common_remove(store->storage, path);
            } else if(n > 4 && !strcmp(name + n - 4, ".bak")) {
                char path[192], final[192];
                snprintf(path, sizeof(path), "%s/%s", RF_STORE_EVENTS_DIR, name);
                name[n - 4] = 0;
                snprintf(final, sizeof(final), "%s/%s", RF_STORE_EVENTS_DIR, name);
                FileInfo final_info;
                if(storage_common_stat(store->storage, final, &final_info) == FSE_OK) {
                    storage_common_remove(store->storage, path);
                } else {
                    storage_common_rename(store->storage, path, final);
                }
            }
        }
        storage_dir_close(dir);
    }
    storage_file_free(dir);

    /* If power failed after an ACK receipt was synced but before the old
       event file was reclaimed, finish the reclamation now.  The receipt is
       the durable commit marker. */
    if(store->retention_policy != 1) {
        dir = storage_file_alloc(store->storage);
        if(storage_dir_open(dir, RF_STORE_EVENTS_DIR)) {
            char name[128];
            FileInfo info;
            while(storage_dir_read(dir, &info, name, sizeof(name))) {
                size_t n = strlen(name);
                if(!is_json_file(name)) continue;
                char id[128];
                if(n - 5 >= sizeof(id)) continue;
                memcpy(id, name, n - 5);
                id[n - 5] = 0;
                if(rf_store_is_acked(store, id)) {
                    char path[192];
                    snprintf(path, sizeof(path), "%s/%s", RF_STORE_EVENTS_DIR, name);
                    storage_common_remove(store->storage, path);
                }
            }
            storage_dir_close(dir);
        }
        storage_file_free(dir);
    }
}

RfStore* rf_store_alloc(Storage* storage) {
    if(!storage) return NULL;
    RfStore* store = malloc(sizeof(RfStore));
    memset(store, 0, sizeof(RfStore));
    store->storage = storage;
    store->retention_policy = 0;
    store->min_free_bytes = 32U * 1024U;
    store->last_error = RfStoreErrorNone;
    storage_common_mkdir(storage, RF_STORE_DIR);
    storage_common_mkdir(storage, RF_STORE_EVENTS_DIR);
    storage_common_mkdir(storage, RF_STORE_RECEIPTS_DIR);
    if(!read_small(storage, RF_STORE_DEVICE_ID, store->device_id, sizeof(store->device_id))) {
        hex_random(store->device_id);
        write_small(storage, RF_STORE_DEVICE_ID, store->device_id);
    }
    /* The caller configures retention before recovery so a persisted
       Keep-Index policy cannot be accidentally reclaimed at startup. */
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

void rf_store_configure(RfStore* store, uint8_t retention_policy, uint32_t min_free_bytes) {
    if(!store) return;
    store->retention_policy = retention_policy > 2 ? 0 : retention_policy;
    store->min_free_bytes = min_free_bytes < 4096 ? 4096 : min_free_bytes;
    rf_store_recover(store);
}

uint8_t rf_store_retention_policy(const RfStore* store) {
    return store ? store->retention_policy : 0;
}

uint32_t rf_store_min_free_bytes(const RfStore* store) {
    return store ? store->min_free_bytes : 0;
}

bool rf_store_storage_low(RfStore* store, size_t required_bytes) {
    if(!store) return true;
    uint64_t free_bytes = rf_store_free_bytes(store);
    return free_bytes == 0 || free_bytes < (uint64_t)store->min_free_bytes + required_bytes;
}

RfStoreError rf_store_last_error(const RfStore* store) {
    return store ? store->last_error : RfStoreErrorInvalidArgument;
}

const char* rf_store_error_text(RfStoreError error) {
    switch(error) {
    case RfStoreErrorNone: return "OK";
    case RfStoreErrorInvalidArgument: return "INVALID";
    case RfStoreErrorStorageLow: return "STORAGE LOW";
    case RfStoreErrorWrite: return "WRITE FAILED";
    case RfStoreErrorCommit: return "COMMIT FAILED";
    default: return "ERROR";
    }
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
        while(storage_dir_read(dir, &info, name, sizeof(name))) {
            if(!is_json_file(name)) continue;
            size_t n = strlen(name);
            char id[128];
            if(n - 5 >= sizeof(id)) continue;
            memcpy(id, name, n - 5);
            id[n - 5] = 0;
            if(!rf_store_is_acked(store, id)) count++;
        }
        storage_dir_close(dir);
    }
    storage_file_free(dir);
    return count;
}

bool rf_store_save(RfStore* store, const char* event_id, const char* json, size_t len) {
    if(!store || !rf_store_valid_event_id(event_id) || !json || !len) {
        rf_store_set_error(store, RfStoreErrorInvalidArgument);
        return false;
    }
    char final[160];
    event_path(final, sizeof(final), RF_STORE_EVENTS_DIR, event_id, ".json");
    FileInfo existing;
    /* Event identity is immutable.  Never silently replace pending evidence
       when a caller accidentally reuses an ID. */
    if(storage_common_stat(store->storage, final, &existing) == FSE_OK || rf_store_is_acked(store, event_id)) {
        rf_store_set_error(store, RfStoreErrorInvalidArgument);
        return false;
    }
    if(rf_store_storage_low(store, len + 512U)) {
        rf_store_set_error(store, RfStoreErrorStorageLow);
        return false;
    }
    char tmp[160];
    event_path(tmp, sizeof(tmp), RF_STORE_EVENTS_DIR, event_id, ".json.part");
    File* file = storage_file_alloc(store->storage);
    bool ok = false;
    if(storage_file_open(file, tmp, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        ok = storage_file_write(file, json, len) == len && storage_file_sync(file);
        storage_file_close(file);
    }
    storage_file_free(file);
    if(!ok) {
        storage_common_remove(store->storage, tmp);
        rf_store_set_error(store, RfStoreErrorWrite);
        return false;
    }
    /* Keep a rollback name while committing.  This closes the small window
       where remove(final) followed by a failed rename would lose an event. */
    char backup[160];
    event_path(backup, sizeof(backup), RF_STORE_EVENTS_DIR, event_id, ".json.bak");
    storage_common_remove(store->storage, backup);
    FileInfo final_info;
    bool had_final = storage_common_stat(store->storage, final, &final_info) == FSE_OK;
    if(had_final && storage_common_rename(store->storage, final, backup) != FSE_OK) {
        storage_common_remove(store->storage, tmp);
        rf_store_set_error(store, RfStoreErrorCommit);
        return false;
    }
    bool committed = storage_common_rename(store->storage, tmp, final) == FSE_OK;
    if(committed) {
        storage_common_remove(store->storage, backup);
        rf_store_set_error(store, RfStoreErrorNone);
        return true;
    }
    storage_common_remove(store->storage, tmp);
    if(had_final) storage_common_rename(store->storage, backup, final);
    rf_store_set_error(store, RfStoreErrorCommit);
    return false;
}

bool rf_store_read(RfStore* store, const char* event_id, char* out, size_t cap, size_t* used) {
    if(used) *used = 0;
    if(!store || !rf_store_valid_event_id(event_id) || !out || cap < 2) return false;
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
    if(!store || !rf_store_valid_event_id(event_id)) return false;
    char path[160];
    event_path(path, sizeof(path), RF_STORE_RECEIPTS_DIR, event_id, ".ack");
    FileInfo info;
    return storage_common_stat(store->storage, path, &info) == FSE_OK;
}

bool rf_store_ack(RfStore* store, const char* event_id) {
    if(!store || !rf_store_valid_event_id(event_id)) {
        rf_store_set_error(store, RfStoreErrorInvalidArgument);
        return false;
    }
    char path[160], receipt[160];
    event_path(path, sizeof(path), RF_STORE_EVENTS_DIR, event_id, ".json");
    event_path(receipt, sizeof(receipt), RF_STORE_RECEIPTS_DIR, event_id, ".ack");
    FileInfo event_info;
    bool event_exists = storage_common_stat(store->storage, path, &event_info) == FSE_OK;
    bool receipt_exists = rf_store_is_acked(store, event_id);
    if(receipt_exists) {
        if(!event_exists || store->retention_policy == 1) return true;
        /* A receipt is durable, but a previous reclaim may have failed. */
        bool reclaimed = storage_common_remove(store->storage, path) == FSE_OK;
        if(!reclaimed) rf_store_set_error(store, RfStoreErrorCommit);
        return reclaimed;
    }
    if(!event_exists) {
        rf_store_set_error(store, RfStoreErrorInvalidArgument);
        return false;
    }
    char receipt_tmp[160];
    event_path(receipt_tmp, sizeof(receipt_tmp), RF_STORE_RECEIPTS_DIR, event_id, ".ack.part");
    File* file = storage_file_alloc(store->storage);
    bool ok = storage_file_open(file, receipt_tmp, FSAM_WRITE, FSOM_CREATE_ALWAYS);
    if(ok) {
        ok = write_receipt(store, file, event_id, path) && storage_file_sync(file);
        storage_file_close(file);
    }
    storage_file_free(file);
    if(!ok || storage_common_rename(store->storage, receipt_tmp, receipt) != FSE_OK) {
        storage_common_remove(store->storage, receipt_tmp);
        rf_store_set_error(store, RfStoreErrorWrite);
        return false;
    }
    if(store->retention_policy != 1 && storage_common_remove(store->storage, path) != FSE_OK) {
        /* The receipt remains as the recovery marker; the next retry or app
           launch will attempt reclamation again. */
        rf_store_set_error(store, RfStoreErrorCommit);
        return false;
    }
    rf_store_set_error(store, RfStoreErrorNone);
    return true;
}
