// Live, read-only checks against Yahoo and Kotak (no login, no orders). Skipped unless NR7_LIVE=1; Kotak checks
// need KOTAK_CONSUMER_KEY in the environment.
//   NR7_LIVE=1 KOTAK_CONSUMER_KEY=... flutter test test/live_test.dart
@Tags(['live'])
library;

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:nr7_mobile/core/nr7.dart';
import 'package:nr7_mobile/core/time.dart';
import 'package:nr7_mobile/data/settings.dart';
import 'package:nr7_mobile/net/contracts.dart';
import 'package:nr7_mobile/net/kotak.dart';
import 'package:nr7_mobile/net/yahoo.dart' as yahoo;

void main() {
  final live = Platform.environment['NR7_LIVE'] == '1';
  final ck = Platform.environment['KOTAK_CONSUMER_KEY'] ?? '';

  test('Yahoo daily and minute candles', () async {
    final d = await yahoo.daily(range: '1mo');
    expect(d.keys, containsAll(['NIFTY', 'BANKNIFTY', 'SENSEX']));
    for (final e in d.entries) {
      final b = e.value.last;
      // ignore: avoid_print
      print('${e.key}: ${e.value.length} sessions, last ${b.day} H ${b.h} L ${b.l} · NR7 ${isNr7(e.value)}');
    }
    final m = await yahoo.minuteBars('NIFTY');
    // ignore: avoid_print
    print('NIFTY minute bars: ${m.length}, last ${tsstr(m.last.ts)}');
    expect(m, isNotEmpty);
  }, skip: !live);

  test('Kotak index quotes, F&O contracts and an ITM option pick', () async {
    final k = Kotak(Credentials(ck, '', '', '', ''));
    final q = await k.quotes([('nse_cm', 'Nifty 50'), ('nse_cm', 'Nifty Bank'), ('bse_cm', 'SENSEX')]);
    for (final x in q) {
      // ignore: avoid_print
      print('${x['exchange_token']}: ${parseQuote(x)}');
    }
    expect(q.length, 3);
    final csv = await k.download(await k.scripMasterUrl('nse_fo'));
    final cs = parseIndexMaster(csv, 'nse_fo', {'NIFTY', 'BANKNIFTY'});
    final bank = [for (final c in cs) if (c.underlying == 'BANKNIFTY' && !c.expiry.isBefore(dateOnly(istNow()))) c];
    final ce = [for (final c in bank) if (c.opt == 'CE') c];
    final exp = pickExpiry(ce.map((c) => c.expiry), istNow(), 14)!;
    // ignore: avoid_print
    print('${cs.length} index contracts; BANKNIFTY lot ${bank.first.lot}; monthly expiry 14+ days out: ${dstr(exp)}');
    final spot = parseQuote(q.firstWhere((x) => x['exchange_token'] == 'Nifty Bank'))!['ltp'] as double;
    // what the bot would pick for a buy level 0.5% above the spot
    final buyLevel = spot * 1.005;
    final rows = [for (final c in ce) if (c.expiry == exp) c];
    final strikes = rows.map((c) => c.strike!).toSet().toList()..sort();
    final itm = strikes.reversed.where((s) => s <= buyLevel).take(itmStrikes).toSet();
    final qs = await k.quotes([for (final c in rows) if (itm.contains(c.strike)) ('nse_fo', c.token)]);
    expect(qs, isNotEmpty);
    // ignore: avoid_print
    print('Quoted ${qs.length} ITM calls; first: ${qs.first['exchange_token']} mid ${midPrice(qs.first)}');
  }, skip: !live || ck.isEmpty);
}
