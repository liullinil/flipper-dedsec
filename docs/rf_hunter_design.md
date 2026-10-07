# RF Hunter — design and protocol

RF Hunter is an **RF tab of the DedSec Uplink app** (`apps/dedsec_uplink`) and an **RF analyzer window
of the companion** (`uplink/`). It has no BLE profile or desktop BLE client of its own: records travel
over the Uplink BLE link, imported by the companion. The product goals are in
[rf_hunter_spec.md](rf_hunter_spec.md); this file is the contract between the parts.

Passive only: never call any Sub-GHz TX, NFC poller/listener/field-on API.

## 1. Flipper: RF engine module (`apps/dedsec_uplink/rf_engine.h`)

Implemented in `rf_engine.c` (+ `rf_store`, `rf_capture`, `rf_record`, `rf_proto`; see
[RF_ENGINE.md](../apps/dedsec_uplink/RF_ENGINE.md)). `uplink.c` owns the UI and the
BLE link and talks to the engine only through this header. The engine owns a worker thread; every
Sub-GHz/NFC HAL call and every journal write happens on that thread. Public functions are callable
from any app thread (they post to the worker's queue or copy a mutex-protected snapshot).

```c
typedef enum { RfModeScout, RfModeCapture, RfModeFollow, RfModeNfc, RfModeCount } RfMode;
typedef enum { RfBandAll, RfBand433, RfBand315, RfBand868, RfBandCount } RfBand;

typedef struct {
    uint8_t band;               // RfBand
    int8_t rssi_threshold_dbm;  // e.g. -75; bursts below are ignored
    uint16_t dwell_ms;          // Scout hop interval per frequency
    uint16_t capture_ms;        // longest capture window
    uint16_t silence_us;        // gap that closes a burst
    bool feedback;              // short vibro + LED blink on events (rate limited)
    bool geiger;                // Follow: speaker clicks that quicken as the live RSSI rises
    bool keep_uploaded;         // after an ACK move the record to uploaded/ instead of deleting it
    int16_t tz_offset_minutes;  // RTC local time minus UTC (sent by the PC, see Z| below)
} RfConfig;

typedef struct {
    RfMode mode;
    bool running;               // receiver / field detector active
    uint32_t frequency_hz;      // current tuning (0 in NFC mode)
    uint32_t events;            // events recorded since the engine started
    uint32_t families;          // distinct local fingerprints since start
    uint32_t unseen;            // events since the last rf_engine_mark_seen()
    uint32_t pending;           // records waiting for upload (files in events/)
    uint32_t errors;            // failed journal writes since start
    uint32_t free_kb;           // SD free space
    bool storage_full;
    uint32_t last_unix;         // UTC time of the last event (0 = none)
    uint32_t last_frequency_hz;
    int16_t last_rssi_dbm;
    uint32_t last_duration_us;
    bool follow_valid;          // Follow has a profile (from the last event)
    uint8_t last_similarity;    // Follow match of the last event, 0..100
    bool nfc_field;             // NFC field present right now
    char last_label[20];        // what the last event was: "KeeLoq 66b", "OOK 25sym", "carrier"
    char last_info[32];         // "sn 0ABCDEF btn 2", "+23.4C 45% ch1" (rf_decode.h)
    bool last_rolling;
    char follow_label[20];      // the Follow profile's label
    int16_t live_rssi_dbm;      // Sub-GHz RX: the latest RSSI sample (the Geiger meter)
    int16_t peak_rssi_dbm;      // peak hold
    int16_t floor_dbm;          // noise floor of the current frequency
    uint8_t geiger_rate;        // clicks per second right now
    bool geiger_sound;          // the speaker is ours and clicking
} RfStatus;

typedef struct RfEngine RfEngine;
typedef void (*RfReplyCallback)(const char* line, void* context); // one protocol line, no '\n'
typedef void (*RfChangedCallback)(void* context);                 // status changed / new event

RfEngine* rf_engine_alloc(RfReplyCallback reply, RfChangedCallback changed, void* context);
void rf_engine_free(RfEngine* engine);             // stops the radio, releases HAL, joins the thread
void rf_engine_configure(RfEngine* engine, const RfConfig* config);
void rf_engine_set_mode(RfEngine* engine, RfMode mode);
void rf_engine_start(RfEngine* engine);            // receiver/detector on (current mode)
void rf_engine_stop(RfEngine* engine);
void rf_engine_mark_seen(RfEngine* engine);        // user looked at the RF tab: unseen = 0
void rf_engine_get_status(RfEngine* engine, RfStatus* out);
void rf_engine_request(RfEngine* engine, const char* line); // a PC line starting with "R" (see §2)
void rf_engine_status_line(RfEngine* engine, char* out, size_t size); // formats the R| line
```

Callbacks run on the engine thread; `uplink.c` queues reply lines and sends them from its own thread.
Journal folder: `EXT_PATH("apps_data/dedsec_uplink/rf")` with `events/`, `uploaded/` and
`carry/<pc_id>/` (absolute paths, never `/data`, because `/data` resolves per calling thread).
`RfStatus` also has `carry` (records a PC left here for another PC) and `listed` (what the PC on the
link can import: pending plus the records other PCs carried here).

## 2. Protocol over the Uplink link

Text lines, `|`-separated. Flipper → PC lines are BLE notifications of at most 243 bytes including `\n`.

PC → Flipper:

```
RO|pc_id                    who the PC is (8-16 hex digits); sent before every round and every push
RL|cursor                   list: the record at index `cursor` (0 = first) of the pending records,
                            then of the records other PCs carried here
RR|event_id|offset          read up to 120 bytes of a record from `offset` (the PC may pipeline up to 4)
RA|event_id|size|crc32      acknowledge a durably imported record (size and crc32 must match)
RP|event_id|size|crc32      carry: the PC is about to put one of its records on the Flipper
RW|event_id|offset|base64   carry: the next bytes of that record, n <= 180, in order (up to 4 in flight)
Z|utc_unix|tz_offset_min    PC clock: UTC seconds and the PC's local offset (for RF timestamps)
```

Flipper → PC:

```
R|listed|stored|free_kb|state|errors|carry   RF journal status (link up, when it changes, every 10 s)
                                          listed: what this PC can import; state: 0 off,
                                          1 Sub-GHz RX, 2 NFC detect; carry: records carried here
RO|listed|carry                           reply to RO
RI|next|event_id|size|crc32              reply to RL; `next` is the cursor of the following record
RE                                        reply to RL: no record at that cursor
RD|event_id|offset|base64                 reply to RR: bytes [offset, offset + n), n <= 120
RK|event_id                               reply to RA: record moved to uploaded/ or deleted
RG|event_id|received                      reply to RP (0) and to every RW: bytes stored so far;
                                          received == size: verified and committed
RH|event_id                               reply to RP: the Flipper already holds this record
RX|code|text                              a request failed: nf (not found), bad (malformed/mismatch),
                                          io (SD error), off (RF sync disabled on the Flipper);
                                          the text starts with the event id when there is one
```

Sync loop (PC): `RO|pc_id`, then `cursor = 0`; `RL|cursor` → `RE` ends the round; on `RI` read all
bytes with `RR`, check size + crc32 (`binascii.crc32`, unsigned decimal), commit the record durably on
the PC (fsync), then `RA` and wait for `RK` — the record leaves the Flipper, so the cursor stays the
same. If a record cannot be imported, skip it with `cursor = next`. A new round starts whenever
`R|listed` > 0. A Flipper app before 1.3.0 answers `RO` with `RX|bad` and the round goes on.

Carrying (PC → Flipper → another PC): the PC sends `RO`, then for each of its records `RP`; on
`RH` it skips the record, on `RG|id|0` it streams `RW` chunks. The Flipper writes them to
`carry/<pc_id>/<event_id>.json` strictly in order and answers every chunk with the bytes it holds,
so a lost chunk shows up as no progress and the PC sends again from there; at the announced size
it checks the CRC-32 and the JSON shape (`{` … `}\n`) and commits. A record left unfinished (the PC
went quiet for 3 s, or another `RP` came) is deleted. Carried records are listed, read and
acknowledged like pending ones, but only to a PC that sent `RO`, and never to the PC that brought
them; a broken carried copy is deleted instead of quarantined (the PC that brought it still has it).

Records: one UTF-8 JSON object per event, unchanged from the former standalone app
(`schema_version, event_id, device_uuid, session_id, sequence_number, captured_at_utc,
captured_at_unix, timezone_offset_minutes, rtc_local_unix, monotonic_ms, source_type, mode,
frequency_hz, modulation, …, rssi_min_dbm/avg/max, pulse_count, last_duration_us,
pulse_timings_us[], upload_state` and the NFC fields), plus from app 1.5.0 the decode:
`rf_protocol, rf_bits, rf_key, rf_info, rf_frames, rf_identical, rf_te_us, rf_rolling, rf_confidence`
and `first_level` (see [RF_ENGINE.md](../apps/dedsec_uplink/RF_ENGINE.md)); the companion computes the
same for records without them (`uplink/uplink/rf_decode.py`). Event ids: `[A-Za-z0-9][A-Za-z0-9_.-]*`,
at most 48 characters.

## 3. Companion: `uplink/uplink/rf_sync.py`

```python
class RfSync:
    def __init__(self, store_root: str): ...          # EventStore folder, e.g. %LOCALAPPDATA%\DedSecUplink\rf_hunter
    def handle_line(self, parts: list) -> bool: ...   # Flipper line already split on "|"; True if it was an RF line
    def urgent_lines(self) -> list: ...               # lines to send now (polled every 20–50 ms by the link)
    def on_link(self, up: bool) -> None: ...           # link up/down (abort the round on down)
    def sync_now(self) -> None: ...                    # start a round even if pending looks 0
    def push_now(self) -> None: ...                    # carry every record of the store to the Flipper
    def status(self) -> dict: ...                      # {"pending", "stored", "free_kb", "state", "errors",
                                                       #  "carry", "imported", "failed", "last_error",
                                                       #  "syncing", "last_sync", "pushing", "push_total",
                                                       #  "push_done", "push_sent", "push_present",
                                                       #  "push_failed", "push_error", "last_push"}
    store: EventStore                                  # the analyzer reads from the same folder
```

## 4. Companion: analyzer window (`uplink/uplink/rf_analyzer.py`)

`AnalyzerWindow(parent, store_root, sync=None)` builds a `tk.Toplevel` on an existing Tk root that the
companion runs in its single UI thread (the tray panel lives in the same thread). It reloads the
store when the folder changes (poll every few seconds), shows `sync.status()` and offers two buttons:
**FLIPPER → PC** (`sync.sync_now()`) and **FLIPPER ← PC** (`sync.push_now()`); it never opens its own
BLE connection. `main(argv)` keeps the CLI (export,
standalone viewer).
