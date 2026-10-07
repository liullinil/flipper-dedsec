# RF Signal Hunter — Product and Technical Specification

## 1. Document status

- **Status:** Planning specification
- **Target hardware:** Flipper Zero, using its built-in Sub-GHz and NFC hardware
- **Target software:** A standalone Flipper application (FAP) plus a desktop analysis application
- **Primary runtime mode:** Passive monitoring
- **Transmission policy:** Hunter and Capture modes must never transmit RF or NFC commands
- **Audience:** The developer implementing the Flipper FAP and the desktop application

This document describes the intended product behavior and data model. It is implementation-neutral where the exact Flipper SDK API may vary.

## 2. Product vision

RF Signal Hunter is a portable, passive signal logger and visual analysis system.

The Flipper stays in a backpack and observes configured Sub-GHz activity in the background. When it detects a signal, it immediately gives local feedback, records a detailed capture, and stores the event on the SD card. The user can then walk toward the suspected source and collect more observations.

The desktop application turns the collected observations into an interactive visual investigation:

- an animated waterfall and spectrum view;
- a timeline with exact date and time;
- automatically grouped signal families;
- comparison of similar and different captures;
- an animated view of how observations belong to the same signal family;
- RSSI and time-based evidence that helps the user connect a signal with a physical place.

The system must preserve the distinction between:

1. a single RF transmission;
2. repeated frames belonging to one transmission event;
3. repeated observations of the same source at different times;
4. a retransmission of already uploaded data caused by an interrupted upload.

Only the fourth case is a transport duplicate. The other cases are valuable observations and must remain visible in the desktop application.

## 3. Goals

### 3.1 Primary goals

1. Detect passive Sub-GHz activity while the Flipper is carried in a backpack.
2. Provide immediate local feedback when a signal is detected.
3. Capture enough raw and derived information for later analysis.
4. Store the exact calendar date and time of every event.
5. Automatically group observations that likely belong to the same physical source.
6. Preserve all individual observations, even when they belong to the same group.
7. Periodically upload captured data to a desktop application.
8. Free the Flipper's storage after the desktop application acknowledges a successful upload.
9. Prevent re-importing the same event when an upload is retried.
10. Provide a visually distinctive, high-quality desktop analysis experience.

### 3.2 Secondary goals

1. Support NFC observations on a shared timeline with Sub-GHz events.
2. Allow the user to follow a selected signal family after observing it.
3. Make the system useful without requiring the user to name, merge, or administrate every signal.
4. Allow expert users to inspect and override automatic grouping in the desktop application without making manual correction mandatory.

### 3.3 Non-goals

1. The system is not a full-band software-defined radio.
2. The system does not promise to identify an exact commercial device model from a single observation.
3. The system does not locate a transmitter with GPS-grade accuracy.
4. Hunter and Capture modes do not replay, jam, modify, or transmit captured signals.
5. The system does not discard raw evidence merely because two observations were automatically grouped.

## 4. Hardware constraints

### 4.1 Sub-GHz limitations

The Flipper's Sub-GHz radio is a narrowband transceiver, not a continuous IQ SDR. The implementation must not claim to record an unlimited, continuous spectrum.

The realistic capture product is:

- a sweep over configured frequency ranges;
- RSSI samples from the sweep;
- the detected carrier frequency;
- modulation and receiver parameters when available;
- raw pulse timing or demodulated frame data when available;
- a short pre-trigger and post-trigger capture window;
- repetition and timing information;
- a local spectrum snapshot around the detected signal.

The desktop application may render these measurements as a waterfall. The waterfall is a sampled RF activity view, not raw IQ across the entire radio spectrum.

### 4.2 NFC limitations

NFC is a near-field technology and must be treated as a separate observation source. NFC events are expected to occur only when the Flipper is physically close to a card, tag, phone, or reader.

NFC support should record the detected technology/protocol and available identifiers or protocol features. NFC and Sub-GHz monitoring may be separate operating modes if simultaneous operation is not practical on the hardware.

### 4.3 Time source

Every event must contain an absolute timestamp from the Flipper RTC, including date and time. The event should also contain a monotonic session offset so events remain correctly ordered if the RTC is adjusted.

Recommended fields:

- UTC Unix timestamp;
- displayed/local timezone offset when known;
- local date/time as a convenience field;
- session ID;
- monotonic milliseconds since session start.

The desktop application should normalize timestamps internally and display them in the user's selected timezone.

## 5. User-facing operating modes

### 5.1 Scout mode

Scout is the low-power background monitoring mode intended for carrying the Flipper in a backpack.

Behavior:

1. Sweep configured Sub-GHz bands using a power-conscious schedule.
2. Detect activity above configured thresholds.
3. Create a lightweight event candidate.
4. Attempt a short Capture window for the detected activity.
5. Immediately update counters and local indicators.
6. Continue monitoring without requiring user interaction.

The main Scout screen must show at least:

- number of event bursts detected since the last review;
- number of signal families first seen since the last review;
- number of events currently pending upload;
- current monitoring state;
- battery level;
- selected band profile;
- whether there are unreviewed events.

The counters must distinguish repeated observations from new automatically grouped families. For example:

~~~
Events since review: 129
Signal families since review: 7
Pending upload: 42
~~~

An indicator LED must remain active while there are unreviewed events. The exact LED pattern may distinguish:

- a new event;
- a new signal family;
- an upload pending;
- a Follow match.

Scout must not transmit.

### 5.2 Capture mode

Capture is the detailed recording phase triggered by Scout or started manually.

The feedback path must be immediate:

~~~
RF activity detected
→ vibrate and update indicator immediately
→ record detailed data
→ calculate a preliminary fingerprint
→ store the complete event
~~~

Vibration must not wait for complete analysis or a final classifier decision.

Capture should save, when the hardware and SDK make the data available:

- detected frequency;
- receiver configuration;
- modulation estimate;
- bandwidth estimate;
- RSSI minimum, average, maximum, and peak shape;
- signal start and end time;
- pre-trigger context;
- post-trigger context;
- pulse timing sequence;
- frame length and repetition pattern;
- raw demodulated payload or raw timing data;
- local sweep/spectrum snapshot;
- battery and firmware/application version;
- provisional classification and confidence.

The capture window must be configurable. The implementation should use a ring buffer so the beginning of a burst is not lost while the trigger is being processed.

### 5.3 Follow mode

Follow monitors one selected signal family with a tighter trigger condition.

The user should be able to select the last observed family with one action. Naming and manual profile administration are optional.

Follow behavior:

1. Monitor the frequency and structural fingerprint of the selected family.
2. Ignore unrelated activity as much as possible.
3. Trigger vibration and indicator output when a sufficiently similar observation is detected.
4. Save the event like any other Capture.
5. Record the similarity score and the matching profile ID.

Follow is intended for the physical search workflow:

1. Scout detects an interesting signal.
2. Capture gives immediate feedback and records it.
3. The user starts Follow for that last observed family.
4. The user walks toward the suspected source.
5. The Flipper gives feedback when the family is detected again.

Follow must not claim to provide direction finding. RSSI trends can help the user search, but one Flipper antenna cannot provide reliable bearing information.

### 5.4 NFC observation mode

NFC observations should use the same event model and timeline as Sub-GHz events.

An NFC event may include:

- event timestamp;
- NFC technology/protocol;
- available UID or identifier;
- modulation/technology details exposed by the NFC stack;
- response timing;
- raw protocol exchange when available and permitted by the API;
- confidence and classification;
- source type estimate such as tag, card, phone, or reader.

## 6. Event and signal terminology

The implementation must use precise terms:

- **Frame:** A single decoded or demodulated packet.
- **Burst:** A short period of RF activity containing one or more frames.
- **Event:** A stored observation of a burst, with timestamp and capture data.
- **Fingerprint:** Stable measurable features extracted from one event.
- **Signal family:** A group of events with similar fingerprints.
- **Source hypothesis:** A human-readable interpretation of a family, such as remote, gate/barrier, sensor, or unknown.
- **Transport duplicate:** The same event uploaded more than once because an upload was retried.

An event may be assigned to a signal family even if its payload differs. This is required for devices using changing counters or rolling codes.

## 7. Fingerprinting and automatic grouping

The purpose of automatic grouping is to make the desktop investigation understandable, not to hide evidence.

### 7.1 Stable features

The fingerprint should favor features that remain stable for one transmitter:

- carrier frequency and tolerated frequency drift;
- modulation family;
- approximate bandwidth;
- pulse-width and gap distributions;
- preamble structure;
- frame length range;
- number and spacing of repeated frames;
- burst duration;
- timing ratios;
- spectral shape available from the sweep;
- RSSI range as a supporting feature, not a device identity feature.

Changing payload bits must not be the only basis for deciding that two events are different.

### 7.2 Grouping behavior

The grouping engine should:

1. Create a new family when no existing family is sufficiently similar.
2. Add an event to an existing family when similarity is high.
3. Keep uncertain events as separate provisional families rather than forcing a merge.
4. Merge provisional families automatically after enough supporting observations.
5. Split a family automatically if later evidence shows two distinct structures.
6. Keep a confidence score and the features responsible for the match.
7. Preserve every event and its original fingerprint regardless of later regrouping.

The Flipper should perform lightweight grouping for local counters and Follow. The desktop application should perform the authoritative, more detailed grouping because it has more processing power and access to the complete history.

### 7.3 Displaying groups

The desktop application should distinguish:

- same exact event;
- same structural signal family;
- related family with variable payload;
- weak similarity;
- unknown.

Example:

~~~
Signal Family 07
433.920 MHz · OOK
47 observations
12 unique waveform variants
Payload: variable
Similarity confidence: 94%
~~~

## 8. Data model

### 8.1 Device identity

Each Flipper installation must have a stable device UUID. The UUID must not change on every boot.

### 8.2 Session identity

Each monitoring run must have a session ID. A new session is created after boot, application restart, or explicit session reset.

### 8.3 Event identity

Each event must have a unique immutable event ID. A recommended construction is:

~~~
event_id = hash(device_uuid + session_id + sequence_number)
~~~

The raw fields should also be stored so the ID can be audited.

Required identity fields:

- device UUID;
- session ID;
- sequence number;
- event ID.

### 8.4 Event record

Recommended event record:

~~~json
{
  "event_id": "stable-unique-id",
  "device_uuid": "flipper-device-id",
  "session_id": "session-id",
  "sequence_number": 1842,
  "captured_at_utc": "2026-10-06T08:01:12.431Z",
  "timezone_offset_minutes": 180,
  "monotonic_ms": 912341,
  "source_type": "subghz",
  "frequency_hz": 433920000,
  "modulation": "OOK",
  "bandwidth_hz": 12000,
  "rssi_min_dbm": -71,
  "rssi_avg_dbm": -53,
  "rssi_max_dbm": -44,
  "duration_us": 12800,
  "repeat_count": 3,
  "fingerprint_id": "local-fingerprint-id",
  "family_id": "desktop-family-id-or-null",
  "classification": "unknown",
  "classification_confidence": 0.0,
  "capture_blob": "path-or-binary-reference",
  "upload_state": "pending"
}
~~~

The exact serialization format may be binary on the Flipper for space efficiency. The desktop export must provide a documented, versioned format. JSON metadata plus binary capture blobs is acceptable.

## 9. Upload, acknowledgement, and storage reclamation

The upload protocol must handle interruptions safely and must not depend on filenames alone.

### 9.1 Upload flow

1. The desktop application requests the pending event manifest.
2. Flipper sends metadata and capture data in chunks.
3. The desktop application validates each event ID and stores the chunk.
4. The desktop application sends an acknowledgement for successfully committed event IDs.
5. Flipper marks acknowledged events as uploaded.
6. Flipper deletes the large raw capture blobs only after acknowledgement.
7. Flipper retains a compact index of uploaded event IDs and local fingerprints.

If the connection fails before acknowledgement, the event remains pending and may be sent again. The desktop application must treat a repeated event ID as the same event and must not create a second copy.

### 9.2 Storage after upload

After successful upload, the Flipper should retain only lightweight data needed for local operation:

- event ID;
- timestamp;
- fingerprint summary;
- family/profile reference;
- last-seen time;
- event count;
- upload state;
- a compact Follow profile if selected.

Large raw captures may be deleted to free space.

### 9.3 New observations after deletion

If the same physical source is observed later, the event receives a new sequence number and event ID. It must be uploaded as a new observation, even if the fingerprint matches an older family.

The desktop application groups both observations under the same family while preserving their separate date, time, RSSI, and capture context.

### 9.4 Optional raw-data policy

To reduce storage without losing the investigation history, the implementation may support:

- full raw capture for every event;
- full raw capture only for new waveform variants;
- lightweight occurrence record for repeated identical waveform variants.

This must be a user-visible policy. The default should preserve full capture data until successful upload.

## 10. Desktop application

### 10.1 Import and synchronization

The desktop application must:

- discover or connect to the Flipper;
- display pending event count before import;
- resume interrupted imports;
- acknowledge only validated event IDs;
- ignore transport duplicates;
- show import progress;
- show which data has been safely deleted from the Flipper;
- maintain a local project/database;
- support multiple Flippers in the future.

### 10.2 Main investigation view

The main screen should feel like a signal investigation console rather than a spreadsheet.

Recommended layout:

- center: animated waterfall/spectrum;
- left: live event stream and new-family notifications;
- right: selected signal family and similarity details;
- bottom: time scrubber and event timeline;
- optional side panel: RSSI trend and source hypothesis.

### 10.3 Waterfall

The waterfall should visualize:

- time on one axis;
- frequency on the other axis;
- intensity by color;
- selected signal family as a highlighted overlay;
- other families as dimmed traces;
- capture windows as marked regions;
- new detections with an animated pulse.

The visual language may use a dark background, cyan/magenta/orange signal colors, glowing traces, and animated transitions inspired by cyberpunk investigation interfaces. Readability and precise labels must remain primary.

### 10.4 Similarity animation

When an event is selected:

1. Its waveform and spectrum appear in the main view.
2. Similar events animate toward the selected family node.
3. Dissimilar events remain separate.
4. Stable features are highlighted in one color.
5. Variable payload regions are highlighted in another color.
6. The similarity score and reasons are shown in plain language.

Example explanation:

~~~
94% similar
Same carrier frequency
Same modulation
Same preamble timing
Same repetition pattern
Payload differs
~~~

### 10.5 Timeline and physical-context analysis

The timeline must show the exact date and time of every event, not only relative time.

The user should be able to:

- zoom from months to individual microseconds within a capture;
- filter by date, time, family, frequency, and RSSI;
- select a time interval and see all activity;
- compare a morning observation with an evening observation;
- see signal strength changes during a walk;
- add optional user notes or location markers such as office gate or home.

The application must clearly label location conclusions as observations or hypotheses. Time and RSSI can support a physical search, but they are not a GPS position.

### 10.6 Signal family detail view

For each family, show:

- family ID;
- observation count;
- first-seen and last-seen dates/times;
- frequency range;
- modulation;
- waveform variants;
- RSSI distribution;
- time-of-day distribution;
- event timeline;
- source hypothesis;
- confidence;
- selected Follow status;
- all raw captures and their import status.

## 11. Local feedback and notifications

### 11.1 Capture notification

When a Capture trigger is detected:

- vibrate immediately;
- illuminate the indicator;
- create a local event;
- continue capturing;
- allow the user to silence future feedback without stopping logging.

### 11.2 Scout counters

Scout should expose at least:

- total event bursts since last review;
- new signal families since last review;
- pending upload count;
- last event date/time;
- last event frequency;
- storage remaining.

### 11.3 Follow notification

When Follow matches the selected family:

- vibrate with a distinguishable pattern;
- illuminate the indicator;
- show the matching family ID;
- show similarity and RSSI;
- save a normal event capture.

## 12. Battery, storage, and reliability

The implementation should prioritize long unattended operation.

Recommended strategies:

- configurable scan duty cycle;
- low-power Scout scan;
- short detailed Capture windows;
- in-memory ring buffer before SD write;
- batched SD writes when practical;
- configurable feedback intensity;
- graceful behavior when storage is nearly full;
- event priority over cosmetic UI updates;
- recovery after application restart or power loss.

The application must never silently overwrite unuploaded events. If storage is full, it must indicate the condition and use an explicit retention policy.

## 13. Safety and default behavior

Hunter, Scout, Capture, and Follow are passive modes. RF and NFC transmission must be disabled in these modes.

Any future transmit or replay feature must be a separate explicitly entered mode and must never be reachable accidentally from the background monitor.

## 14. Suggested implementation phases

### Phase 1 — Flipper logger

- Scout sweep;
- trigger detection;
- immediate vibration/indicator;
- event date and time;
- frequency, RSSI, duration, and basic pulse capture;
- SD storage;
- unique event IDs;
- pending-upload list.

### Phase 2 — Reliable desktop upload

- manifest;
- chunked upload;
- acknowledgement;
- retry;
- transport deduplication by event ID;
- safe deletion after acknowledgement.

### Phase 3 — Desktop analysis

- event timeline;
- basic waterfall;
- basic fingerprint extraction;
- signal-family grouping;
- event and family counters;
- raw capture inspection.

### Phase 4 — Investigation experience

- similarity animation;
- family graph;
- RSSI trends;
- Follow profiles;
- user notes and location markers;
- advanced waveform comparison.

### Phase 5 — NFC and advanced classification

- NFC event timeline;
- protocol classification;
- unified Sub-GHz/NFC project view;
- improved source hypotheses;
- richer offline PC analysis.

## 15. Acceptance criteria

The first usable version is complete when all of the following are true:

1. Flipper can monitor configured Sub-GHz bands in Scout mode.
2. A detected burst produces immediate vibration and indicator feedback.
3. The event contains an exact date and time.
4. Capture stores the best available raw/timing and sweep data.
5. Multiple repeated observations are preserved as separate events.
6. Events are automatically assigned to provisional signal families.
7. Scout shows both event count and new-family count since the last review.
8. The desktop application can import events repeatedly without duplicating them.
9. Upload acknowledgement allows raw data to be deleted safely from Flipper.
10. A later observation of the same source is retained as a new event and grouped with the existing family.
11. The desktop application renders a waterfall, timeline, family view, and similarity comparison.
12. Follow can monitor a selected recently observed family and provide immediate feedback.
13. No background mode transmits RF or NFC data.

## 16. Core design principle

The product must preserve evidence first and automate organization second:

~~~
Capture every observation
→ assign stable event identity
→ retain exact date and time
→ automatically group similar fingerprints
→ preserve all observations inside the group
→ visualize the result so the user can investigate the source
~~~

