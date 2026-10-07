/* Scenarios for the real rf_engine.c in the deterministic fake world
 * (engine_fake.c).  Every record the engine writes is printed after a
 * "#record <scenario>" line so the Python test can json.loads it. */
#include <furi.h>
#include <storage/storage.h>
#include "engine_fake.h"
#include "rf_engine.h"
#include "rf_store.h"
#include "storage_fake.h"

#define CHECK(condition)                                                                   \
    do {                                                                                   \
        if(!(condition)) {                                                                 \
            fprintf(                                                                       \
                stderr,                                                                    \
                "CHECK failed at %s:%d (t=%lu ms): %s\n",                                  \
                __FILE__,                                                                  \
                __LINE__,                                                                  \
                (unsigned long)world_now(),                                                \
                #condition);                                                               \
            exit(1);                                                                       \
        }                                                                                  \
    } while(0)

#define MAX_RECORDS 16
#define F315        315000000U
#define F433        433920000U
#define F868        868350000U
#define DAY0        1791244800U /* 2026-10-06 00:00:00 as an epoch */

typedef struct {
    char text[4200];
    uint32_t pulses[600];
    uint32_t pulse_total;
} Record;

static RfEngine* engine;
static Record records[MAX_RECORDS];
static uint32_t record_count;
static const char* scenario = "";

static RfConfig config_for(uint8_t band) {
    RfConfig config;
    memset(&config, 0, sizeof(config));
    config.band = band;
    config.rssi_threshold_dbm = -75;
    config.dwell_ms = 250;
    config.capture_ms = 1000;
    config.silence_us = 8000;
    config.feedback = true;
    config.keep_uploaded = false;
    config.tz_offset_minutes = 180;
    return config;
}

static void begin(const char* name, uint8_t band, RfMode mode, bool run) {
    scenario = name;
    fake_reset();
    world_reset();
    engine = rf_engine_alloc(NULL, NULL, NULL);
    RfConfig config = config_for(band);
    rf_engine_configure(engine, &config);
    rf_engine_set_mode(engine, mode);
    if(run) rf_engine_start(engine);
}

static uint32_t json_u32(const char* text, const char* key) {
    char needle[64];
    snprintf(needle, sizeof(needle), "\"%s\":", key);
    const char* at = strstr(text, needle);
    CHECK(at != NULL);
    return (uint32_t)strtoul(at + strlen(needle), NULL, 10);
}

static float json_float(const char* text, const char* key) {
    char needle[64];
    snprintf(needle, sizeof(needle), "\"%s\":", key);
    const char* at = strstr(text, needle);
    CHECK(at != NULL);
    return (float)strtod(at + strlen(needle), NULL);
}

static bool json_has(const char* text, const char* fragment) { return strstr(text, fragment) != NULL; }

/* Run the engine thread until the scripted world ends, then load the records. */
static void finish(void) {
    rf_engine_free(engine);
    engine = NULL;
    CHECK(world_radio_asleep());
    CHECK(world_nfc_released());
    CHECK(fake_open_handles() == 0);
    record_count = 0;
    File* dir = storage_file_alloc(&fake_storage);
    CHECK(storage_dir_open(dir, RF_STORE_EVENTS_DIR));
    FileInfo info;
    char name[128];
    while(storage_dir_read(dir, &info, name, sizeof(name))) {
        if(info.flags & FSF_DIRECTORY) continue;
        CHECK(record_count < MAX_RECORDS);
        char path[200];
        snprintf(path, sizeof(path), "%s/%s", RF_STORE_EVENTS_DIR, name);
        const unsigned char* data = NULL;
        size_t size = 0;
        CHECK(fake_get(path, &data, &size) && size < sizeof(records[0].text));
        Record* record = &records[record_count++];
        memcpy(record->text, data, size);
        record->text[size] = '\0';
        record->pulse_total = 0;
        const char* list = strstr(record->text, "\"pulse_timings_us\":[");
        if(list) {
            list += strlen("\"pulse_timings_us\":[");
            while(*list && *list != ']') {
                CHECK(record->pulse_total < 600);
                record->pulses[record->pulse_total++] = (uint32_t)strtoul(list, (char**)&list, 10);
                if(*list == ',') list++;
            }
        }
        printf("#record %s\n%s", scenario, record->text);
    }
    storage_dir_close(dir);
    storage_file_free(dir);
}

static uint32_t count_value(const Record* record, uint32_t value) {
    uint32_t count = 0;
    for(uint32_t i = 0; i < record->pulse_total; i++) count += record->pulses[i] == value;
    return count;
}

static RfStatus status(void) {
    RfStatus value;
    rf_engine_get_status(engine, &value);
    return value;
}

/* ------------------------------------------------------------------ 1: Scout hopping */

static void hop_check(void) {
    RfStatus s = status();
    CHECK(s.running && s.mode == RfModeScout && s.frequency_hz == F433);
    CHECK(s.events == 0 && s.pending == 0);
    char line[64];
    rf_engine_status_line(engine, line, sizeof(line));
    CHECK(strncmp(line, "R|0|0|", 6) == 0 && strstr(line, "|1|0") != NULL);
}

static void hop_stop(void) { rf_engine_stop(engine); }

static void hop_stopped(void) {
    RfStatus s = status();
    CHECK(!s.running);
    char line[64];
    rf_engine_status_line(engine, line, sizeof(line));
    CHECK(strstr(line, "|0|0") != NULL);
}

static void scenario_hopping(void) {
    begin("hopping", RfBandAll, RfModeScout, true);
    world_at(100, hop_check);
    world_at(1110, hop_stop);
    world_at(1200, hop_stopped);
    world_end_at(1300);
    finish();
    const WorldTune* tunes = NULL;
    uint32_t count = world_tunes(&tunes);
    static const uint32_t cycle[] = {F433, F868, F315, F433, F868};
    CHECK(count == 5);
    for(uint32_t i = 0; i < count; i++) {
        CHECK(tunes[i].frequency == cycle[i]);
        CHECK(tunes[i].at_ms == i * 250U);
    }
    CHECK(record_count == 0);
    CHECK(world_feedback(1) == 0 && world_red_blinks() == 0);
}

/* ------------------------------------------------------------------ 2: bursts */

static void bursts_check(void) {
    RfStatus s = status();
    CHECK(s.events == 2 && s.pending == 2 && s.unseen == 2 && s.families == 2);
    CHECK(s.running && s.frequency_hz == F433 && s.last_frequency_hz == F433);
    CHECK(s.last_rssi_dbm == -50 && s.errors == 0 && !s.storage_full);
    CHECK(s.follow_valid && s.last_similarity == 0);
    CHECK(s.last_unix == DAY0 + 5U * 3600U); /* RTC 08:00 local, UTC+3 */
    rf_engine_mark_seen(engine);
}

static void bursts_seen(void) {
    RfStatus s = status();
    CHECK(s.unseen == 0 && s.events == 2);
    char line[64];
    rf_engine_status_line(engine, line, sizeof(line));
    CHECK(strncmp(line, "R|2|2|", 6) == 0 && strstr(line, "|1|0") != NULL);
}

static void scenario_bursts(void) {
    begin("bursts", RfBand433, RfModeScout, true);
    world_add_tx(300, 400, F433, -50.0f, 400, 800);
    world_add_tx(600, 650, F433, -50.0f, 1200, 400);
    world_add_tx(900, 950, F433, -85.0f, 400, 800); /* below the threshold */
    world_add_tx(1100, 1160, F315, -40.0f, 400, 800); /* not tuned */
    world_at(1400, bursts_check);
    world_at(1450, bursts_seen);
    world_end_at(1500);
    finish();
    CHECK(record_count == 2);
    const Record* first = &records[0];
    const Record* second = &records[1];
    /* Event 1: only its own 400/800 pattern, closing gap excluded. */
    CHECK(first->pulse_total >= 160 && first->pulse_total <= 170);
    CHECK(count_value(first, 400) + count_value(first, 800) == first->pulse_total);
    CHECK(first->pulses[0] == 400 && first->pulses[first->pulse_total - 1] == 400);
    CHECK(json_u32(first->text, "pulse_count") == first->pulse_total);
    uint32_t duration = json_u32(first->text, "duration_us");
    CHECK(duration >= 99000 && duration <= 100000);
    CHECK(json_u32(first->text, "frequency_hz") == F433);
    CHECK(json_has(first->text, "\"mode\":\"SCOUT\""));
    CHECK(json_float(first->text, "rssi_max_dbm") == -50.0f);
    CHECK(json_float(first->text, "rssi_min_dbm") <= -96.0f);
    CHECK(json_u32(first->text, "captured_at_unix") == DAY0 + 5U * 3600U);
    CHECK(json_u32(first->text, "rtc_local_unix") == DAY0 + 8U * 3600U);
    CHECK(json_u32(first->text, "monotonic_ms") == 300);
    CHECK(json_u32(first->text, "sequence_number") == 1);
    /* Event 2: the pre-trigger of event 1 never leaks into it. */
    CHECK(count_value(second, 800) == 0);
    CHECK(count_value(second, 1200) + count_value(second, 400) == second->pulse_total);
    CHECK(second->pulses[0] == 1200 && second->pulse_total >= 60);
    CHECK(json_u32(second->text, "pulse_count") == second->pulse_total);
    CHECK(json_u32(second->text, "sequence_number") == 2);
    /* One short pulse: the second burst came within the 1 s rate limit. */
    CHECK(world_feedback(1) == 1 && world_feedback(2) == 0 && world_red_blinks() == 0);
    const WorldTune* tunes = NULL;
    CHECK(world_tunes(&tunes) == 1 && tunes[0].frequency == F433);
}

/* ------------------------------------------------------------------ 3: noise */

static void scenario_noise(void) {
    begin("noise", RfBand433, RfModeCapture, true);
    world_set_noise(0, 2000);
    world_add_tx(500, 600, F433, -50.0f, 400, 800);
    world_end_at(2000);
    finish();
    /* Noise edges alone never trigger; the burst is one event even though
     * the demodulator keeps toggling after it. */
    CHECK(record_count == 1);
    const Record* record = &records[0];
    CHECK(count_value(record, 400) + count_value(record, 800) >= 150);
    CHECK(json_has(record->text, "\"mode\":\"CAPTURE\""));
    CHECK(json_u32(record->text, "monotonic_ms") >= 500 && json_u32(record->text, "monotonic_ms") <= 505);
}

/* ------------------------------------------------------------------ 4: NFC */

static void nfc_switch(void) { rf_engine_set_mode(engine, RfModeNfc); }

static void nfc_check(void) {
    RfStatus s = status();
    CHECK(s.mode == RfModeNfc && s.running && s.frequency_hz == 0 && !s.nfc_field);
    CHECK(s.events == 2 && s.pending == 2 && s.last_frequency_hz == 13560000U);
    char line[64];
    rf_engine_status_line(engine, line, sizeof(line));
    CHECK(strstr(line, "|2|0") != NULL);
}

static void nfc_field_check(void) { CHECK(status().nfc_field); }

static void nfc_back(void) { rf_engine_set_mode(engine, RfModeScout); }

static void nfc_back_check(void) {
    RfStatus s = status();
    CHECK(s.mode == RfModeScout && s.running && s.frequency_hz == F433);
}

static void scenario_nfc(void) {
    begin("nfc", RfBandAll, RfModeScout, true);
    world_add_nfc_field(1000, 1300);
    world_add_nfc_field(1500, 1600); /* gap < 1 s: same event */
    world_add_nfc_field(3000, 3100);
    world_at(200, nfc_switch);
    world_at(1100, nfc_field_check);
    world_at(4500, nfc_check);
    world_at(4600, nfc_back);
    world_at(4700, nfc_back_check);
    world_end_at(4800);
    finish();
    CHECK(record_count == 2);
    uint32_t first = json_u32(records[0].text, "nfc_field_duration_ms");
    uint32_t second = json_u32(records[1].text, "nfc_field_duration_ms");
    CHECK(first >= 600 && first <= 620);
    CHECK(second >= 100 && second <= 120);
    CHECK(json_u32(records[0].text, "nfc_field_count") == 2);
    CHECK(json_u32(records[1].text, "nfc_field_count") == 3);
    CHECK(json_u32(records[0].text, "monotonic_ms") == 1000);
    CHECK(json_has(records[0].text, "\"source_type\":\"nfc\""));
    CHECK(world_feedback(1) == 2);
}

/* ------------------------------------------------------------------ 5: NFC busy */

static void busy_check(void) { CHECK(!status().running); }
static void busy_recovered(void) { CHECK(status().running); }

static void scenario_nfc_busy(void) {
    begin("nfc_busy", RfBandAll, RfModeNfc, true);
    world_nfc_busy(2);
    world_add_nfc_field(2500, 2600);
    world_at(500, busy_check);
    world_at(1500, busy_check);
    world_at(2300, busy_recovered);
    world_end_at(4000);
    finish();
    CHECK(record_count == 1);
}

/* ------------------------------------------------------------------ 6: Follow */

static void follow_on(void) { rf_engine_set_mode(engine, RfModeFollow); }

static void follow_check(void) {
    RfStatus s = status();
    CHECK(s.mode == RfModeFollow && s.follow_valid && s.events == 3);
    CHECK(s.last_similarity >= 70 && s.frequency_hz == F433);
}

static void scenario_follow(void) {
    begin("follow", RfBand433, RfModeCapture, true);
    world_add_tx(300, 400, F433, -50.0f, 400, 800);
    world_add_tx(1500, 1600, F433, -48.0f, 420, 780); /* same family, > 1 s after A's pulse */
    world_add_tx(2500, 2600, F433, -45.0f, 3000, 3000); /* different */
    world_add_tx(4000, 4100, F433, -52.0f, 400, 800); /* same family */
    world_at(600, follow_on);
    world_at(4500, follow_check);
    world_end_at(4700);
    finish();
    CHECK(record_count == 3);
    CHECK(json_has(records[0].text, "\"mode\":\"CAPTURE\""));
    CHECK(json_has(records[0].text, "\"follow_profile_id\":\"\""));
    const char* fingerprint = strstr(records[0].text, "\"fingerprint_id\":\"local-");
    CHECK(fingerprint != NULL);
    char expected[64];
    snprintf(expected, sizeof(expected), "\"follow_profile_id\":\"local-%.8s\"", fingerprint + 24);
    for(uint32_t i = 1; i < 3; i++) {
        CHECK(json_has(records[i].text, "\"mode\":\"FOLLOW\""));
        CHECK(json_has(records[i].text, expected));
        CHECK(json_float(records[i].text, "follow_similarity") >= 0.7f);
        CHECK(count_value(&records[i], 3000) == 0);
    }
    CHECK(world_feedback(1) == 1 && world_feedback(2) == 2);
}

/* ------------------------------------------------------------------ 7: storage full */

static void full_check(void) {
    RfStatus s = status();
    CHECK(s.errors == 1 && s.storage_full && s.events == 0 && s.pending == 0);
    char line[64];
    rf_engine_status_line(engine, line, sizeof(line));
    CHECK(strncmp(line, "R|0|0|", 6) == 0 && strstr(line, "|1|1") != NULL);
    fake_set_base_free(1024ULL * 1024ULL * 1024ULL);
}

static void full_recovered(void) {
    RfStatus s = status();
    CHECK(s.errors == 1 && !s.storage_full && s.events == 1 && s.pending == 1);
}

static void scenario_storage_full(void) {
    begin("storage_full", RfBand433, RfModeCapture, true);
    fake_set_base_free(RF_STORE_RESERVE_BYTES / 2);
    world_add_tx(300, 400, F433, -50.0f, 400, 800);
    world_add_tx(1500, 1600, F433, -50.0f, 400, 800);
    world_at(1000, full_check);
    world_at(1900, full_recovered);
    world_end_at(2000);
    finish();
    CHECK(record_count == 1);
    CHECK(json_u32(records[0].text, "sequence_number") == 2);
    CHECK(world_red_blinks() == 1);
}

/* ------------------------------------------------------------------ 8: windows and gaps */

static void scenario_gaps(void) {
    begin("gaps", RfBand433, RfModeCapture, true);
    /* Weak 150/150 activity right before the trigger: pre-trigger context. */
    world_add_tx(290, 300, F433, -85.0f, 150, 150);
    /* A carrier keyed at 2.5/2.5 ms is strong at every 5 ms sample, so each
     * capture window is followed at once by the next capture. */
    world_add_tx(300, 2600, F433, -50.0f, 2500, 2500);
    /* An 8.4 ms gap (> silence_us, shorter than the 9 ms idle check)
     * separates X from Y: the gap timing itself must close X. */
    world_add_tx(3000, 3050, F433, -50.0f, 400, 800);
    world_add_tx(3058, 3100, F433, -50.0f, 1200, 400);
    world_end_at(3400);
    finish();
    CHECK(record_count == 5);
    CHECK(count_value(&records[0], 150) >= 30 && records[0].pulses[0] == 150);
    CHECK(count_value(&records[0], 2500) >= 390);
    for(uint32_t i = 1; i < 3; i++) {
        /* A capture that starts right after the previous window never
         * inherits the old pre-trigger. */
        CHECK(count_value(&records[i], 150) == 0);
        CHECK(count_value(&records[i], 2500) == records[i].pulse_total);
    }
    CHECK(count_value(&records[3], 400) + count_value(&records[3], 800) == records[3].pulse_total);
    CHECK(records[3].pulse_total >= 80);
    CHECK(count_value(&records[4], 1200) >= 20);
    CHECK(count_value(&records[4], 800) == 0);
}

/* ------------------------------------------------------------------ 9: churn + exit */

static void churn_capture(void) { rf_engine_set_mode(engine, RfModeCapture); }
static void churn_follow(void) { rf_engine_set_mode(engine, RfModeFollow); }
static void churn_nfc(void) { rf_engine_set_mode(engine, RfModeNfc); }
static void churn_scout(void) { rf_engine_set_mode(engine, RfModeScout); }
static void churn_stop(void) { rf_engine_stop(engine); }
static void churn_start(void) { rf_engine_start(engine); }

static void churn_band(void) {
    RfConfig config = config_for(RfBand868);
    config.dwell_ms = 7; /* clamped to 50 */
    config.rssi_threshold_dbm = -20; /* clamped to -30 */
    rf_engine_configure(engine, &config);
}

static void churn_request(void) {
    rf_engine_request(engine, "RL|0");
    rf_engine_request(engine, "RR|nothing|0");
}

static void scenario_churn(void) {
    begin("churn", RfBandAll, RfModeScout, true);
    world_add_tx(700, 3000, F868, -20.0f, 500, 500); /* still on at exit */
    world_at(100, churn_capture);
    world_at(101, churn_follow);
    world_at(102, churn_nfc);
    world_at(103, churn_scout);
    world_at(104, churn_stop);
    world_at(105, churn_start);
    world_at(106, churn_nfc);
    world_at(300, churn_scout);
    world_at(400, churn_band);
    world_at(500, churn_request);
    world_at(600, churn_capture);
    world_end_at(900);
    finish();
    /* The burst still open at exit is recorded at shutdown. */
    CHECK(record_count == 1);
    CHECK(json_u32(records[0].text, "frequency_hz") == F868);
    CHECK(json_has(records[0].text, "\"mode\":\"CAPTURE\""));
    CHECK(world_queue_drops() == 0);
}

int main(void) {
    scenario_hopping();
    scenario_bursts();
    scenario_noise();
    scenario_nfc();
    scenario_nfc_busy();
    scenario_follow();
    scenario_storage_full();
    scenario_gaps();
    scenario_churn();
    puts("RF engine scenarios passed");
    return 0;
}
