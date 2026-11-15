import 'package:flutter/material.dart';

import '../core/nr7.dart' as nr7;
import '../data/db.dart';
import '../data/settings.dart';
import '../engine/auto_trade.dart' as auto;
import '../engine/tracker.dart' show pts, rupees;
import 'common.dart';

class TradesPage extends StatefulWidget {
  const TradesPage({super.key});

  @override
  State<TradesPage> createState() => _TradesPageState();
}

class _TradesPageState extends State<TradesPage> with AutoRefresh {
  List<Map<String, dynamic>> _trades = [];
  Settings _s = Settings();

  @override
  Future<void> reload() async {
    final t = await (await Db.open()).trades();
    final s = await Settings.load();
    if (mounted) setState(() => (_trades = t, _s = s));
  }

  Future<void> _close(Map t) async {
    final sell = t['state'] == auto.open;
    final go = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text(sell ? 'Sell ${t['tsym']} now?' : 'Cancel this NR7 trade?'),
        content: Text(sell
            ? 'A limit sell goes to Kotak at the bid less ${_s.optionProtectionPct}%.'
            : 'The bot stops watching ${nr7.indices[t['idx']]!.name} for this setup; anything already bought is sold.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('No')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(sell ? 'Sell' : 'Cancel trade')),
        ],
      ),
    );
    if (go != true) return;
    try {
      await command('close', key: t['key'] as String);
      if (mounted) toast(context, 'Sent');
    } catch (e) {
      if (mounted) toast(context, '$e');
    }
    await reload();
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return ListView(padding: const EdgeInsets.all(16), children: [
      Text('Auto-trade orders', style: Theme.of(context).textTheme.headlineSmall),
      const SizedBox(height: 8),
      Card(
        color: auto.enabled(_s) ? cs.errorContainer : null,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Text(auto.enabled(_s)
              ? 'Auto-trade is ON: real orders on ${_s.autoIndices.join(', ')}, ${_s.autoLots} lot a trade, '
                  'in-the-money monthly options ${_s.optionMinDays}+ days out. The watcher must keep running.'
              : 'Auto-trade is off. Turn on both "NR7 Trade" and "Auto-trade" in Settings to place real orders.'),
        ),
      ),
      const SizedBox(height: 8),
      if (_trades.isEmpty) const Padding(padding: EdgeInsets.all(8), child: Text('No orders yet')),
      for (final t in _trades) _tradeCard(t),
    ]);
  }

  Widget _tradeCard(Map<String, dynamic> t) {
    final st = t['state'] as String;
    final name = nr7.indices[t['idx']]!.name;
    final fill = (t['fill_price'] as num?)?.toDouble(), exit = (t['exit_price'] as num?)?.toDouble();
    final qty = t['qty'] as int?;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(
                child: Text('$name${t['side'] == null ? '' : ' ${t['side']}'} · NR7 ${t['nr7_date']}',
                    style: Theme.of(context).textTheme.titleSmall)),
            Chip2(st, color: statusColor(context, st)),
          ]),
          if (t['tsym'] != null) KV('Contract', '${t['tsym']}'),
          if (t['u_entry'] != null)
            KV('Index levels', 'in ${pts(t['u_entry'])} · stop ${pts(t['u_stop'])} · tgt ${pts(t['u_target'])}'),
          if (qty != null && fill != null) KV('Bought', '$qty @ ₹${pts(fill)} (${rupees(fill * qty)})'),
          if (st == auto.buying) KV('Buy order', '${t['buy_order']} @ ₹${pts(t['buy_price'])}'),
          if (st == auto.selling) KV('Sell order', '${t['sell_order']}'),
          if (exit != null && fill != null && qty != null)
            KV('Sold', '₹${pts(exit)} · P&L ${rupees((exit - fill) * qty)}'),
          if (t['exit_why'] != null) KV('Exit', '${t['exit_why']}'),
          if (t['note'] != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text('${t['note']}', style: TextStyle(color: Theme.of(context).colorScheme.outline)),
            ),
          if ([auto.wait, auto.buying, auto.open].contains(st))
            Align(
              alignment: Alignment.centerRight,
              child: TextButton(
                  onPressed: () => _close(t), child: Text(st == auto.open ? 'Sell now' : 'Cancel trade')),
            ),
        ]),
      ),
    );
  }
}
