import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';

import '../engine/auto_trade.dart' as auto;
import '../engine/runner.dart';
import '../engine/tracker.dart' as tracker;
import '../main.dart' show refreshBus;

/// Runs a command in the background watcher when it is running (it owns the engine then), else right here.
Future<void> command(String cmd, {String? key}) async {
  if (await serviceRunning) {
    FlutterForegroundTask.sendDataToTask({'cmd': cmd, 'key': ?key});
    return;
  }
  switch (cmd) {
    case 'scan':
      await tracker.scan(force: true);
      await tracker.check(forceFetch: true);
    case 'tick':
      await Engine().tick(forceAll: true);
    case 'close':
      await auto.closeTrade(key!);
  }
  refreshBus.add({'event': cmd});
}

void toast(BuildContext context, String text) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

/// A page that reloads its data every [every] and whenever the watcher reports something.
mixin AutoRefresh<T extends StatefulWidget> on State<T> {
  Timer? _timer;
  StreamSubscription? _sub;
  Duration get every => const Duration(seconds: 10);
  Future<void> reload();

  @override
  void initState() {
    super.initState();
    reload();
    _timer = Timer.periodic(every, (_) => reload());
    _sub = refreshBus.stream.listen((_) => reload());
  }

  @override
  void dispose() {
    _timer?.cancel();
    _sub?.cancel();
    super.dispose();
  }
}

class Chip2 extends StatelessWidget {
  const Chip2(this.text, {super.key, this.color});
  final String text;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final c = color ?? Theme.of(context).colorScheme.secondary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(color: c.withValues(alpha: 0.14), borderRadius: BorderRadius.circular(20)),
      child: Text(text, style: TextStyle(color: c, fontWeight: FontWeight.w600, fontSize: 12)),
    );
  }
}

Color statusColor(BuildContext context, String? s) {
  final cs = Theme.of(context).colorScheme;
  switch (s) {
    case 'WAIT':
    case 'BUYING':
      return Colors.amber.shade800;
    case 'OPEN':
    case 'SELLING':
      return cs.primary;
    case 'TARGET':
      return Colors.green.shade700;
    case 'STOP':
    case 'REJECTED':
      return cs.error;
    default:
      return cs.outline;
  }
}

class KV extends StatelessWidget {
  const KV(this.k, this.v, {super.key});
  final String k, v;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(children: [
          Expanded(child: Text(k, style: TextStyle(color: Theme.of(context).colorScheme.outline))),
          Text(v, style: const TextStyle(fontFeatures: [FontFeature.tabularFigures()])),
        ]),
      );
}
