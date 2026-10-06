#include "uplink_ble.h"

#include <furi_hal_version.h>
#include <furi_ble/gatt.h>
#include <furi_ble/event_dispatcher.h>
#include <gap.h>
#include <ble/core/ble_std.h>
#include <ble/core/ble_defs.h>

/* The dispatcher hands services raw HCI packets; the SDK does not ship ST's hci_tl.h,
 * so these are the few packed layouts we need (same as ST hci_tl.h / ble_events.h). */
typedef struct __attribute__((packed)) {
    uint8_t type;
    uint8_t data[1];
} UplinkHciUartPacket;

typedef struct __attribute__((packed)) {
    uint8_t evt;
    uint8_t plen;
    uint8_t data[1];
} UplinkHciEventPacket;

typedef struct __attribute__((packed)) {
    uint16_t ecode;
    uint8_t data[1];
} UplinkBlecoreEvent;

#define UPLINK_ACI_GATT_ATTRIBUTE_MODIFIED_VSEVT_CODE (0x0C01U)

/* little-endian forms of UPLINK_SERVICE_UUID / UPLINK_RX_UUID */
static const Service_UUID_t uplink_service_uuid = {
    .Service_UUID_128 = {
        0x00, 0x10, 0xca, 0xe7, 0x11, 0x0f, 0x5e, 0x8b,
        0x1e, 0x4a, 0x00, 0x1d, 0x00, 0xc0, 0x5e, 0xde},
};

static const BleGattCharacteristicParams uplink_rx_params = {
    .name = "Uplink RX",
    .data_prop_type = FlipperGattCharacteristicDataFixed,
    .data.fixed.length = UPLINK_RX_MAX,
    .uuid.Char_UUID_128 = {
        0x00, 0x10, 0xca, 0xe7, 0x11, 0x0f, 0x5e, 0x8b,
        0x1e, 0x4a, 0x00, 0x1d, 0x01, 0xc0, 0x5e, 0xde},
    .uuid_type = UUID_TYPE_128,
    .char_properties = CHAR_PROP_WRITE_WITHOUT_RESP | CHAR_PROP_WRITE,
    .security_permissions = ATTR_PERMISSION_NONE,
    .gatt_evt_mask = GATT_NOTIFY_ATTRIBUTE_WRITE,
    .is_variable = CHAR_VALUE_LEN_VARIABLE,
};

/* TX uses the callback data type so each notification carries only the bytes we pass,
 * not a fixed-length padding (the fixed path always sends data.fixed.length bytes). */
typedef struct {
    const uint8_t* ptr;
    uint16_t len;
} UplinkTxData;

static bool uplink_tx_data_cb(const void* context, const uint8_t** data, uint16_t* data_len) {
    if(data == NULL) {
        *data_len = UPLINK_TX_MAX; // asked for the max length at init time
        return false;
    }
    const UplinkTxData* tx = context;
    *data = tx->ptr;
    *data_len = tx->len;
    return false; // caller keeps ownership of the buffer
}

static const BleGattCharacteristicParams uplink_tx_params = {
    .name = "Uplink TX",
    .data_prop_type = FlipperGattCharacteristicDataCallback,
    .data.callback.fn = uplink_tx_data_cb,
    .data.callback.context = NULL,
    .uuid.Char_UUID_128 = {
        0x00, 0x10, 0xca, 0xe7, 0x11, 0x0f, 0x5e, 0x8b,
        0x1e, 0x4a, 0x00, 0x1d, 0x02, 0xc0, 0x5e, 0xde},
    .uuid_type = UUID_TYPE_128,
    .char_properties = CHAR_PROP_READ | CHAR_PROP_NOTIFY,
    .security_permissions = ATTR_PERMISSION_NONE,
    .gatt_evt_mask = GATT_DONT_NOTIFY_EVENTS,
    .is_variable = CHAR_VALUE_LEN_VARIABLE,
};

typedef struct {
    FuriHalBleProfileBase base;
    uint16_t svc_handle;
    bool svc_ok;
    BleGattCharacteristicInstance rx_char;
    BleGattCharacteristicInstance tx_char;
    GapSvcEventHandler* event_handler;
    UplinkRxCallback rx_callback;
    void* rx_context;
} UplinkProfile;

_Static_assert(offsetof(UplinkProfile, base) == 0, "Wrong layout");

static BleEventAckStatus uplink_event_handler(void* event, void* context) {
    UplinkProfile* profile = context;
    UplinkHciEventPacket* packet = (UplinkHciEventPacket*)(((UplinkHciUartPacket*)event)->data);
    if(packet->evt != HCI_VENDOR_SPECIFIC_DEBUG_EVT_CODE) return BleEventNotAck;

    UplinkBlecoreEvent* core = (UplinkBlecoreEvent*)packet->data;
    if(core->ecode != UPLINK_ACI_GATT_ATTRIBUTE_MODIFIED_VSEVT_CODE) return BleEventNotAck;

    aci_gatt_attribute_modified_event_rp0* modified =
        (aci_gatt_attribute_modified_event_rp0*)core->data;
    if(modified->Attr_Handle == profile->rx_char.handle + 1) {
        if(profile->rx_callback) {
            profile->rx_callback(
                modified->Attr_Data, modified->Attr_Data_Length, profile->rx_context);
        }
        return BleEventAckFlowEnable;
    }
    return BleEventNotAck;
}

static FuriHalBleProfileBase* uplink_profile_start(FuriHalBleProfileParams params) {
    UplinkProfileParams* uplink_params = params;
    UplinkProfile* profile = malloc(sizeof(UplinkProfile));
    profile->base.config = uplink_ble_profile;
    profile->rx_callback = uplink_params ? uplink_params->rx_callback : NULL;
    profile->rx_context = uplink_params ? uplink_params->rx_context : NULL;

    profile->event_handler =
        ble_event_dispatcher_register_svc_handler(uplink_event_handler, profile);
    profile->svc_ok = ble_gatt_service_add(
        UUID_TYPE_128, &uplink_service_uuid, PRIMARY_SERVICE, 10, &profile->svc_handle);
    if(profile->svc_ok) {
        ble_gatt_characteristic_init(profile->svc_handle, &uplink_rx_params, &profile->rx_char);
        ble_gatt_characteristic_init(profile->svc_handle, &uplink_tx_params, &profile->tx_char);
    }
    FURI_LOG_I(
        "UplinkBle",
        "service %s, svc 0x%04X rx 0x%04X tx 0x%04X",
        profile->svc_ok ? "up" : "FAILED",
        profile->svc_handle,
        profile->rx_char.handle,
        profile->tx_char.handle);
    return &profile->base;
}

static void uplink_profile_stop(FuriHalBleProfileBase* base) {
    furi_check(base);
    furi_check(base->config == uplink_ble_profile);
    UplinkProfile* profile = (UplinkProfile*)base;
    ble_event_dispatcher_unregister_svc_handler(profile->event_handler);
    if(profile->svc_ok) {
        ble_gatt_characteristic_delete(profile->svc_handle, &profile->tx_char);
        ble_gatt_characteristic_delete(profile->svc_handle, &profile->rx_char);
        ble_gatt_service_delete(profile->svc_handle);
    }
    // the HAL drops its pointer after stop() and never frees it, so we do
    free(profile);
}

bool uplink_ble_tx(FuriHalBleProfileBase* base, const uint8_t* data, uint16_t size) {
    if(!base || base->config != uplink_ble_profile) return false;
    UplinkProfile* profile = (UplinkProfile*)base;
    if(!profile->svc_ok) return false;
    UplinkTxData tx = {.ptr = data, .len = MIN(size, (uint16_t)UPLINK_TX_MAX)};
    // returns true on failure, so invert
    return !ble_gatt_characteristic_update(profile->svc_handle, &profile->tx_char, &tx);
}

static const GapConfig uplink_gap_template = {
    .adv_service =
        {
            .UUID_Type = UUID_TYPE_16,
            .Service_UUID_16 = UPLINK_ADV_UUID16,
        },
    .appearance_char = 0x0000,
    .bonding_mode = false,
    .pairing_method = GapPairingNone,
    .conn_param =
        {
            .conn_int_min = 0x06,
            .conn_int_max = 0x24,
            .slave_latency = 0,
            .supervisor_timeout = 0,
        },
};

static void uplink_profile_get_gap_config(GapConfig* config, FuriHalBleProfileParams params) {
    UNUSED(params);
    furi_check(config);
    memcpy(config, &uplink_gap_template, sizeof(GapConfig));
    // own identity, so Windows never mixes us up with the phone-paired Flipper
    memcpy(config->mac_address, furi_hal_version_get_ble_mac(), sizeof(config->mac_address));
    config->mac_address[0] ^= 0xD5;
    config->mac_address[1] ^= 0xDE;
    config->mac_address[2] += 3;
    snprintf(
        config->adv_name,
        sizeof(config->adv_name),
        "%c%s %s",
        furi_hal_version_get_ble_local_device_name_ptr()[0],
        UPLINK_NAME_PREFIX,
        furi_hal_version_get_name_ptr());
}

static const FuriHalBleProfileTemplate uplink_profile_callbacks = {
    .start = uplink_profile_start,
    .stop = uplink_profile_stop,
    .get_gap_config = uplink_profile_get_gap_config,
};

const FuriHalBleProfileTemplate* const uplink_ble_profile = &uplink_profile_callbacks;
