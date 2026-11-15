// The Dart port gives the Python app's answers: the NR7 walk over three years of real Yahoo candles on all three
// indices, the pattern, the summary, TOTP codes, implied vol / delta and marketable limit prices.
// Regenerate test/fixtures/parity.json from the Python side when the rules change.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:nr7_mobile/core/nr7.dart';
import 'package:nr7_mobile/core/options_math.dart';
import 'package:nr7_mobile/core/time.dart';
import 'package:nr7_mobile/core/totp.dart';

void main() {
  final fx = jsonDecode(File('test/fixtures/parity.json').readAsStringSync()) as Map<String, dynamic>;

  List<Bar> bars(List raw) => [
        for (final b in raw)
          Bar(parseIst(b[0]), (b[1] as num).toDouble(), (b[2] as num).toDouble(), (b[3] as num).toDouble(),
              (b[4] as num).toDouble())
      ];

  for (final e in (fx['indices'] as Map).entries) {
    test('NR7 walk on ${e.key} matches the Python app, trade for trade', () {
      final df = bars(e.value['bars']);
      final mine = walk(df, e.key, skipDays: {'2025-02-03'}).toList();
      final theirs = e.value['walk'] as List;
      expect(mine.length, theirs.length);
      for (var i = 0; i < theirs.length; i++) {
        final p = theirs[i] as Map;
        for (final k in p.keys) {
          final a = mine[i][k], b = p[k];
          if (a is num && b is num) {
            expect(a, closeTo(b, 1e-6), reason: '${p['key']} $k');
          } else {
            expect(a, b, reason: '${p['key']} $k');
          }
        }
      }
      expect(isNr7(df), e.value['nr7_last']);
      final s = summarize([for (final t in mine) if (closed.contains(t['status'])) t]);
      final ps = e.value['summary'] as Map;
      for (final k in ['trades', 'wins', 'losses', 'win_pct', 'avg_r', 'total_r', 'max_losing_streak', 'max_dd_r',
        'profit_factor']) {
        expect(s[k], ps[k], reason: k);
      }
    });
  }

  test('TOTP codes match', () {
    for (final r in fx['totp'] as List) {
      final at = DateTime.fromMillisecondsSinceEpoch((r[1] as int) * 1000, isUtc: true);
      expect(totp(r[0], at: at), r[2]);
    }
  });

  test('RFC 6238 test vector', () {
    // secret "12345678901234567890" in base32; T = 59 s -> 94287082 (8 digits)
    expect(totp('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', at: DateTime.fromMillisecondsSinceEpoch(59000, isUtc: true), digits: 8),
        '94287082');
  });

  test('implied vol and delta match', () {
    for (final r in fx['bs'] as List) {
      final iv = impliedVol(r[0], (r[4] as num).toDouble(), (r[1] as num).toDouble(), (r[2] as num).toDouble(),
          (r[3] as num).toDouble());
      if (r[5] == null) {
        expect(iv, isNull);
      } else {
        expect(iv, closeTo(r[5], 1e-9));
      }
      final d = delta(r[0], (r[1] as num).toDouble(), (r[2] as num).toDouble(), (r[3] as num).toDouble(), iv ?? 0);
      expect(d, closeTo(r[6], 1e-9));
    }
  });

  test('marketable limit prices match', () {
    for (final r in fx['protect'] as List) {
      expect(protect((r[0] as num).toDouble(), r[1], (r[2] as num).toDouble(), (r[3] as num).toDouble()), r[4]);
    }
  });

  test('IST helpers', () {
    expect(dstr(nextBDay(parseIst('2026-11-13'))), '2026-11-16'); // Friday -> Monday
    expect(dstr(prevBDay(parseIst('2026-11-16'))), '2026-11-13');
    expect(busdayCount(parseIst('2026-11-13'), parseIst('2026-11-20')), 5);
    expect(tsstr(istFromEpochSec(1763265600)), '2025-11-16T09:30:00');
  });
}
