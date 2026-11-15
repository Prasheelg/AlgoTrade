/// Index candles from Yahoo's chart API (what yfinance uses), straight from the phone.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../core/nr7.dart';
import '../core/time.dart';

const _ua = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36';

Future<List<Bar>> _chart(String symbol, String range, String interval, {bool daily = false}) async {
  final uri = Uri.https('query1.finance.yahoo.com', '/v8/finance/chart/$symbol',
      {'range': range, 'interval': interval, 'includePrePost': 'false'});
  final res = await http.get(uri, headers: {'User-Agent': _ua}).timeout(const Duration(seconds: 30));
  if (res.statusCode != 200) throw Exception('Yahoo $symbol $interval: HTTP ${res.statusCode}');
  final r = (jsonDecode(res.body)['chart']?['result'] as List?)?.first;
  if (r == null) throw Exception('Yahoo $symbol $interval: no data');
  final ts = List<num?>.from(r['timestamp'] ?? []);
  final q = r['indicators']['quote'][0];
  final o = List<num?>.from(q['open']), h = List<num?>.from(q['high']), l = List<num?>.from(q['low']),
      c = List<num?>.from(q['close']);
  final out = <Bar>[];
  for (var i = 0; i < ts.length; i++) {
    if ([ts[i], o[i], h[i], l[i], c[i]].contains(null)) continue;
    var t = istFromEpochSec(ts[i]!);
    if (daily) t = dateOnly(t);
    if (out.isNotEmpty && !t.isAfter(out.last.ts)) {
      if (t != out.last.ts) continue;
      out.removeLast(); // Yahoo sometimes repeats today's daily bar: keep the latest
    }
    out.add(Bar(t, o[i]!.toDouble(), h[i]!.toDouble(), l[i]!.toDouble(), c[i]!.toDouble()));
  }
  return out;
}

/// Daily candles of real full sessions (see nr7.sessions), per index.
Future<Map<String, List<Bar>>> daily({String range = '1y', Iterable<String>? only}) async {
  final out = <String, List<Bar>>{};
  for (final e in indices.entries) {
    if (only != null && !only.contains(e.key)) continue;
    try {
      out[e.key] = sessions(await _chart(e.value.yahoo, range, '1d', daily: true));
    } catch (_) {
      // an index missing today just isn't scanned
    }
  }
  return out;
}

/// The last 7 days of 1-minute bars on real trading days.
Future<List<Bar>> minuteBars(String idx) async => realDays(await _chart(indices[idx]!.yahoo, '7d', '1m'));
