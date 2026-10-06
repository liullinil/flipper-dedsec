#pragma once

#include <furi.h>
#include <storage/storage.h>

#define RF_STORE_DIR APP_DATA_PATH("rf_signal_hunter")
#define RF_STORE_EVENTS_DIR RF_STORE_DIR "/events"
#define RF_STORE_RECEIPTS_DIR RF_STORE_DIR "/receipts"
#define RF_STORE_DEVICE_ID RF_STORE_DIR "/device_id"

typedef struct RfStore RfStore;

RfStore* rf_store_alloc(Storage* storage);
void rf_store_free(RfStore* store);
const char* rf_store_device_id(const RfStore* store);
uint64_t rf_store_free_bytes(RfStore* store);
uint32_t rf_store_pending_count(RfStore* store);
bool rf_store_save(RfStore* store, const char* event_id, const char* json, size_t len);
bool rf_store_read(RfStore* store, const char* event_id, char* out, size_t cap, size_t* used);
bool rf_store_ack(RfStore* store, const char* event_id);
bool rf_store_is_acked(RfStore* store, const char* event_id);

