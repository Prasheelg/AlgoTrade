/// Alerts: every event goes to the on-phone log and, unless quiet, to an Android notification (in place of the
/// Python app's Telegram messages).
library;

import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../data/db.dart';

final _plugin = FlutterLocalNotificationsPlugin();
bool _ready = false;
int _id = 1000;

Future<void> initNotifications() async {
  if (_ready) return;
  try {
    await _plugin.initialize(
        settings: const InitializationSettings(android: AndroidInitializationSettings('@mipmap/ic_launcher')));
    _ready = true;
  } catch (_) {
    // tests / no plugin: the log still gets everything
  }
}

AndroidNotificationDetails _channel(String body) => AndroidNotificationDetails('nr7_alerts', 'NR7 alerts',
    channelDescription: 'Setups, entries, exits and orders', importance: Importance.high, priority: Priority.high,
    styleInformation: BigTextStyleInformation(body));

/// kind: setup / trade / order / error / info
Future<void> notify(String kind, String text, {bool alert = true}) async {
  await (await Db.open()).log(kind, text);
  if (!alert) return;
  await initNotifications();
  if (!_ready) return;
  final lines = text.split('\n');
  final body = lines.skip(1).join('\n');
  try {
    await _plugin.show(id: _id++, title: lines.first, body: body,
        notificationDetails: NotificationDetails(android: _channel(body)));
  } catch (_) {}
}
