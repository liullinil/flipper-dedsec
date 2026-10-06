/* Fault injection against the actual FAP persistence implementation.  The
 * storage facade is an in-memory FAT-like filesystem, not a second model of
 * the RF algorithms.  In particular rename refuses to replace an existing
 * destination, matching the Flipper storage API. */
#include <assert.h>
#include <furi.h>
#include <storage/storage.h>
#include "rf_settings.h"
#include "rf_store.h"

#define FILES 96
#define CAPACITY 4096
typedef struct { char path[200]; unsigned char bytes[CAPACITY]; size_t size; bool used; } Entry;
struct File { int index; size_t offset; bool open; bool directory; size_t cursor; char path[200]; };
static Entry entries[FILES];
static uint64_t available = 1024 * 1024;
static bool fail_sync, fail_write, fail_rename, fail_remove;
static Storage storage;

static int lookup(const char* path) {
    for(int i = 0; i < FILES; i++) if(entries[i].used && !strcmp(path, entries[i].path)) return i;
    return -1;
}
static int put(const char* path, const void* data, size_t size) {
    int i = lookup(path);
    if(i < 0) for(i = 0; i < FILES && entries[i].used; i++) {}
    assert(i < FILES && size < CAPACITY);
    entries[i].used = true;
    snprintf(entries[i].path, sizeof(entries[i].path), "%s", path);
    entries[i].size = size;
    if(size) memcpy(entries[i].bytes, data, size);
    return i;
}
static void reset(void) { memset(entries, 0, sizeof(entries)); available = 1024 * 1024; fail_sync = fail_write = fail_rename = fail_remove = false; }
File* storage_file_alloc(Storage* unused) { (void)unused; return calloc(1, sizeof(File)); }
void storage_file_free(File* file) { free(file); }
bool storage_file_open(File* file, const char* path, FS_AccessMode access, FS_OpenMode mode) {
    (void)access;
    int i = lookup(path);
    if(mode == FSOM_CREATE_ALWAYS) i = put(path, NULL, 0);
    if(i < 0) return false;
    file->index = i; file->offset = 0; file->open = true; return true;
}
bool storage_file_is_open(File* file) { return file->open; }
bool storage_file_close(File* file) { file->open = false; return true; }
size_t storage_file_read(File* file, void* data, size_t count) {
    Entry* entry = &entries[file->index];
    size_t n = count < entry->size - file->offset ? count : entry->size - file->offset;
    memcpy(data, entry->bytes + file->offset, n); file->offset += n; return n;
}
size_t storage_file_write(File* file, const void* data, size_t count) {
    if(fail_write) return 0;
    Entry* entry = &entries[file->index]; assert(count + file->offset < CAPACITY);
    memcpy(entry->bytes + file->offset, data, count); file->offset += count; entry->size = file->offset; return count;
}
bool storage_file_sync(File* file) { (void)file; return !fail_sync; }
FS_Error storage_common_mkdir(Storage* unused, const char* path) { (void)unused; (void)path; return FSE_OK; }
FS_Error storage_common_stat(Storage* unused, const char* path, FileInfo* info) {
    (void)unused; int i = lookup(path); if(i < 0) return FSE_NOT_EXIST;
    if(info) info->size = (uint32_t)entries[i].size; return FSE_OK;
}
FS_Error storage_common_remove(Storage* unused, const char* path) {
    (void)unused; if(fail_remove) return FSE_INTERNAL; int i = lookup(path); if(i < 0) return FSE_NOT_EXIST;
    entries[i].used = false; return FSE_OK;
}
FS_Error storage_common_rename(Storage* unused, const char* from, const char* to) {
    (void)unused; if(fail_rename) return FSE_INTERNAL; int i = lookup(from);
    if(i < 0) return FSE_NOT_EXIST; if(lookup(to) >= 0) return FSE_INTERNAL;
    snprintf(entries[i].path, sizeof(entries[i].path), "%s", to); return FSE_OK;
}
FS_Error storage_common_fs_info(Storage* unused, const char* path, uint64_t* total, uint64_t* free_bytes) {
    (void)unused; (void)path; *total = 1024 * 1024; *free_bytes = available; return FSE_OK;
}
bool storage_dir_open(File* file, const char* path) { file->directory = true; file->open = true; file->cursor = 0; snprintf(file->path, sizeof(file->path), "%s/", path); return true; }
bool storage_dir_read(File* file, FileInfo* info, char* name, size_t cap) {
    size_t prefix = strlen(file->path);
    while(file->cursor < FILES) {
        Entry* entry = &entries[file->cursor++];
        if(entry->used && !strncmp(entry->path, file->path, prefix) && !strchr(entry->path + prefix, '/')) {
            snprintf(name, cap, "%s", entry->path + prefix); info->size = (uint32_t)entry->size; return true;
        }
    }
    return false;
}
bool storage_dir_close(File* file) { return storage_file_close(file); }
void furi_hal_random_fill_buf(void* data, size_t size) { memset(data, 0x5A, size); }

static void settings_roundtrip_and_failure(void) {
    reset(); RfHunterSettings value, loaded;
    rf_settings_defaults(&value);
    value.timezone_offset_minutes = 345;
    value.retention_policy = RfRetentionKeepCapture;
    value.min_free_bytes = 65536;
    assert(rf_settings_save(&storage, &value));
    assert(rf_settings_load(&storage, &loaded));
    assert(loaded.timezone_offset_minutes == 345 && loaded.retention_policy == RfRetentionKeepCapture && loaded.min_free_bytes == 65536);
    fail_sync = true; value.timezone_offset_minutes = -210;
    assert(!rf_settings_save(&storage, &value)); fail_sync = false;
    assert(rf_settings_load(&storage, &loaded) && loaded.timezone_offset_minutes == 345);
    assert(storage_common_rename(&storage, "/data/rf_signal_hunter/settings.bin", "/data/rf_signal_hunter/settings.bin.bak") == FSE_OK);
    put("/data/rf_signal_hunter/settings.bin.part", "broken", 6);
    assert(rf_settings_load(&storage, &loaded) && loaded.timezone_offset_minutes == 345);
    assert(lookup("/data/rf_signal_hunter/settings.bin.part") < 0);
}
static void preserve_pending_and_receipts(void) {
    reset(); RfStore* store = rf_store_alloc(&storage);
    rf_store_configure(store, RfRetentionCompactAfterAck, 32768);
    assert(rf_store_save(store, "one", "{\"value\":1}", 11));
    available = 100;
    assert(!rf_store_save(store, "two", "{}", 2));
    assert(rf_store_last_error(store) == RfStoreErrorStorageLow);
    assert(rf_store_pending_count(store) == 1);
    available = 1024 * 1024;
    fail_sync = true;
    assert(!rf_store_ack(store, "one"));
    assert(!rf_store_is_acked(store, "one") && rf_store_pending_count(store) == 1);
    fail_sync = false;
    assert(rf_store_ack(store, "one"));
    assert(rf_store_is_acked(store, "one") && rf_store_pending_count(store) == 0);
    assert(lookup(RF_STORE_EVENTS_DIR "/one.json") < 0);
    assert(!rf_store_ack(store, "never-recorded"));
    rf_store_free(store);
}
static void reset_recovery_and_policy(void) {
    reset();
    put(RF_STORE_EVENTS_DIR "/one.json.bak", "old", 3);
    put(RF_STORE_EVENTS_DIR "/one.json.part", "partial", 7);
    put(RF_STORE_EVENTS_DIR "/two.json", "acked", 5);
    put(RF_STORE_RECEIPTS_DIR "/two.ack", "", 0);
    RfStore* store = rf_store_alloc(&storage);
    rf_store_configure(store, RfRetentionKeepCapture, 32768);
    assert(lookup(RF_STORE_EVENTS_DIR "/one.json") >= 0);
    assert(lookup(RF_STORE_EVENTS_DIR "/one.json.part") < 0);
    assert(lookup(RF_STORE_EVENTS_DIR "/two.json") >= 0);
    assert(rf_store_pending_count(store) == 1);
    rf_store_configure(store, RfRetentionCompactAfterAck, 32768);
    assert(lookup(RF_STORE_EVENTS_DIR "/two.json") < 0);
    assert(rf_store_pending_count(store) == 1);
    fail_write = true;
    assert(!rf_store_save(store, "one", "new", 3));
    fail_write = false;
    char data[20]; size_t length = 0;
    assert(rf_store_read(store, "one", data, sizeof(data), &length));
    assert(length == 3 && !strcmp(data, "old"));
    rf_store_free(store);
}
int main(void) {
    settings_roundtrip_and_failure();
    preserve_pending_and_receipts();
    reset_recovery_and_policy();
    puts("RF persistence fault tests passed");
    return 0;
}
