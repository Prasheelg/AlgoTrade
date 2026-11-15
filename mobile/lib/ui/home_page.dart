import 'package:flutter/material.dart';

import '../core/nr7.dart' as nr7;
import '../core/time.dart';
import '../data/db.dart';
import '../data/settings.dart';
import '../engine/auto_trade.dart' as auto;
import '../engine/runner.dart';
import '../engine/tracker.dart' show pts, rupees, lastScanKey;
import '../net/contracts.dart';
import '../net/kotak.dart';
import 'common.dart';

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with AutoRefresh {
  List<Map<String, dynamic>> _setups = [];
  Settings _s = Settings();
  bool _running = false, _scanning = false;
  String? _lastScan, _heartbeat;

  @override
  Future<void> reload() async {
    final db = await Db.open();
    final setups = await db.setups(limit: 60);
    final s = await Settings.load();
    final running = await serviceRunning;
    final last = await db.get<String>(lastScanKey);
    final hb = await db.get<String>(heartbeatKey);
    if (!mounted) return;
    setState(() {
      _setups = setups;
      _s = s;
      _running = running;
      _lastScan = last;
      _heartbeat = hb;
    });
  }

  Future<void> _toggleService(bool on) async {
    if (on) {
      final ok = await startService();
      if (!ok && mounted) toast(context, 'Could not start the watcher (notification permission?)');
    } else {
      if (auto.enabled(_s) && mounted) {
        final go = await showDialog<bool>(
            context: context,
            builder: (c) => AlertDialog(
                  title: const Text('Stop the watcher?'),
                  content: const Text('Auto-trade is on. With the watcher stopped, open options are NOT sold at '
                      'the stop, the target or the day-5 exit until you start it again.'),
                  actions: [
                    TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Keep running')),
                    FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Stop')),
                  ],
                ));
        if (go != true) return;
      }
      await stopService();
    }
    await reload();
  }

  Future<void> _scan() async {
    setState(() => _scanning = true);
    try {
      await command('scan');
      if (mounted) toast(context, 'Scan done');
    } catch (e) {
      if (mounted) toast(context, 'Scan failed: $e');
    } finally {
      if (mounted) setState(() => _scanning = false);
      await reload();
    }
  }

  @override
  Widget build(BuildContext context) {
    final live = [for (final t in _setups) if (nr7.active.contains(t['status'])) t];
    final past = [for (final t in _setups) if (!nr7.active.contains(t['status'])) t];
    final open = marketOpen();
    return RefreshIndicator(
      onRefresh: _scan,
      child: ListView(padding: const EdgeInsets.all(16), children: [
        Text('NR7 Breakout', style: Theme.of(context).textTheme.headlineSmall),
        const SizedBox(height: 4),
        Text('Nifty 50 · Bank Nifty · Sensex', style: TextStyle(color: Theme.of(context).colorScheme.outline)),
        const SizedBox(height: 12),
        Card(
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(children: [
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Background watcher'),
                subtitle: Text(_running
                    ? 'Running · last check ${_heartbeat == null ? '—' : _heartbeat!.substring(11, 16)}'
                    : 'Stopped: no scans, no alerts, no orders'),
                value: _running,
                onChanged: _toggleService,
              ),
              const Divider(),
              Wrap(spacing: 8, runSpacing: 6, children: [
                Chip2(open ? 'Market open' : 'Market closed', color: open ? Colors.green.shade700 : null),
                Chip2(_s.on ? 'Watching' : 'Watching off'),
                Chip2(auto.enabled(_s) ? 'Auto-trade ON' : 'Alerts only',
                    color: auto.enabled(_s) ? Theme.of(context).colorScheme.error : null),
              ]),
              const SizedBox(height: 8),
              KV('Last evening scan', _lastScan ?? 'never'),
              const SizedBox(height: 8),
              SizedBox(
                width: double.infinity,
                child: FilledButton.tonalIcon(
                  onPressed: _scanning ? null : _scan,
                  icon: _scanning
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.search),
                  label: const Text('Scan now'),
                ),
              ),
            ]),
          ),
        ),
        const SizedBox(height: 16),
        Text('Live', style: Theme.of(context).textTheme.titleMedium),
        if (live.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 12),
            child: Text('No setup waiting or trade open. The scan runs by itself after 16:00 IST.'),
          ),
        for (final t in live) _SetupCard(t),
        const SizedBox(height: 16),
        Text('Recent', style: Theme.of(context).textTheme.titleMedium),
        for (final t in past) _PastTile(t),
        if (past.isEmpty) const Padding(padding: EdgeInsets.all(8), child: Text('Nothing yet')),
      ]),
    );
  }
}

class _SetupCard extends StatelessWidget {
  const _SetupCard(this.t);
  final Map<String, dynamic> t;

  @override
  Widget build(BuildContext context) {
    final name = nr7.indices[t['idx']]!.name;
    final hi = (t['high'] as num).toDouble(), lo = (t['low'] as num).toDouble(), r = (t['risk'] as num).toDouble();
    final isOpen = t['status'] == nr7.open;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(child: Text(name, style: Theme.of(context).textTheme.titleMedium)),
            Chip2(isOpen ? '${t['side']} · day ${t['day_no']}' : 'WAITING',
                color: statusColor(context, t['status'])),
          ]),
          Text('NR7 on ${t['nr7_date']}${t['trigger_day'] != null ? ' · trades ${t['trigger_day']}' : ' · next session only'}',
              style: TextStyle(color: Theme.of(context).colorScheme.outline)),
          const SizedBox(height: 8),
          if (!isOpen) ...[
            KV('Buy above', '${pts(hi)}  (stop ${pts(lo)} · tgt ${pts(hi + nr7.targetR * r)})'),
            KV('Sell below', '${pts(lo)}  (stop ${pts(hi)} · tgt ${pts(lo - nr7.targetR * r)})'),
            KV('Risk', '${pts(r)} pts · ${t['lots']} lot${t['lots'] == 1 ? '' : 's'} of ${t['lot']}'),
          ] else ...[
            KV('Entry', '${pts(t['entry'])} at ${(t['entry_at'] as String).substring(11, 16)} on ${(t['entry_at'] as String).substring(0, 10)}'),
            KV('Stop', pts(t['stop'])),
            KV('Target', pts(t['target'])),
            KV('Time exit', '15:15 on day ${nr7.holdDays}'),
          ],
          const SizedBox(height: 4),
          Align(
            alignment: Alignment.centerRight,
            child: TextButton.icon(
              icon: const Icon(Icons.candlestick_chart, size: 18),
              label: const Text('What to trade'),
              onPressed: () => _showPlans(context, t),
            ),
          ),
        ]),
      ),
    );
  }
}

Future<void> _showPlans(BuildContext context, Map<String, dynamic> t) async {
  final s = await Settings.load();
  if (!context.mounted) return;
  showModalBottomSheet(
    context: context,
    showDragHandle: true,
    builder: (c) => FutureBuilder<List<(String, OptionPlan?)>>(
      future: () async {
        final k = await Kotak.create();
        if (k.creds.consumerKey.isEmpty) throw 'Add your Kotak consumer key in Settings to see contracts';
        final q = await indexQuote(k, t['idx']);
        final spot = (q?['ltp'] as num?)?.toDouble();
        final sides = t['status'] == nr7.open ? [t['side'] == 'LONG' ? 'CE' : 'PE'] : ['CE', 'PE'];
        return [for (final o in sides) (o, await optionPlan(k, t, o, spot, istNow(), s.optionMinDays))];
      }(),
      builder: (c, snap) {
        Widget body;
        if (snap.hasError) {
          body = Text('${snap.error}');
        } else if (!snap.hasData) {
          body = const Center(child: CircularProgressIndicator());
        } else {
          body = Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (final (o, p) in snap.data!)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: p == null
                    ? Text('${o == 'CE' ? 'Call' : 'Put'}: no contract ${s.optionMinDays}+ days out')
                    : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(o == 'CE' ? 'Call (buy level)' : 'Put (sell level)',
                            style: Theme.of(c).textTheme.titleSmall),
                        KV('Contract', p.tsym),
                        KV('Premium', '₹${pts(p.premium)} · Δ${p.delta.toStringAsFixed(2)}${p.iv == null ? '' : ' · IV ${p.iv}%'}'),
                        KV('One lot', '${p.lot} × ₹${pts(p.premium)} = ${rupees(p.premium * p.lot)}'),
                        KV('Auto-trade buys', '${s.autoLots} lot${s.autoLots == 1 ? '' : 's'}'),
                      ]),
              ),
          ]);
        }
        return Padding(padding: const EdgeInsets.fromLTRB(16, 0, 16, 24), child: body);
      },
    ),
  );
}

class _PastTile extends StatelessWidget {
  const _PastTile(this.t);
  final Map<String, dynamic> t;

  @override
  Widget build(BuildContext context) {
    final name = nr7.indices[t['idx']]!.name;
    final st = t['status'] as String;
    String sub;
    if (nr7.closed.contains(st)) {
      final r = (t['r'] as num).toDouble();
      sub = '${t['side']} ${pts(t['entry'])} → ${pts(t['exit'])} · ${r >= 0 ? '+' : ''}${r.toStringAsFixed(2)}R';
    } else {
      sub = (t['note'] as String?) ?? '';
    }
    return ListTile(
      dense: true,
      contentPadding: EdgeInsets.zero,
      title: Text('$name · ${t['nr7_date']}'),
      subtitle: Text(sub),
      trailing: Chip2(st, color: statusColor(context, st)),
    );
  }
}
