import 'package:flutter/material.dart';

import '../core/nr7.dart' as nr7;
import '../engine/tracker.dart' as tracker;
import 'common.dart';

/// The last few months of NR7 setups replayed on daily candles under the live rules (R after costs).
class HistoryPage extends StatefulWidget {
  const HistoryPage({super.key});

  @override
  State<HistoryPage> createState() => _HistoryPageState();
}

class _HistoryPageState extends State<HistoryPage> {
  int _months = 3;
  Future<Map<String, dynamic>>? _data;

  void _load() => setState(() => _data = tracker.history(months: _months));

  @override
  Widget build(BuildContext context) {
    return ListView(padding: const EdgeInsets.all(16), children: [
      Text('Past trades', style: Theme.of(context).textTheme.headlineSmall),
      const SizedBox(height: 4),
      Text('Every NR7 replayed on daily candles with the live rules, after costs.',
          style: TextStyle(color: Theme.of(context).colorScheme.outline)),
      const SizedBox(height: 12),
      SegmentedButton<int>(
        segments: const [
          ButtonSegment(value: 3, label: Text('3 mo')),
          ButtonSegment(value: 6, label: Text('6 mo')),
          ButtonSegment(value: 12, label: Text('12 mo')),
          ButtonSegment(value: 24, label: Text('24 mo')),
        ],
        selected: {_months},
        onSelectionChanged: (v) {
          _months = v.first;
          _load();
        },
      ),
      const SizedBox(height: 8),
      if (_data == null) FilledButton.tonal(onPressed: _load, child: const Text('Load from Yahoo')),
      if (_data != null)
        FutureBuilder<Map<String, dynamic>>(
          future: _data,
          builder: (c, snap) {
            if (snap.hasError) return Text('Failed: ${snap.error}');
            if (!snap.hasData) {
              return const Padding(padding: EdgeInsets.all(24), child: Center(child: CircularProgressIndicator()));
            }
            final d = snap.data!;
            final total = d['total'] as Map;
            return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              _summary(context, 'All indices since ${d['from']}', total),
              for (final e in (d['indices'] as Map).entries) _summary(context, nr7.indices[e.key]!.name, e.value),
              const SizedBox(height: 8),
              for (final t in d['trades'] as List) _row(context, t),
            ]);
          },
        ),
    ]);
  }

  Widget _summary(BuildContext context, String title, Map s) => Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: Theme.of(context).textTheme.titleSmall),
            KV('Setups / trades', '${s['setups']} / ${s['trades']}'),
            if ((s['trades'] as int) > 0) ...[
              KV('Win rate', '${s['win_pct']}% (${s['wins']}W ${s['losses']}L)'),
              KV('Total R · avg', '${s['total_r']}R · ${s['avg_r']}R'),
              KV('₹ per futures lot', tracker.rupees(s['rs_per_lot'] as int)),
            ],
          ]),
        ),
      );

  Widget _row(BuildContext context, Map t) {
    final st = t['status'] as String;
    final r = t['r'] as num?;
    return ListTile(
      dense: true,
      contentPadding: EdgeInsets.zero,
      title: Text('${nr7.indices[t['idx']]!.name} · ${t['nr7_date']}'),
      subtitle: Text(nr7.closed.contains(st)
          ? '${t['side']} ${tracker.pts(t['entry'])} → ${tracker.pts(t['exit'])} · ${t['days']} day(s)'
          : (t['note'] as String?) ?? (st == 'OPEN' ? '${t['side']} from ${tracker.pts(t['entry'])}' : '')),
      trailing: Column(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.end, children: [
        Chip2(st, color: statusColor(context, st)),
        if (r != null) Text('${r >= 0 ? '+' : ''}${r.toStringAsFixed(2)}R'),
      ]),
    );
  }
}
