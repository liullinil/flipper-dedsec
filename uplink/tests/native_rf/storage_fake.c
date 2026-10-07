#include "storage_fake.h"

#include <furi_hal_random.h>

#define FAKE_FILES 2048
#define FAKE_PATH  160
#define FAKE_CHUNK 512

typedef struct {
    char path[FAKE_PATH];
    unsigned char* data;
    size_t size;
    size_t capacity;
    bool used;
    bool dir;
    int open_count;
} Entry;

struct File {
    int index;
    size_t offset;
    bool open;
    bool is_dir;
    size_t cursor;
    char prefix[FAKE_PATH + 2];
    FS_Error error;
};

Storage fake_storage;
static Entry entries[FAKE_FILES];
static uint64_t base_free = 64ULL * 1024ULL * 1024ULL;
static bool fail_write, fail_sync, fail_remove, dead;
static long cut_budget = -1;
static int open_handles;

static void drop(int i) {
    free(entries[i].data);
    memset(&entries[i], 0, sizeof(entries[i]));
}

void fake_reset(void) {
    for(int i = 0; i < FAKE_FILES; i++) drop(i);
    base_free = 64ULL * 1024ULL * 1024ULL;
    fail_write = fail_sync = fail_remove = dead = false;
    cut_budget = -1;
    open_handles = 0;
}

void fake_set_base_free(uint64_t bytes) { base_free = bytes; }
void fake_fail_writes(bool on) { fail_write = on; }
void fake_fail_sync(bool on) { fail_sync = on; }
void fake_fail_remove(bool on) { fail_remove = on; }
void fake_power_cut_after(long bytes) { cut_budget = bytes; }
bool fake_dead(void) { return dead; }
int fake_open_handles(void) { return open_handles; }

void fake_reboot(void) {
    fail_write = fail_sync = fail_remove = dead = false;
    cut_budget = -1;
}

uint64_t fake_used_bytes(void) {
    uint64_t used = 0;
    for(int i = 0; i < FAKE_FILES; i++) if(entries[i].used) used += entries[i].size;
    return used;
}

static int lookup(const char* path) {
    for(int i = 0; i < FAKE_FILES; i++) {
        if(entries[i].used && strcmp(entries[i].path, path) == 0) return i;
    }
    return -1;
}

static int create(const char* path, bool dir) {
    for(int i = 0; i < FAKE_FILES; i++) {
        if(!entries[i].used) {
            entries[i].used = true;
            entries[i].dir = dir;
            entries[i].size = 0;
            snprintf(entries[i].path, sizeof(entries[i].path), "%s", path);
            return i;
        }
    }
    fprintf(stderr, "fake storage full\n");
    exit(3);
}

static bool reserve(Entry* entry, size_t size) {
    if(size <= entry->capacity) return true;
    size_t capacity = entry->capacity ? entry->capacity : 64;
    while(capacity < size) capacity *= 2;
    unsigned char* data = realloc(entry->data, capacity);
    if(!data) return false;
    entry->data = data;
    entry->capacity = capacity;
    return true;
}

/* Write through the power-cut budget; false when the device died. */
static bool put_bytes(Entry* entry, size_t offset, const void* data, size_t count, size_t* written) {
    size_t n = count;
    if(cut_budget >= 0 && (long)n > cut_budget) n = (size_t)cut_budget;
    if(!reserve(entry, offset + n)) return false;
    if(n) memcpy(entry->data + offset, data, n);
    if(offset + n > entry->size) entry->size = offset + n;
    if(cut_budget >= 0) cut_budget -= (long)n;
    *written = n;
    if(n < count) {
        dead = true;
        return false;
    }
    return true;
}

bool fake_put(const char* path, const void* data, size_t size) {
    int i = lookup(path);
    if(i < 0) i = create(path, false);
    entries[i].size = 0;
    if(!reserve(&entries[i], size)) return false;
    if(size) memcpy(entries[i].data, data, size);
    entries[i].size = size;
    return true;
}

bool fake_get(const char* path, const unsigned char** data, size_t* size) {
    int i = lookup(path);
    if(i < 0 || entries[i].dir) return false;
    *data = entries[i].data;
    *size = entries[i].size;
    return true;
}

bool fake_exists(const char* path) { return lookup(path) >= 0; }

static bool direct_child(const char* path, const char* prefix) {
    size_t n = strlen(prefix);
    return strncmp(path, prefix, n) == 0 && path[n] && !strchr(path + n, '/');
}

size_t fake_files_in(const char* dir) {
    char prefix[FAKE_PATH + 2];
    snprintf(prefix, sizeof(prefix), "%s/", dir);
    size_t count = 0;
    for(int i = 0; i < FAKE_FILES; i++) {
        if(entries[i].used && !entries[i].dir && direct_child(entries[i].path, prefix)) count++;
    }
    return count;
}

File* storage_file_alloc(Storage* storage) {
    (void)storage;
    File* file = calloc(1, sizeof(File));
    file->index = -1;
    return file;
}

bool storage_file_is_open(File* file) { return file->open; }

bool storage_file_close(File* file) {
    if(!file->open) return false;
    if(!file->is_dir && file->index >= 0) entries[file->index].open_count--;
    file->open = false;
    open_handles--;
    return true;
}

void storage_file_free(File* file) {
    if(file->open) storage_file_close(file);
    free(file);
}

bool storage_file_open(File* file, const char* path, FS_AccessMode access, FS_OpenMode mode) {
    (void)access;
    if(file->open) storage_file_close(file);
    file->offset = 0;
    file->is_dir = false;
    if(dead) {
        file->error = FSE_NOT_READY;
        return false;
    }
    int i = lookup(path);
    if(i >= 0 && entries[i].dir) {
        file->error = FSE_DENIED;
        return false;
    }
    switch(mode) {
    case FSOM_OPEN_EXISTING:
        if(i < 0) {
            file->error = FSE_NOT_EXIST;
            return false;
        }
        break;
    case FSOM_OPEN_ALWAYS:
    case FSOM_OPEN_APPEND:
        if(i < 0) i = create(path, false);
        if(mode == FSOM_OPEN_APPEND) file->offset = entries[i].size;
        break;
    case FSOM_CREATE_NEW:
        if(i >= 0) {
            file->error = FSE_EXIST;
            return false;
        }
        i = create(path, false);
        break;
    case FSOM_CREATE_ALWAYS:
        if(i < 0) {
            i = create(path, false);
        } else if(entries[i].open_count) {
            file->error = FSE_ALREADY_OPEN;
            return false;
        } else {
            entries[i].size = 0;
        }
        break;
    default:
        file->error = FSE_INVALID_PARAMETER;
        return false;
    }
    entries[i].open_count++;
    file->index = i;
    file->open = true;
    file->error = FSE_OK;
    open_handles++;
    return true;
}

size_t storage_file_read(File* file, void* data, size_t count) {
    if(!file->open || file->is_dir || dead) {
        file->error = FSE_NOT_READY;
        return 0;
    }
    Entry* entry = &entries[file->index];
    if(file->offset >= entry->size) return 0;
    size_t n = entry->size - file->offset < count ? entry->size - file->offset : count;
    memcpy(data, entry->data + file->offset, n);
    file->offset += n;
    return n;
}

size_t storage_file_write(File* file, const void* data, size_t count) {
    if(!file->open || file->is_dir || dead || fail_write) {
        file->error = FSE_INTERNAL;
        return 0;
    }
    size_t written = 0;
    bool alive = put_bytes(&entries[file->index], file->offset, data, count, &written);
    file->offset += written;
    if(!alive) file->error = FSE_NOT_READY;
    return written;
}

bool storage_file_seek(File* file, uint32_t offset, bool from_start) {
    if(!file->open || dead) return false;
    file->offset = from_start ? offset : file->offset + offset;
    return true;
}

uint64_t storage_file_size(File* file) {
    return file->open && !file->is_dir ? entries[file->index].size : 0;
}

bool storage_file_sync(File* file) { return file->open && !dead && !fail_sync; }

FS_Error storage_file_get_error(File* file) { return file->error; }

FS_Error storage_common_mkdir(Storage* storage, const char* path) {
    (void)storage;
    if(dead) return FSE_NOT_READY;
    if(lookup(path) >= 0) return FSE_EXIST;
    create(path, true);
    return FSE_OK;
}

FS_Error storage_common_stat(Storage* storage, const char* path, FileInfo* info) {
    (void)storage;
    if(dead) return FSE_NOT_READY;
    int i = lookup(path);
    if(i < 0) return FSE_NOT_EXIST;
    if(info) {
        info->flags = entries[i].dir ? FSF_DIRECTORY : 0;
        info->size = entries[i].size;
    }
    return FSE_OK;
}

bool storage_common_exists(Storage* storage, const char* path) {
    return storage_common_stat(storage, path, NULL) == FSE_OK;
}

FS_Error storage_common_remove(Storage* storage, const char* path) {
    (void)storage;
    if(dead) return FSE_NOT_READY;
    if(fail_remove) return FSE_INTERNAL;
    int i = lookup(path);
    if(i < 0) return FSE_NOT_EXIST;
    if(entries[i].open_count) return FSE_ALREADY_OPEN;
    drop(i);
    return FSE_OK;
}

/* Unleashed: remove the destination, copy, remove the source. */
FS_Error storage_common_rename(Storage* storage, const char* from, const char* to) {
    (void)storage;
    if(dead) return FSE_NOT_READY;
    int source = lookup(from);
    if(source < 0) return FSE_NOT_EXIST;
    if(strcmp(from, to) == 0) return FSE_OK;
    int target = lookup(to);
    if(target >= 0) {
        if(entries[target].open_count) return FSE_ALREADY_OPEN;
        drop(target);
    }
    target = create(to, false);
    size_t copied = 0;
    while(copied < entries[source].size) {
        size_t chunk = entries[source].size - copied;
        if(chunk > FAKE_CHUNK) chunk = FAKE_CHUNK;
        size_t written = 0;
        if(!put_bytes(&entries[target], copied, entries[source].data + copied, chunk, &written)) {
            return FSE_NOT_READY;
        }
        copied += written;
    }
    if(fail_remove) return FSE_INTERNAL;
    if(entries[source].open_count) return FSE_ALREADY_OPEN;
    drop(source);
    return FSE_OK;
}

FS_Error storage_common_fs_info(Storage* storage, const char* path, uint64_t* total, uint64_t* free_bytes) {
    (void)storage;
    (void)path;
    if(dead) return FSE_NOT_READY;
    uint64_t used = fake_used_bytes();
    if(total) *total = 1024ULL * 1024ULL * 1024ULL;
    if(free_bytes) *free_bytes = base_free > used ? base_free - used : 0;
    return FSE_OK;
}

bool storage_dir_open(File* file, const char* path) {
    if(file->open) storage_file_close(file);
    if(dead) {
        file->error = FSE_NOT_READY;
        return false;
    }
    snprintf(file->prefix, sizeof(file->prefix), "%s/", path);
    file->is_dir = true;
    file->open = true;
    file->cursor = 0;
    file->index = -1;
    open_handles++;
    return true;
}

bool storage_dir_read(File* file, FileInfo* info, char* name, uint16_t name_length) {
    if(!file->open || !file->is_dir || dead) return false;
    while(file->cursor < FAKE_FILES) {
        Entry* entry = &entries[file->cursor++];
        if(!entry->used || !direct_child(entry->path, file->prefix)) continue;
        snprintf(name, name_length, "%s", entry->path + strlen(file->prefix));
        info->flags = entry->dir ? FSF_DIRECTORY : 0;
        info->size = entry->size;
        return true;
    }
    return false;
}

bool storage_dir_close(File* file) { return storage_file_close(file); }

void furi_hal_random_fill_buf(void* data, size_t size) {
    static unsigned char seed = 0x5A;
    unsigned char* bytes = data;
    for(size_t i = 0; i < size; i++) bytes[i] = (unsigned char)(seed += 0x3D);
}
