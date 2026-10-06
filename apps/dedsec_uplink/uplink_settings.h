#pragma once

#include <furi.h>

/* Persistent, user-editable settings (saved to APP_DATA_PATH(".uplink.settings")). */

typedef enum {
    ScreenSys = 0,
    ScreenCodex = 1,
    ScreenClaude = 2,
    ScreenCmd = 3,
    ScreenOff = 4,   // slot disabled
    ScreenKindCount = 4,
} ScreenId;

typedef enum {
    IndicatorsBars = 0,
    IndicatorsText = 1,
} Indicators;

typedef enum {
    ThemeNormal = 0,   // dark ink on light screen
    ThemeInverted = 1, // light on dark
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
    uint8_t vibro;       // master vibration on/off
    uint8_t led;         // RGB led on alerts
    uint8_t backlight;   // wake the screen on alerts
    uint8_t cmd_vibro;   // vibrate when the console replies
    uint8_t indicators;  // Indicators
    uint8_t theme;       // Theme
    uint8_t font;        // FontSize
    uint8_t orientation; // Orientation
    uint8_t auto_update; // 0 notify only, 1 install new releases automatically
    uint8_t tabs[4];     // ordered tab slots, each a ScreenId (ScreenOff to hide)
} UplinkSettings;

void uplink_settings_default(UplinkSettings* s);
void uplink_settings_load(UplinkSettings* s);
void uplink_settings_save(const UplinkSettings* s);
