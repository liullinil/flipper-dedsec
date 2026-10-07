#pragma once

#include <furi.h>

/* Persistent, user-editable settings (saved to APP_DATA_PATH(".uplink.settings")). */

/* Stored numbers are kept from v1.1 (Off = 4), RF was added later as 5. */
typedef enum {
    ScreenSys = 0,
    ScreenCodex = 1,
    ScreenClaude = 2,
    ScreenCmd = 3,
    ScreenOff = 4, // slot disabled
    ScreenRf = 5,
    ScreenIdCount = 6,
} ScreenId;

#define TAB_SLOTS 5

typedef enum {
    IndicatorsBars = 0,
    IndicatorsText = 1,
} Indicators;

typedef enum {
    ThemeNormal = 0, // dark ink on light screen
    ThemeInverted = 1, // light on dark (no longer offered in the settings)
} Theme;

typedef enum {
    FontNormal = 0,
    FontLarge = 1,
    FontSmall = 2,
    FontMicro = 3,
} FontSize;

typedef enum {
    OrientationHorizontal = 0,
    OrientationVertical = 1,
} Orientation;

typedef struct {
    uint8_t vibro; // master vibration on/off
    uint8_t led; // RGB led on alerts
    uint8_t backlight; // wake the screen on alerts
    uint8_t cmd_vibro; // vibrate when the console replies
    uint8_t indicators; // Indicators
    uint8_t theme; // Theme
    uint8_t font; // FontSize
    uint8_t orientation; // Orientation
    uint8_t auto_update; // 0 notify only, 1 install new releases automatically
    uint8_t tabs[TAB_SLOTS]; // ordered tab slots, each a ScreenId (ScreenOff to hide)
    // RF Hunter
    uint8_t rf_band; // RfBand: all / 433 / 315 / 868
    int8_t rf_rssi; // trigger threshold, dBm
    uint16_t rf_dwell_ms; // Scout: time on each frequency
    uint16_t rf_capture_ms; // longest capture window
    uint8_t rf_feedback; // vibrate + blink on events
    uint8_t rf_geiger; // Follow: Geiger counter clicks from the speaker
    uint8_t rf_keep; // keep records on the SD card after the PC imported them
    uint8_t rf_autostart; // start the receiver when the app starts
    uint8_t rf_sync; // let the PC import records over BLE
    int16_t rf_tz; // RTC local time minus UTC, minutes (learned from the PC)
} UplinkSettings;

void uplink_settings_default(UplinkSettings* s);
void uplink_settings_load(UplinkSettings* s);
void uplink_settings_save(const UplinkSettings* s);
