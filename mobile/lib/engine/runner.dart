/// The background watcher: an Android foreground service that keeps the NR7 engine running on the phone with the
/// screen off. Each tick (every [tickSec]) it runs the evening scan (self-gated to once a day after 16:00 IST),
/// moves setups on through the minute bars, and steps auto-trade. The app's screens send it commands.
library;

import 'dart:async';

import 'package:flutter_foreground_task/flutter_foreground_task.dart';

import '../core/nr7.dart' as nr7;
import '../core/time.dart';
import '../data/db.dart';
import '../data/settings.dart';
import 'auto_trade.dart' as auto;
import 'notify.dart';
import 'tracker.dart' as tracker;

const tickSec = 15;
const heartbeatKey = 'runner_heartbeat';

class Engine {
  DateTime? _lastScanTry, _lastAuto;
  bool _busy = false;

  /// One pass of everything that is due. Safe to call often.
  Future<String> tick({bool forceAll = false}) async {
    if (_busy) return 'busy';
    _busy = true;
    final db = await Db.open();
    try {
      final now = istNow();
      final open = marketOpen(now);
      // evening scan (and the morning catch-up): the call is cheap when it isn't due, but it may hit Yahoo, so try
      // at most every 5 minutes
      if (forceAll || _lastScanTry == null || now.difference(_lastScanTry!).inMinutes >= 5) {
        _lastScanTry = now;
        try {
          await tracker.scan(now: now);
        } catch (e) {
          await db.log('error', 'NR7 scan failed: $e');
        }
      }
      try {
        await tracker.check(now: now, forceFetch: forceAll);
      } catch (e) {
        await db.log('error', 'NR7 check failed: $e');
      }
      final autoEvery = open ? tickSec : 60;
      if (forceAll || _lastAuto == null || now.difference(_lastAuto!).inSeconds >= autoEvery - 1) {
        _lastAuto = now;
        try {
          await auto.stepAll();
        } catch (e) {
          await db.log('error', 'NR7 auto-trade loop failed: $e');
        }
      }
      await db.set(heartbeatKey, tsstr(now));
      return await status();
    } finally {
      _busy = false;
    }
  }

  /// One line for the service notification.
  Future<String> status() async {
    final db = await Db.open();
    final s = await Settings.load();
    final setups = [for (final t in await db.setups(limit: 20)) if (nr7.active.contains(t['status'])) t];
    final trades = [for (final t in await db.trades()) if (auto.activeStates.contains(t['state'])) t];
    final parts = <String>[
      if (!s.on) 'Watching off',
      if (s.on) marketOpen() ? 'Market open' : 'Market closed',
      if (setups.isNotEmpty) '${setups.length} setup${setups.length == 1 ? '' : 's'} live',
      if (trades.isNotEmpty) '${trades.length} order${trades.length == 1 ? '' : 's'} live',
      if (auto.enabled(s)) 'Auto-trade ON',
    ];
    return parts.join(' · ');
  }
}

@pragma('vm:entry-point')
void startCallback() {
  FlutterForegroundTask.setTaskHandler(NR7TaskHandler());
}

class NR7TaskHandler extends TaskHandler {
  final _engine = Engine();

  Future<void> _run({bool force = false}) async {
    final text = await _engine.tick(forceAll: force);
    if (text != 'busy') {
      FlutterForegroundTask.updateService(notificationTitle: 'NR7 Trader running', notificationText: text);
      FlutterForegroundTask.sendDataToMain({'event': 'tick', 'status': text});
    }
  }

  @override
  Future<void> onStart(DateTime timestamp, TaskStarter starter) async {
    await initNotifications();
    await (await Db.open()).log('info', 'Background watcher started (${starter.name})');
    unawaited(_run());
  }

  @override
  void onRepeatEvent(DateTime timestamp) => unawaited(_run());

  @override
  Future<void> onDestroy(DateTime timestamp, bool isTimeout) async {
    await (await Db.open()).log('info', 'Background watcher stopped${isTimeout ? ' (timeout)' : ''}');
  }

  @override
  void onReceiveData(Object data) {
    if (data is! Map) return;
    unawaited(() async {
      final cmd = data['cmd'];
      try {
        if (cmd == 'scan') {
          final made = await tracker.scan(force: true);
          await tracker.check(forceFetch: true);
          FlutterForegroundTask.sendDataToMain({'event': 'scan', 'made': made.length});
        } else if (cmd == 'tick') {
          await _run(force: true);
        } else if (cmd == 'close') {
          await auto.closeTrade(data['key'] as String);
          FlutterForegroundTask.sendDataToMain({'event': 'closed', 'key': data['key']});
        }
      } catch (e) {
        await notify('error', '⚠️ $cmd failed\n$e');
        FlutterForegroundTask.sendDataToMain({'event': 'error', 'cmd': cmd, 'error': '$e'});
      }
    }());
  }

  @override
  void onNotificationPressed() => FlutterForegroundTask.launchApp('/');
}

// ---- service control (UI side) ----

void initService() {
  FlutterForegroundTask.init(
    androidNotificationOptions: AndroidNotificationOptions(
      channelId: 'nr7_watcher',
      channelName: 'NR7 background watcher',
      channelDescription: 'Shown while the NR7 watcher runs',
      onlyAlertOnce: true,
    ),
    iosNotificationOptions: const IOSNotificationOptions(showNotification: false, playSound: false),
    foregroundTaskOptions: ForegroundTaskOptions(
      eventAction: ForegroundTaskEventAction.repeat(tickSec * 1000),
      autoRunOnBoot: true,
      autoRunOnMyPackageReplaced: true,
      allowWakeLock: true,
      allowWifiLock: true,
    ),
  );
}

Future<void> requestPermissions() async {
  if (await FlutterForegroundTask.checkNotificationPermission() != NotificationPermission.granted) {
    await FlutterForegroundTask.requestNotificationPermission();
  }
  if (!await FlutterForegroundTask.isIgnoringBatteryOptimizations) {
    await FlutterForegroundTask.requestIgnoreBatteryOptimization();
  }
}

Future<bool> startService() async {
  await requestPermissions();
  if (await FlutterForegroundTask.isRunningService) return true;
  final r = await FlutterForegroundTask.startService(
    serviceId: 707,
    serviceTypes: [ForegroundServiceTypes.specialUse],
    notificationTitle: 'NR7 Trader running',
    notificationText: 'Starting…',
    callback: startCallback,
  );
  return r is ServiceRequestSuccess;
}

Future<void> stopService() => FlutterForegroundTask.stopService();

Future<bool> get serviceRunning => FlutterForegroundTask.isRunningService;
