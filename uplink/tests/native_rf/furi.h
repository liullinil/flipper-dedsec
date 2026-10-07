#pragma once
/* Host stand-in for the parts of <furi.h> the RF engine, journal and protocol use. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define APP_DATA_PATH(path) "/data/" path
#define EXT_PATH(path) "/ext/" path
#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#endif
#define UNUSED(x) (void)(x)
#define COUNT_OF(x) (sizeof(x) / sizeof((x)[0]))
#define FURI_LOG_E(tag, ...) ((void)0)
#define FURI_LOG_W(tag, ...) ((void)0)
#define FURI_LOG_I(tag, ...) ((void)0)
#define FURI_LOG_D(tag, ...) ((void)0)

#define FuriWaitForever 0xFFFFFFFFU
typedef enum {
    FuriStatusOk = 0,
    FuriStatusError = -1,
    FuriStatusErrorTimeout = -2,
    FuriStatusErrorResource = -3,
} FuriStatus;
typedef enum { FuriMutexTypeNormal, FuriMutexTypeRecursive } FuriMutexType;
typedef struct FuriMutex FuriMutex;
typedef struct FuriMessageQueue FuriMessageQueue;
typedef struct FuriThread FuriThread;
typedef int32_t (*FuriThreadCallback)(void* context);

uint32_t furi_get_tick(void);
uint32_t furi_ms_to_ticks(uint32_t milliseconds);
FuriMutex* furi_mutex_alloc(FuriMutexType type);
void furi_mutex_free(FuriMutex* mutex);
FuriStatus furi_mutex_acquire(FuriMutex* mutex, uint32_t timeout);
FuriStatus furi_mutex_release(FuriMutex* mutex);
FuriMessageQueue* furi_message_queue_alloc(uint32_t msg_count, uint32_t msg_size);
void furi_message_queue_free(FuriMessageQueue* queue);
FuriStatus furi_message_queue_put(FuriMessageQueue* queue, const void* msg, uint32_t timeout);
FuriStatus furi_message_queue_get(FuriMessageQueue* queue, void* msg, uint32_t timeout);
FuriThread* furi_thread_alloc_ex(const char* name, uint32_t stack_size, FuriThreadCallback callback, void* context);
void furi_thread_start(FuriThread* thread);
bool furi_thread_join(FuriThread* thread);
void furi_thread_free(FuriThread* thread);
void* furi_record_open(const char* name);
void furi_record_close(const char* name);
