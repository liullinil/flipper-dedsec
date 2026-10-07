#pragma once
/* Host stand-in for <storage/storage.h>: same names, types and values as the
 * Flipper SDK (API 88.9) for the calls the RF journal makes. */
#include <furi.h>
#define RECORD_STORAGE "storage"
typedef struct Storage { int unused; } Storage;
typedef struct File File;
typedef enum { FSF_DIRECTORY = (1 << 0) } FS_Flags;
typedef struct { uint8_t flags; uint64_t size; } FileInfo;
typedef enum {
    FSE_OK, FSE_NOT_READY, FSE_EXIST, FSE_NOT_EXIST, FSE_INVALID_PARAMETER, FSE_DENIED,
    FSE_INVALID_NAME, FSE_INTERNAL, FSE_NOT_IMPLEMENTED, FSE_ALREADY_OPEN
} FS_Error;
typedef enum { FSAM_READ = 1, FSAM_WRITE = 2, FSAM_READ_WRITE = 3 } FS_AccessMode;
typedef enum {
    FSOM_OPEN_EXISTING = 1, FSOM_OPEN_ALWAYS = 2, FSOM_OPEN_APPEND = 4,
    FSOM_CREATE_NEW = 8, FSOM_CREATE_ALWAYS = 16
} FS_OpenMode;
File* storage_file_alloc(Storage* storage);
void storage_file_free(File* file);
bool storage_file_open(File* file, const char* path, FS_AccessMode access, FS_OpenMode mode);
bool storage_file_is_open(File* file);
bool storage_file_close(File* file);
size_t storage_file_read(File* file, void* data, size_t count);
size_t storage_file_write(File* file, const void* data, size_t count);
bool storage_file_seek(File* file, uint32_t offset, bool from_start);
uint64_t storage_file_size(File* file);
bool storage_file_sync(File* file);
FS_Error storage_file_get_error(File* file);
FS_Error storage_common_mkdir(Storage* storage, const char* path);
FS_Error storage_common_stat(Storage* storage, const char* path, FileInfo* info);
FS_Error storage_common_remove(Storage* storage, const char* path);
FS_Error storage_common_rename(Storage* storage, const char* from, const char* to);
bool storage_common_exists(Storage* storage, const char* path);
FS_Error storage_common_fs_info(Storage* storage, const char* path, uint64_t* total, uint64_t* free_bytes);
bool storage_dir_open(File* file, const char* path);
bool storage_dir_read(File* file, FileInfo* info, char* name, uint16_t name_length);
bool storage_dir_close(File* file);
