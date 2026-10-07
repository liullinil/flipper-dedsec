#pragma once
/* Host stand-in for the notification service: messages are opaque objects
 * whose addresses the fake recognises. */
#define RECORD_NOTIFICATION "notification"
typedef struct NotificationApp NotificationApp;
typedef struct {
    int id;
} NotificationMessage;
typedef const NotificationMessage* NotificationSequence[];
extern const NotificationMessage message_force_vibro_setting_on;
extern const NotificationMessage message_force_vibro_setting_off;
extern const NotificationMessage message_vibro_on;
extern const NotificationMessage message_vibro_off;
extern const NotificationMessage message_red_255;
extern const NotificationMessage message_red_0;
extern const NotificationMessage message_green_255;
extern const NotificationMessage message_green_0;
extern const NotificationMessage message_blue_255;
extern const NotificationMessage message_blue_0;
extern const NotificationMessage message_delay_25;
extern const NotificationMessage message_delay_50;
extern const NotificationMessage message_delay_100;
void notification_message(NotificationApp* app, const NotificationSequence* sequence);
