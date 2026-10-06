#pragma once
#include <furi.h>
typedef struct Storage { int unused; } Storage;
typedef struct File File;
typedef struct { uint32_t size; } FileInfo;
typedef enum { FSE_OK, FSE_NOT_EXIST, FSE_INTERNAL } FS_Error;
typedef enum { FSAM_READ, FSAM_WRITE } FS_AccessMode;
typedef enum { FSOM_OPEN_EXISTING, FSOM_CREATE_ALWAYS } FS_OpenMode;
File* storage_file_alloc(Storage* storage);
void storage_file_free(File* file);
bool storage_file_open(File*, const char*, FS_AccessMode, FS_OpenMode);
bool storage_file_is_open(File*);
bool storage_file_close(File*);
size_t storage_file_read(File*, void*, size_t);
size_t storage_file_write(File*, const void*, size_t);
bool storage_file_sync(File*);
FS_Error storage_common_mkdir(Storage*, const char*);
FS_Error storage_common_stat(Storage*, const char*, FileInfo*);
FS_Error storage_common_remove(Storage*, const char*);
FS_Error storage_common_rename(Storage*, const char*, const char*);
FS_Error storage_common_fs_info(Storage*, const char*, uint64_t*, uint64_t*);
bool storage_dir_open(File*, const char*);
bool storage_dir_read(File*, FileInfo*, char*, size_t);
bool storage_dir_close(File*);
