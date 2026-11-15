import 'package:flutter/material.dart';

import '../data/db.dart';
import 'common.dart';

class LogPage extends StatefulWidget {
  const LogPage({super.key});

  @override
  State<LogPage> createState() => _LogPageState();
}

class _LogPageState extends State<LogPage> with AutoRefresh {
  List<Map<String, Object?>> _rows = [];
  bool _errorsOnly = false;

  @override
  Future<void> reload() async {
    final rows = await (await Db.open()).logs();
    if (mounted) setState(() => _rows = rows);
  }

  static const _icons = {
    'setup': Icons.radar, 'trade': Icons.show_chart, 'order': Icons.shopping_cart, 'error': Icons.error_outline,
    'info': Icons.info_outline,
  };

  @override
  Widget build(BuildContext context) {
    final rows = _errorsOnly ? _rows.where((r) => r['kind'] == 'error').toList() : _rows;
    return ListView(padding: const EdgeInsets.all(16), children: [
      Row(children: [
        Expanded(child: Text('Log', style: Theme.of(context).textTheme.headlineSmall)),
        FilterChip(label: const Text('Errors'), selected: _errorsOnly, onSelected: (v) => setState(() => _errorsOnly = v)),
      ]),
      const SizedBox(height: 8),
      for (final r in rows)
        ListTile(
          dense: true,
          contentPadding: EdgeInsets.zero,
          leading: Icon(_icons[r['kind']] ?? Icons.circle,
              color: r['kind'] == 'error' ? Theme.of(context).colorScheme.error : null),
          title: Text('${r['text']}'),
          subtitle: Text('${r['at']}'.replaceFirst('T', ' ')),
        ),
      if (rows.isEmpty) const Text('Nothing logged yet'),
    ]);
  }
}
