#pragma once

#include <furi.h>
#include <furi_ble/profile_interface.h>

/* Custom BLE profile: one GATT service, open (no pairing).
 *   RX (write)  - PC -> Flipper: telemetry and command output, newline-terminated text.
 *   TX (notify) - Flipper -> PC: command requests the PC companion runs, newline-terminated.
 */

#define RFHUNTER_SERVICE_UUID "f5510000-1d00-4a1e-8b5e-0f11e7ca1000"
#define RFHUNTER_RX_UUID      "f5510001-1d00-4a1e-8b5e-0f11e7ca1000"
#define RFHUNTER_TX_UUID      "f5510002-1d00-4a1e-8b5e-0f11e7ca1000"
#define RFHUNTER_ADV_UUID16   0xDED5
#define RFHUNTER_NAME_PREFIX  "DedSec"
#define RFHUNTER_RX_MAX       243
#define RFHUNTER_TX_MAX       243

typedef void (*RfHunterRxCallback)(const uint8_t* data, uint16_t size, void* context);

typedef struct {
    RfHunterRxCallback rx_callback;
    void* rx_context;
} RfHunterProfileParams;

extern const FuriHalBleProfileTemplate* const rfhunter_ble_profile;

/* Notify the PC with one line (no trailing newline needed). Call from the app thread,
 * never from the rx callback. Returns true if it was handed to the stack. */
bool rfhunter_ble_tx(FuriHalBleProfileBase* profile, const uint8_t* data, uint16_t size);

