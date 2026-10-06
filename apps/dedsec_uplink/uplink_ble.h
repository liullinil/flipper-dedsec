#pragma once

#include <furi.h>
#include <furi_ble/profile_interface.h>

/* Custom BLE profile: one GATT service, open (no pairing).
 *   RX (write)  - PC -> Flipper: telemetry and command output, newline-terminated text.
 *   TX (notify) - Flipper -> PC: command requests the PC companion runs, newline-terminated.
 */

#define UPLINK_SERVICE_UUID "de5ec000-1d00-4a1e-8b5e-0f11e7ca1000"
#define UPLINK_RX_UUID      "de5ec001-1d00-4a1e-8b5e-0f11e7ca1000"
#define UPLINK_TX_UUID      "de5ec002-1d00-4a1e-8b5e-0f11e7ca1000"
#define UPLINK_ADV_UUID16   0xDED5
#define UPLINK_NAME_PREFIX  "DedSec"
#define UPLINK_RX_MAX       243
#define UPLINK_TX_MAX       243

typedef void (*UplinkRxCallback)(const uint8_t* data, uint16_t size, void* context);

typedef struct {
    UplinkRxCallback rx_callback;
    void* rx_context;
} UplinkProfileParams;

extern const FuriHalBleProfileTemplate* const uplink_ble_profile;

/* Notify the PC with one line (no trailing newline needed). Call from the app thread,
 * never from the rx callback. Returns true if it was handed to the stack. */
bool uplink_ble_tx(FuriHalBleProfileBase* profile, const uint8_t* data, uint16_t size);
