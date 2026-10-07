#pragma once
/* In-memory FAT-like storage with fault injection for the RF journal tests.
 *
 * Models the behaviour of the Unleashed 093 storage service that matters
 * for crash safety:
 *   - storage_common_rename() = remove destination, copy, remove source;
 *   - new directory entries reuse the first free slot (FAT order);
 *   - an open file cannot be removed;
 *   - a power cut stops all I/O mid-write and leaves partial data behind. */
#include <storage/storage.h>

extern Storage fake_storage;

void fake_reset(void);
/* Free space = base - bytes stored. */
void fake_set_base_free(uint64_t bytes);
uint64_t fake_used_bytes(void);
void fake_fail_writes(bool on);
void fake_fail_sync(bool on);
void fake_fail_remove(bool on);
/* After `bytes` more bytes reach the card the device "loses power":
 * the write in progress is cut and every later call fails.  -1 = off. */
void fake_power_cut_after(long bytes);
bool fake_dead(void);
/* Power back: clears faults, keeps the files. */
void fake_reboot(void);
int fake_open_handles(void);
bool fake_put(const char* path, const void* data, size_t size);
bool fake_get(const char* path, const unsigned char** data, size_t* size);
bool fake_exists(const char* path);
size_t fake_files_in(const char* dir);
