/* Host harness for the DedSec Uplink RF journal, record builder and sync
 * protocol (apps/dedsec_uplink/rf_store.c, rf_record.c, rf_proto.c).
 *
 *   rf_harness store    crash/fault tests of the journal (asserts, exit 1 on failure)
 *   rf_harness record   prints records built from edge-case inputs for json.loads
 *   rf_harness proto    line REPL: PC requests on stdin, Flipper replies on stdout
 */
#include <furi.h>
#include <storage/storage.h>
#include "storage_fake.h"
#include "rf_proto.h"
#include "rf_record.h"
#include "rf_store.h"

#define CHECK(condition)                                                                   \
    do {                                                                                   \
        if(!(condition)) {                                                                 \
            fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, #condition); \
            exit(1);                                                                       \
        }                                                                                  \
    } while(0)

#define RECORD_CAPACITY 4096U

static char record_text[RECORD_CAPACITY];
static uint32_t pulses[512];

static size_t build(const char* id, uint32_t sequence, uint32_t count, uint32_t base) {
    for(uint32_t i = 0; i < count && i < 512; i++) {
        pulses[i] = ((i & 1U) ? 0U : 0x80000000UL) | (base + (i % 7U) * 13U);
    }
    RfSubGhzRecord record;
    memset(&record, 0, sizeof(record));
    record.common.event_id = id;
    record.common.device_id = "0123456789abcdef";
    record.common.session_id = "s00112233445566";
    record.common.sequence = sequence;
    record.common.rtc_local_unix = 1791273672U;
    record.common.tz_offset_minutes = 180;
    record.common.monotonic_ms = 1234;
    record.common.battery_pct = 77;
    record.mode = "SCOUT";
    record.frequency_hz = 433920000U;
    record.duration_us = 12800;
    record.fingerprint = 0xdeadbeefU;
    record.rssi_min_dbm = -80.0f;
    record.rssi_avg_dbm = -70.5f;
    record.rssi_max_dbm = -60.0f;
    record.pulse_count = count;
    record.last_duration_us = base;
    record.timings = pulses;
    record.timing_count = count;
    size_t length = rf_record_subghz(record_text, sizeof(record_text), &record, NULL);
    CHECK(length > 0);
    return length;
}

static void event_path(char* out, size_t size, const char* dir, const char* id, const char* suffix) {
    snprintf(out, size, "%s/%s%s", dir, id, suffix);
}

static bool has_file(const char* dir, const char* id, const char* suffix) {
    char path[200];
    event_path(path, sizeof(path), dir, id, suffix);
    return fake_exists(path);
}

static bool file_equals(const char* dir, const char* id, const char* data, size_t length) {
    char path[200];
    event_path(path, sizeof(path), dir, id, ".json");
    const unsigned char* bytes = NULL;
    size_t size = 0;
    return fake_get(path, &bytes, &size) && size == length && memcmp(bytes, data, length) == 0;
}

static RfStore* boot(void) {
    RfStore* store = rf_store_alloc(&fake_storage);
    rf_store_open(store);
    return store;
}

static void power_cycle(RfStore** store) {
    rf_store_free(*store);
    CHECK(fake_open_handles() == 0);
    fake_reboot();
    *store = boot();
    CHECK(fake_open_handles() == 0);
}

static void save_ok(RfStore* store, const char* id, uint32_t sequence) {
    size_t length = build(id, sequence, 24, 400);
    CHECK(rf_store_save(store, id, record_text, length) == RfStoreOk);
}

/* ------------------------------------------------------------------ store tests */

static void test_crc_and_ids(void) {
    CHECK(rf_store_crc32(0, "123456789", 9) == 0xCBF43926UL);
    CHECK(rf_store_crc32(rf_store_crc32(0, "1234", 4), "56789", 5) == 0xCBF43926UL);
    CHECK(rf_store_crc32(0, "", 0) == 0);
    CHECK(rf_store_valid_id("rf-0123456789abcdef-s0123456789abcd-4294967295"));
    CHECK(rf_store_valid_id("A.b_c-1"));
    CHECK(!rf_store_valid_id(""));
    CHECK(!rf_store_valid_id(".hidden"));
    CHECK(!rf_store_valid_id("-x"));
    CHECK(!rf_store_valid_id("a/b"));
    CHECK(!rf_store_valid_id("a\\b"));
    CHECK(!rf_store_valid_id("a b"));
    CHECK(!rf_store_valid_id("a|b"));
    char id[64];
    memset(id, 'a', 48);
    id[48] = '\0';
    CHECK(rf_store_valid_id(id));
    id[48] = 'a';
    id[49] = '\0';
    CHECK(!rf_store_valid_id(id));
}

static void test_round_trip(void) {
    fake_reset();
    RfStore* store = boot();
    CHECK(rf_store_pending(store) == 0 && rf_store_stored(store) == 0);
    char device[17];
    CHECK(strlen(rf_store_device_id(store)) == 16);
    strcpy(device, rf_store_device_id(store));

    static char a[RECORD_CAPACITY], b[RECORD_CAPACITY];
    size_t la = build("rec-a", 1, 40, 400);
    memcpy(a, record_text, la);
    CHECK(rf_store_save(store, "rec-a", a, la) == RfStoreOk);
    size_t lb = build("rec-b", 2, 30, 300);
    memcpy(b, record_text, lb);
    CHECK(rf_store_save(store, "rec-b", b, lb) == RfStoreOk);
    save_ok(store, "rec-c", 3);
    CHECK(rf_store_pending(store) == 3);

    char id[RF_STORE_ID_MAX + 1];
    uint32_t size = 0, crc = 0;
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk);
    CHECK(strcmp(id, "rec-a") == 0 && size == la && crc == rf_store_crc32(0, a, la));
    uint32_t crc_a = crc;
    uint8_t chunk[120];
    size_t got = 0;
    uint32_t total = 0;
    CHECK(rf_store_read(store, "rec-a", 0, chunk, sizeof(chunk), &got, &total) == RfStoreOk);
    CHECK(got == 120 && total == la && memcmp(chunk, a, 120) == 0);
    CHECK(rf_store_read(store, "rec-a", (uint32_t)la - 5U, chunk, 120, &got, &total) == RfStoreOk);
    CHECK(got == 5 && memcmp(chunk, a + la - 5, 5) == 0);
    CHECK(rf_store_read(store, "rec-a", (uint32_t)la, chunk, 120, &got, &total) == RfStoreOk && !got);
    CHECK(rf_store_read(store, "rec-a", (uint32_t)la + 1U, chunk, 120, &got, &total) == RfStoreErrInvalid);
    CHECK(rf_store_read(store, "missing", 0, chunk, 120, &got, &total) == RfStoreErrNotFound);
    CHECK(rf_store_read(store, "../x", 0, chunk, 120, &got, &total) == RfStoreErrInvalid);
    CHECK(rf_store_list(store, 1, id, sizeof(id), &size, &crc) == RfStoreOk && !strcmp(id, "rec-b"));
    uint32_t crc_b = crc;
    CHECK(rf_store_list(store, 2, id, sizeof(id), &size, &crc) == RfStoreOk && !strcmp(id, "rec-c"));
    CHECK(rf_store_list(store, 3, id, sizeof(id), &size, &crc) == RfStoreErrNotFound);

    CHECK(rf_store_ack(store, "rec-a", (uint32_t)la, crc_a ^ 1U, false) == RfStoreErrMismatch);
    CHECK(rf_store_ack(store, "rec-a", (uint32_t)la + 1U, crc_a, false) == RfStoreErrMismatch);
    CHECK(rf_store_pending(store) == 3 && has_file(RF_STORE_EVENTS_DIR, "rec-a", ".json"));
    CHECK(rf_store_ack(store, "rec-a", (uint32_t)la, crc_a, false) == RfStoreOk);
    CHECK(rf_store_pending(store) == 2 && rf_store_stored(store) == 2);
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "rec-a", ".json"));
    CHECK(!has_file(RF_STORE_UPLOADED_DIR, "rec-a", ".json"));
    /* A lost RK: the PC repeats the ACK. */
    CHECK(rf_store_ack(store, "rec-a", (uint32_t)la, crc_a, false) == RfStoreOk);
    CHECK(rf_store_ack(store, "rec-a", (uint32_t)la, crc_a ^ 1U, false) == RfStoreErrMismatch);
    /* The record left events/, so cursor 0 is now the next one. */
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk && !strcmp(id, "rec-b"));
    CHECK(rf_store_ack(store, "rec-b", (uint32_t)lb, crc_b, true) == RfStoreOk);
    CHECK(file_equals(RF_STORE_UPLOADED_DIR, "rec-b", b, lb));
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "rec-b", ".json"));
    CHECK(rf_store_pending(store) == 1 && rf_store_stored(store) == 2);
    CHECK(rf_store_ack(store, "never", 1, 2, false) == RfStoreErrNotFound);
    CHECK(rf_store_ack(store, "a/b", 1, 2, false) == RfStoreErrInvalid);

    /* Event ids are immutable. */
    CHECK(rf_store_save(store, "rec-c", "{\"x\":1}\n", 8) == RfStoreErrExists);
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk && size != 8);
    CHECK(rf_store_save(store, "bad/id", a, la) == RfStoreErrInvalid);
    CHECK(rf_store_save(store, "rec-d", "not json", 8) == RfStoreErrInvalid);
    CHECK(rf_store_save(store, "rec-d", "{\"x\":1}", 7) == RfStoreErrInvalid);
    CHECK(rf_store_pending(store) == 1);
    rf_store_idle(store);
    CHECK(fake_open_handles() == 0);

    power_cycle(&store);
    CHECK(strcmp(rf_store_device_id(store), device) == 0);
    CHECK(rf_store_pending(store) == 1 && rf_store_stored(store) == 2);
    /* After a restart the kept copy still proves the earlier ACK. */
    CHECK(rf_store_ack(store, "rec-b", (uint32_t)lb, crc_b, true) == RfStoreOk);
    CHECK(rf_store_ack(store, "rec-b", (uint32_t)lb, crc_b ^ 1U, true) == RfStoreErrMismatch);
    CHECK(rf_store_ack(store, "rec-a", (uint32_t)la, crc_a, false) == RfStoreErrNotFound);
    rf_store_free(store);
}

static void test_torn_write(void) {
    fake_reset();
    RfStore* store = boot();
    save_ok(store, "keep-1", 1);
    size_t length = build("torn-1", 2, 200, 1000);
    /* Power lost in the middle of the record. */
    fake_power_cut_after(64 + 100);
    CHECK(rf_store_save(store, "torn-1", record_text, length) == RfStoreErrIo);
    CHECK(fake_dead());
    power_cycle(&store);
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "torn-1", ".json"));
    CHECK(has_file(RF_STORE_EVENTS_DIR, "keep-1", ".json"));
    CHECK(rf_store_pending(store) == 1);
    /* Power lost right after the file was created. */
    length = build("torn-2", 3, 10, 500);
    fake_power_cut_after(64);
    CHECK(rf_store_save(store, "torn-2", record_text, length) == RfStoreErrIo);
    power_cycle(&store);
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "torn-2", ".json") && rf_store_pending(store) == 1);
    /* Power lost while the intent marker was rewritten: whatever id the torn
     * marker names, a complete record is never dropped. */
    save_ok(store, "keep-2", 4);
    for(long cut = 0; cut < 64; cut += 3) {
        length = build("keep-3", 5, 10, 500);
        fake_power_cut_after(cut);
        CHECK(rf_store_save(store, "keep-3", record_text, length) == RfStoreErrIo);
        power_cycle(&store);
        CHECK(has_file(RF_STORE_EVENTS_DIR, "keep-1", ".json"));
        CHECK(has_file(RF_STORE_EVENTS_DIR, "keep-2", ".json"));
        CHECK(!has_file(RF_STORE_EVENTS_DIR, "keep-3", ".json"));
        CHECK(rf_store_pending(store) == 2);
    }
    /* Plain write/sync errors leave nothing behind. */
    length = build("err-1", 6, 10, 500);
    fake_fail_writes(true);
    CHECK(rf_store_save(store, "err-1", record_text, length) == RfStoreErrIo);
    fake_fail_writes(false);
    fake_fail_sync(true);
    CHECK(rf_store_save(store, "err-1", record_text, length) == RfStoreErrIo);
    fake_fail_sync(false);
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "err-1", ".json") && rf_store_pending(store) == 2);
    CHECK(rf_store_save(store, "err-1", record_text, length) == RfStoreOk);
    rf_store_free(store);
}

static void test_interrupted_move(void) {
    fake_reset();
    RfStore* store = boot();
    static char m[RECORD_CAPACITY];
    size_t lm = build("mv-1", 1, 300, 1200);
    memcpy(m, record_text, lm);
    CHECK(rf_store_save(store, "mv-1", m, lm) == RfStoreOk);
    uint32_t crc = rf_store_crc32(0, m, lm);
    /* Power lost while copying to uploaded/. */
    fake_power_cut_after(64 + 700);
    CHECK(rf_store_ack(store, "mv-1", (uint32_t)lm, crc, true) == RfStoreErrIo);
    power_cycle(&store);
    CHECK(file_equals(RF_STORE_EVENTS_DIR, "mv-1", m, lm));
    CHECK(!has_file(RF_STORE_UPLOADED_DIR, "mv-1", ".json"));
    CHECK(rf_store_pending(store) == 1 && rf_store_stored(store) == 1);
    /* Copy complete, removing the pending record fails. */
    fake_fail_remove(true);
    CHECK(rf_store_ack(store, "mv-1", (uint32_t)lm, crc, true) == RfStoreErrIo);
    fake_fail_remove(false);
    CHECK(has_file(RF_STORE_EVENTS_DIR, "mv-1", ".json"));
    CHECK(has_file(RF_STORE_UPLOADED_DIR, "mv-1", ".json"));
    power_cycle(&store);
    CHECK(has_file(RF_STORE_EVENTS_DIR, "mv-1", ".json"));
    CHECK(!has_file(RF_STORE_UPLOADED_DIR, "mv-1", ".json"));
    CHECK(rf_store_pending(store) == 1 && rf_store_stored(store) == 1);
    CHECK(rf_store_ack(store, "mv-1", (uint32_t)lm, crc, true) == RfStoreOk);
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "mv-1", ".json"));
    CHECK(file_equals(RF_STORE_UPLOADED_DIR, "mv-1", m, lm));
    CHECK(rf_store_pending(store) == 0 && rf_store_stored(store) == 1);
    /* Same failure without a restart: the retry replaces the kept copy. */
    save_ok(store, "mv-2", 2);
    size_t l2 = strlen(record_text);
    uint32_t crc2 = rf_store_crc32(0, record_text, l2);
    fake_fail_remove(true);
    CHECK(rf_store_ack(store, "mv-2", (uint32_t)l2, crc2, true) == RfStoreErrIo);
    fake_fail_remove(false);
    CHECK(rf_store_ack(store, "mv-2", (uint32_t)l2, crc2, true) == RfStoreOk);
    CHECK(rf_store_pending(store) == 0 && rf_store_stored(store) == 2);
    CHECK(fake_files_in(RF_STORE_UPLOADED_DIR) == 2);
    rf_store_free(store);
}

static void test_quarantine_and_names(void) {
    fake_reset();
    RfStore* store = boot();
    static const char broken[] = "{\"schema_version\":1,\"event_id\":\"broken\"";
    CHECK(fake_put(RF_STORE_EVENTS_DIR "/broken.json", broken, sizeof(broken) - 1));
    CHECK(storage_common_mkdir(&fake_storage, RF_STORE_EVENTS_DIR "/folder.json") == FSE_OK);
    CHECK(fake_put(RF_STORE_EVENTS_DIR "/bad name.json", "{}\n", 3));
    CHECK(fake_put(RF_STORE_EVENTS_DIR "/.hidden.json", "{}\n", 3));
    CHECK(fake_put(RF_STORE_EVENTS_DIR "/old.json.bad", "{}\n", 3));
    save_ok(store, "good-1", 1);
    save_ok(store, "good-2", 2);
    power_cycle(&store);
    CHECK(rf_store_pending(store) == 3); /* broken + good-1 + good-2 */
    char id[RF_STORE_ID_MAX + 1];
    uint32_t size = 0, crc = 0;
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk);
    CHECK(strcmp(id, "good-1") == 0);
    CHECK(rf_store_pending(store) == 2);
    CHECK(has_file(RF_STORE_EVENTS_DIR, "broken", ".bad"));
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "broken", ".json"));
    CHECK(rf_store_list(store, 1, id, sizeof(id), &size, &crc) == RfStoreOk && !strcmp(id, "good-2"));
    CHECK(rf_store_list(store, 2, id, sizeof(id), &size, &crc) == RfStoreErrNotFound);
    rf_store_free(store);
}

static void ack_first(RfStore* store, bool keep) {
    char id[RF_STORE_ID_MAX + 1];
    uint32_t size = 0, crc = 0;
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk);
    CHECK(rf_store_ack(store, id, size, crc, keep) == RfStoreOk);
}

static void test_low_space(void) {
    fake_reset();
    RfStore* store = boot();
    char id[32];
    for(uint32_t i = 0; i < 20; i++) {
        snprintf(id, sizeof(id), "up-%lu", (unsigned long)i);
        save_ok(store, id, i);
        ack_first(store, true);
    }
    CHECK(rf_store_pending(store) == 0 && rf_store_stored(store) == 20);
    size_t length = build("new-1", 100, 24, 400);
    fake_set_base_free(fake_used_bytes() + RF_STORE_RESERVE_BYTES + length - 1);
    rf_store_refresh_free(store);
    /* Uploaded copies (already safe on the PC) make room for new evidence. */
    CHECK(rf_store_save(store, "new-1", record_text, length) == RfStoreOk);
    CHECK(rf_store_pending(store) == 1);
    CHECK(rf_store_stored(store) < 21 && rf_store_stored(store) >= 5);
    CHECK(fake_files_in(RF_STORE_UPLOADED_DIR) == rf_store_stored(store) - 1);
    /* Nothing left that may be deleted: the save is refused, pending stays. */
    fake_set_base_free(fake_used_bytes() + RF_STORE_RESERVE_BYTES / 2);
    rf_store_refresh_free(store);
    length = build("new-2", 101, 24, 400);
    CHECK(rf_store_save(store, "new-2", record_text, length) == RfStoreErrLowSpace);
    CHECK(!has_file(RF_STORE_EVENTS_DIR, "new-2", ".json"));
    CHECK(has_file(RF_STORE_EVENTS_DIR, "new-1", ".json"));
    CHECK(rf_store_pending(store) == 1);
    fake_set_base_free(1024ULL * 1024ULL * 1024ULL);
    rf_store_refresh_free(store);
    CHECK(rf_store_save(store, "new-2", record_text, length) == RfStoreOk);
    rf_store_free(store);
}

static void test_uploaded_cap(void) {
    fake_reset();
    RfStore* store = boot();
    char id[32];
    for(uint32_t i = 0; i < RF_STORE_UPLOADED_MAX + 8U; i++) {
        snprintf(id, sizeof(id), "cap-%lu", (unsigned long)i);
        size_t length = build(id, i, 4, 300);
        CHECK(rf_store_save(store, id, record_text, length) == RfStoreOk);
        ack_first(store, true);
    }
    CHECK(fake_files_in(RF_STORE_UPLOADED_DIR) == RF_STORE_UPLOADED_MAX);
    CHECK(rf_store_pending(store) == 0 && rf_store_stored(store) == RF_STORE_UPLOADED_MAX);
    power_cycle(&store);
    CHECK(rf_store_stored(store) == RF_STORE_UPLOADED_MAX);
    rf_store_free(store);
}

/* Send `length` bytes of record_text as RW chunks of `chunk` bytes; returns the last *received. */
static uint32_t put_all(RfStore* store, const char* id, size_t length, size_t chunk) {
    uint32_t received = 0;
    for(size_t offset = 0; offset < length; offset += chunk) {
        size_t n = length - offset < chunk ? length - offset : chunk;
        CHECK(
            rf_store_put_write(
                store, id, (uint32_t)offset, (const uint8_t*)record_text + offset, n, &received) ==
            RfStoreOk);
        CHECK(received == offset + n);
    }
    return received;
}

static void test_carry(void) {
    static const char* const pc_a = "aaaaaaaa11111111";
    static const char* const pc_b = "bbbbbbbb22222222";
    char id[RF_STORE_ID_MAX + 1];
    uint32_t size = 0, crc = 0, received = 0;
    fake_reset();
    RfStore* store = boot();
    CHECK(!rf_store_set_peer(store, "XYZ") && !rf_store_set_peer(store, "abc"));
    CHECK(!rf_store_set_peer(store, "aaaaaaaa111111112")); /* 17 digits */
    save_ok(store, "ev-1", 1);
    uint32_t length = (uint32_t)build("c-1", 7, 40, 500);
    uint32_t record_crc = rf_store_crc32(0, record_text, length);
    /* carrying needs to know the PC */
    CHECK(rf_store_put_begin(store, "c-1", length, record_crc) == RfStoreErrNoPeer);
    CHECK(rf_store_set_peer(store, pc_a));
    CHECK(rf_store_put_begin(store, "c-1", RF_STORE_RECORD_MAX + 1U, record_crc) == RfStoreErrInvalid);
    CHECK(rf_store_put_begin(store, "c-1", length, record_crc) == RfStoreOk);
    /* a gap and a repeat change nothing: the PC continues from *received */
    CHECK(
        rf_store_put_write(store, "c-1", 100, (const uint8_t*)record_text + 100, 50, &received) ==
        RfStoreOk);
    CHECK(received == 0);
    CHECK(rf_store_put_write(store, "c-1", 0, (const uint8_t*)record_text, 100, &received) == RfStoreOk);
    CHECK(received == 100);
    CHECK(rf_store_put_write(store, "c-1", 0, (const uint8_t*)record_text, 100, &received) == RfStoreOk);
    CHECK(received == 100);
    CHECK(
        rf_store_put_write(store, "other", 100, (const uint8_t*)record_text, 10, &received) ==
        RfStoreErrNotFound);
    for(size_t offset = 100; offset < length; offset += 180) {
        size_t n = length - offset < 180 ? length - offset : 180;
        CHECK(
            rf_store_put_write(
                store, "c-1", (uint32_t)offset, (const uint8_t*)record_text + offset, n, &received) ==
            RfStoreOk);
    }
    CHECK(received == length);
    char carry_path[160];
    snprintf(carry_path, sizeof(carry_path), "%s/%s", RF_STORE_CARRY_DIR, pc_a);
    CHECK(file_equals(carry_path, "c-1", record_text, length));
    CHECK(rf_store_carry(store) == 1 && rf_store_pending(store) == 1);
    CHECK(rf_store_listed(store) == 1); /* never listed back to the PC that brought it */
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk && !strcmp(id, "ev-1"));
    CHECK(rf_store_list(store, 1, id, sizeof(id), &size, &crc) == RfStoreErrNotFound);
    /* the same record again, or one pending here: already present */
    CHECK(rf_store_put_begin(store, "c-1", length, record_crc) == RfStoreErrExists);
    CHECK(rf_store_put_begin(store, "c-1", length, record_crc ^ 1U) == RfStoreErrMismatch);
    uint32_t ev_length = (uint32_t)build("ev-1", 1, 24, 400);
    CHECK(
        rf_store_put_begin(store, "ev-1", ev_length, rf_store_crc32(0, record_text, ev_length)) ==
        RfStoreErrExists);
    /* a wrong checksum at the end drops the copy */
    length = (uint32_t)build("c-bad", 8, 10, 300);
    CHECK(rf_store_put_begin(store, "c-bad", length, rf_store_crc32(0, record_text, length) ^ 1U) == RfStoreOk);
    for(size_t offset = 0; offset < length; offset += 180) {
        size_t n = length - offset < 180 ? length - offset : 180;
        RfStoreResult result = rf_store_put_write(
            store, "c-bad", (uint32_t)offset, (const uint8_t*)record_text + offset, n, &received);
        CHECK(offset + n < length ? result == RfStoreOk : result == RfStoreErrMismatch);
    }
    CHECK(!has_file(carry_path, "c-bad", ".json") && rf_store_carry(store) == 1);
    /* the PC goes quiet in the middle of a record: the half copy is removed */
    length = (uint32_t)build("c-2", 9, 40, 500);
    CHECK(rf_store_put_begin(store, "c-2", length, rf_store_crc32(0, record_text, length)) == RfStoreOk);
    CHECK(rf_store_put_write(store, "c-2", 0, (const uint8_t*)record_text, 100, &received) == RfStoreOk);
    rf_store_idle(store);
    CHECK(!has_file(carry_path, "c-2", ".json") && rf_store_carry(store) == 1);
    CHECK(fake_open_handles() == 0);
    /* power fails in the middle of a record: the torn copy survives the reboot ... */
    length = (uint32_t)build("c-3", 10, 40, 500);
    CHECK(rf_store_put_begin(store, "c-3", length, rf_store_crc32(0, record_text, length)) == RfStoreOk);
    CHECK(rf_store_put_write(store, "c-3", 0, (const uint8_t*)record_text, 100, &received) == RfStoreOk);
    fake_power_cut_after(10);
    CHECK(
        rf_store_put_write(store, "c-3", 100, (const uint8_t*)record_text + 100, 100, &received) ==
        RfStoreErrIo);
    power_cycle(&store);
    CHECK(has_file(carry_path, "c-3", ".json") && rf_store_carry(store) == 2);
    /* ... and is deleted, not served, when another PC lists the carried records */
    CHECK(rf_store_listed(store) == 1); /* nobody identified: no carried records */
    CHECK(rf_store_set_peer(store, pc_b));
    CHECK(rf_store_listed(store) == 3);
    CHECK(rf_store_list(store, 0, id, sizeof(id), &size, &crc) == RfStoreOk && !strcmp(id, "ev-1"));
    uint32_t c1_size = 0, c1_crc = 0;
    CHECK(rf_store_list(store, 1, id, sizeof(id), &c1_size, &c1_crc) == RfStoreOk);
    CHECK(!strcmp(id, "c-1") && c1_crc == record_crc);
    CHECK(rf_store_list(store, 2, id, sizeof(id), &size, &crc) == RfStoreErrNotFound);
    CHECK(!has_file(carry_path, "c-3", ".json") && rf_store_carry(store) == 1);
    CHECK(rf_store_listed(store) == 2);
    /* the other PC reads and acknowledges it like a pending record (kept copy in uploaded/) */
    uint8_t chunk[RF_PROTO_CHUNK];
    size_t got = 0;
    CHECK(rf_store_read(store, "c-1", 0, chunk, sizeof(chunk), &got, &size) == RfStoreOk);
    CHECK(got == sizeof(chunk) && size == c1_size);
    CHECK(rf_store_ack(store, "c-1", c1_size, c1_crc, true) == RfStoreOk);
    CHECK(!has_file(carry_path, "c-1", ".json") && has_file(RF_STORE_UPLOADED_DIR, "c-1", ".json"));
    CHECK(rf_store_carry(store) == 0 && rf_store_listed(store) == 1);
    CHECK(rf_store_ack(store, "c-1", c1_size, c1_crc, true) == RfStoreOk); /* a repeated ACK */
    /* the PC that brought it can bring it again (uploaded/ is history, not presence) */
    CHECK(rf_store_set_peer(store, pc_a));
    length = (uint32_t)build("c-1", 7, 40, 500);
    CHECK(rf_store_put_begin(store, "c-1", length, record_crc) == RfStoreOk);
    CHECK(put_all(store, "c-1", length, 150) == length);
    CHECK(rf_store_carry(store) == 1 && rf_store_listed(store) == 1);
    /* forgetting the PC hides carried records again */
    CHECK(rf_store_set_peer(store, ""));
    CHECK(rf_store_listed(store) == 1 && rf_store_carry(store) == 1);
    CHECK(rf_store_put_begin(store, "c-9", length, record_crc) == RfStoreErrNoPeer);
    rf_store_free(store);
    CHECK(fake_open_handles() == 0);
}

static int run_store_tests(void) {
    test_crc_and_ids();
    test_round_trip();
    test_torn_write();
    test_interrupted_move();
    test_quarantine_and_names();
    test_low_space();
    test_uploaded_cap();
    test_carry();
    puts("RF store tests passed");
    return 0;
}

/* ------------------------------------------------------------------ records */

static void print_case(const char* name, size_t length, uint32_t written, uint32_t pulse_count) {
    printf(
        "#case %s %lu %lu %lu\n",
        name,
        (unsigned long)length,
        (unsigned long)written,
        (unsigned long)pulse_count);
    fputs(length ? record_text : "\n", stdout);
}

static int run_records(void) {
    RfSubGhzRecord record;
    uint32_t written = 0;
    size_t length;

    length = build("rf-0123456789abcdef-s00112233445566-1", 1, 20, 400);
    print_case("typical", length, 20, 20);

    for(uint32_t i = 0; i < 512; i++) pulses[i] = 0x7FFFFFFFUL;
    memset(&record, 0, sizeof(record));
    record.common.event_id = "rf-0123456789abcdef-s00112233445566-4294967295";
    record.common.device_id = "0123456789abcdef";
    record.common.session_id = "s00112233445566";
    record.common.sequence = 4294967295U;
    record.common.rtc_local_unix = 4294967295U;
    record.common.tz_offset_minutes = -840;
    record.common.monotonic_ms = 4294967295U;
    record.common.battery_pct = 100;
    record.mode = "CAPTURE";
    record.frequency_hz = 868350000U;
    record.duration_us = 4294967295U;
    record.fingerprint = 0xffffffffU;
    record.follow = true;
    record.follow_fingerprint = 0xffffffffU;
    record.follow_similarity = 1.0f;
    record.rssi_min_dbm = -138.0f;
    record.rssi_avg_dbm = -100.25f;
    record.rssi_max_dbm = -10.5f;
    record.pulse_count = 4294967295U;
    record.last_duration_us = 4294967295U;
    record.timings = pulses;
    record.timing_count = 512;
    length = rf_record_subghz(record_text, sizeof(record_text), &record, &written);
    print_case("worst", length, written, record.pulse_count);

    for(uint32_t i = 0; i < 512; i++) pulses[i] = (i & 1U) ? 380U : 1140U;
    record.common.rtc_local_unix = 1791273672U;
    record.common.tz_offset_minutes = 330;
    record.common.sequence = 7;
    record.common.event_id = "rf-0123456789abcdef-s00112233445566-7";
    record.common.monotonic_ms = 99;
    record.mode = "FOLLOW";
    record.follow_similarity = 0.876f;
    record.pulse_count = 600;
    length = rf_record_subghz(record_text, sizeof(record_text), &record, &written);
    print_case("follow512", length, written, record.pulse_count);

    record.timings = NULL;
    record.timing_count = 0;
    record.pulse_count = 0;
    record.follow = false;
    record.follow_similarity = 0.0f;
    record.mode = "SCOUT";
    record.common.rtc_local_unix = 100;
    record.common.tz_offset_minutes = 840;
    record.common.event_id = "rf-0123456789abcdef-s00112233445566-8";
    record.common.sequence = 8;
    length = rf_record_subghz(record_text, sizeof(record_text), &record, &written);
    print_case("carrier", length, written, 0);

    RfNfcRecord nfc;
    memset(&nfc, 0, sizeof(nfc));
    nfc.common.event_id = "rf-0123456789abcdef-s00112233445566-9";
    nfc.common.device_id = "0123456789abcdef";
    nfc.common.session_id = "s00112233445566";
    nfc.common.sequence = 9;
    nfc.common.rtc_local_unix = 1791273672U;
    nfc.common.tz_offset_minutes = -300;
    nfc.common.monotonic_ms = 5000;
    nfc.common.battery_pct = 50;
    nfc.duration_ms = 4294967295U;
    nfc.field_count = 3;
    length = rf_record_nfc(record_text, sizeof(record_text), &nfc);
    print_case("nfc", length, 0, 0);

    length = rf_record_subghz(record_text, 64, &record, &written);
    print_case("tiny", length, written, 0);
    return 0;
}

/* ------------------------------------------------------------------ protocol REPL */

static bool keep_uploaded = false;

static void reply_line(const char* line, void* context) {
    (void)context;
    if(strlen(line) > RF_PROTO_LINE_MAX - 1U || strchr(line, '\n')) {
        printf("#overlong %lu\n", (unsigned long)strlen(line));
    } else {
        printf("%s\n", line);
    }
    fflush(stdout);
}

static void seed_proto_records(RfStore* store, const char* session) {
    char id[RF_STORE_ID_MAX + 1];
    size_t length;
    snprintf(id, sizeof(id), "rf-%s-%s-1", rf_store_device_id(store), session);
    length = build(id, 1, 40, 350);
    CHECK(rf_store_save(store, id, record_text, length) == RfStoreOk);

    for(uint32_t i = 0; i < 512; i++) pulses[i] = 1000000U + i * 7919U;
    RfSubGhzRecord record;
    memset(&record, 0, sizeof(record));
    snprintf(id, sizeof(id), "rf-%s-%s-2", rf_store_device_id(store), session);
    record.common.event_id = id;
    record.common.device_id = rf_store_device_id(store);
    record.common.session_id = session;
    record.common.sequence = 2;
    record.common.rtc_local_unix = 1791273672U;
    record.common.tz_offset_minutes = 120;
    record.mode = "CAPTURE";
    record.frequency_hz = 315000000U;
    record.rssi_min_dbm = -90.0f;
    record.rssi_avg_dbm = -70.0f;
    record.rssi_max_dbm = -50.0f;
    record.pulse_count = 512;
    record.timings = pulses;
    record.timing_count = 512;
    length = rf_record_subghz(record_text, sizeof(record_text), &record, NULL);
    CHECK(length > 3000 && rf_store_save(store, id, record_text, length) == RfStoreOk);

    RfNfcRecord nfc;
    memset(&nfc, 0, sizeof(nfc));
    snprintf(id, sizeof(id), "rf-%s-%s-3", rf_store_device_id(store), session);
    nfc.common.event_id = id;
    nfc.common.device_id = rf_store_device_id(store);
    nfc.common.session_id = session;
    nfc.common.sequence = 3;
    nfc.common.rtc_local_unix = 1791273700U;
    nfc.duration_ms = 1500;
    nfc.field_count = 1;
    length = rf_record_nfc(record_text, sizeof(record_text), &nfc);
    CHECK(rf_store_save(store, id, record_text, length) == RfStoreOk);

    snprintf(id, sizeof(id), "rf-%s-%s-4", rf_store_device_id(store), session);
    length = build(id, 4, 0, 0);
    CHECK(rf_store_save(store, id, record_text, length) == RfStoreOk);
}

static int run_proto(void) {
    fake_reset();
    RfStore* store = boot();
    seed_proto_records(store, "s00112233445566");
    static char scratch[RF_PROTO_LINE_MAX];
    static char line[1024];
    printf("#ready %lu %s\n", (unsigned long)rf_store_pending(store), rf_store_device_id(store));
    fflush(stdout);
    while(fgets(line, sizeof(line), stdin)) {
        size_t n = strlen(line);
        while(n && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = '\0';
        if(line[0] == '#') {
            if(strncmp(line, "#keep ", 6) == 0) {
                keep_uploaded = line[6] == '1';
                puts("#ok");
            } else if(strcmp(line, "#carry") == 0) {
                printf(
                    "#carry %lu %lu\n",
                    (unsigned long)rf_store_carry(store),
                    (unsigned long)rf_store_listed(store));
            } else if(strcmp(line, "#idle") == 0) {
                rf_store_idle(store);
                puts("#ok");
            } else if(strcmp(line, "#stats") == 0) {
                printf(
                    "#stats %lu %lu\n",
                    (unsigned long)rf_store_pending(store),
                    (unsigned long)rf_store_stored(store));
            } else if(strncmp(line, "#uploaded ", 10) == 0) {
                char path[200];
                snprintf(path, sizeof(path), "%s/%s.json", RF_STORE_UPLOADED_DIR, line + 10);
                const unsigned char* data = NULL;
                size_t size = 0;
                if(fake_get(path, &data, &size)) {
                    printf("#file ");
                    for(size_t i = 0; i < size; i++) printf("%02x", data[i]);
                    printf("\n");
                } else {
                    puts("#nofile");
                }
            } else if(strncmp(line, "#seed ", 6) == 0 && strlen(line + 6) == 15) {
                seed_proto_records(store, line + 6);
                puts("#ok");
            } else if(strcmp(line, "#reboot") == 0) {
                power_cycle(&store);
                puts("#ok");
            } else if(strcmp(line, "#quit") == 0) {
                break;
            } else {
                puts("#unknown");
            }
            fflush(stdout);
            continue;
        }
        rf_proto_handle(store, line, keep_uploaded, scratch, reply_line, NULL);
    }
    rf_store_free(store);
    return 0;
}

int main(int argc, char** argv) {
    if(argc > 1 && strcmp(argv[1], "record") == 0) return run_records();
    if(argc > 1 && strcmp(argv[1], "proto") == 0) return run_proto();
    return run_store_tests();
}
