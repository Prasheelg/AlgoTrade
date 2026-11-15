// The on-phone engine end to end with a fake Kotak and an in-memory database: the evening scan, the minute-bar
// tracker and auto-trade (scenarios from tests/test_nr7_auto.py).
import 'package:flutter_test/flutter_test.dart';
import 'package:nr7_mobile/core/nr7.dart';
import 'package:nr7_mobile/core/time.dart';
import 'package:nr7_mobile/data/db.dart';
import 'package:nr7_mobile/data/settings.dart';
import 'package:nr7_mobile/engine/auto_trade.dart' as auto;
import 'package:nr7_mobile/engine/tracker.dart' as tracker;
import 'package:nr7_mobile/net/contracts.dart';
import 'package:nr7_mobile/net/kotak.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

class FakeKotak extends Kotak {
  FakeKotak() : super(const Credentials('ck', '+910000000000', 'U1', '1234', 'JBSWY3DPEHPK3PXP'));
  final book = <String, Map<String, dynamic>>{};
  final net = <String, int>{}; // a lot closed by hand in the Kotak app
  final placed = <Map<String, dynamic>>[];
  bool fill = true;
  int _n = 0;

  @override
  Future<String> placeOrder({required String segment, required String tsym, required String side,
      required int qty, required double price, String product = 'NRML'}) async {
    final oid = '${++_n}';
    final o = {'nOrdNo': oid, 'ordSt': fill ? 'complete' : 'open', 'fldQty': fill ? '$qty' : '0',
      'avgPrc': fill ? '$price' : '0', 'trdSym': tsym, 'trnsTp': side, 'qty': '$qty', 'prc': price};
    book[oid] = o;
    placed.add(o);
    return oid;
  }

  @override
  Future<void> cancelOrder(String orderId) async {
    final o = book[orderId];
    if (o != null && o['ordSt'] == 'open') o['ordSt'] = 'cancelled';
  }

  @override
  Future<Map<String, Map<String, dynamic>>> orderBook() async => {for (final e in book.entries) e.key: Map.of(e.value)};

  @override
  Future<List<Map<String, dynamic>>> positions() async {
    final n = <String, int>{};
    for (final o in book.values) {
      final q = int.parse(o['fldQty']) * (o['trnsTp'] == 'B' ? 1 : -1);
      n[o['trdSym']] = (n[o['trdSym']] ?? 0) + q;
    }
    n.addAll(net);
    return [for (final e in n.entries) {'trdSym': e.key, 'netQty': '${e.value}', 'flBuyQty': '0', 'flSellQty': '0'}];
  }
}

/// IST wall clock -> the UTC clock the app reads.
void at(String ist) => clock = () => parseIst(ist).subtract(istOffset);

late FakeKotak k;
late Map<String, Map<String, dynamic>> index;
late Map<String, double?> option;
late List<(String, String)> plans;

Future<Db> fresh() async {
  await Db.resetForTests();
  Db.overridePath = inMemoryDatabasePath;
  return Db.open();
}

Future<String> bankSetup({String day = '2026-11-13', double high = 61300.55, double low = 60780.55}) async {
  final t = newSetup('BANKNIFTY', parseIst(day), high, low)..addAll({'lots': 0, 'lot': 30});
  await (await Db.open()).insertSetup(t);
  return t['key'];
}

Future<Map<String, dynamic>> trade(String key) async => (await (await Db.open()).trade(key))!;

void move(Map<String, dynamic> kw) => index['BANKNIFTY']!.addAll(kw);

void main() {
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  setUp(() async {
    await fresh();
    await Settings(trade: true, autoTrade: true).save();
    k = FakeKotak();
    auto.kotakFactory = () async => k;
    index = {'BANKNIFTY': {'ltp': 61000.0, 'open': 61000.0, 'high': 61100.0, 'low': 60900.0}};
    option = {'bid': 1495.0, 'ask': 1500.0, 'ltp': 1497.0};
    plans = [];
    auto.indexQuoteFor = (k, idx) async => index[idx] == null ? null : Map.of(index[idx]!);
    auto.optionQuoteFor = (k, t) async => Map.of(option);
    auto.planFor = (k, setup, opt, spot, now, minDays) async {
      plans.add((setup['idx'] as String, opt));
      return OptionPlan('BANKNIFTY25DEC${opt == 'CE' ? 61000 : 61100}$opt', '111', 'nse_fo', parseIst('2026-12-29'),
          61000, 1497, 0.7, 14, 30, 0.05);
    };
    at('2026-11-16T09:20:00');
  });

  test('a long trigger buys one ITM call and sells it at the target', () async {
    final key = await bankSetup();
    await auto.stepAll();
    expect((await trade(key))['state'], 'WAIT'); // inside the range: nothing yet
    expect(k.placed, isEmpty);

    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    var t = await trade(key);
    expect([t['state'], t['side'], t['opt'], t['u_entry'], t['u_stop'], t['u_target']],
        ['BUYING', 'LONG', 'CE', 61300.55, 60780.55, 62860.55]);
    expect(plans, [('BANKNIFTY', 'CE')]);
    expect(k.placed.single['trnsTp'], 'B');
    expect(k.placed.single['prc'], 1575.0); // ask 1500 + 5%
    expect(k.placed.single['qty'], '30');

    await auto.stepAll();
    t = await trade(key);
    expect([t['state'], t['qty'], t['fill_price'], t['pos_seen']], ['OPEN', 30, 1575.0, 1]);

    move({'ltp': 62900.0, 'high': 62910.0});
    await auto.stepAll();
    t = await trade(key);
    expect(t['state'], 'SELLING');
    expect(k.placed.last['trnsTp'], 'S');
    expect(k.placed.last['prc'], 1420.25); // bid 1495 - 5%, rounded down to the tick
    expect(t['exit_why'], startsWith('target'));

    await auto.stepAll();
    t = await trade(key);
    expect([t['state'], t['exit_price']], ['CLOSED', 1420.25]);
  });

  test('a gap open past the level enters at the open', () async {
    final key = await bankSetup();
    move({'open': 61400.0, 'high': 61450.0, 'low': 61380.0, 'ltp': 61420.0});
    await auto.stepAll();
    final t = await trade(key);
    expect([t['u_entry'], t['u_target']], [61400.0, 61400.0 + 3 * (61400 - 60780.55)].map(r2).toList());
  });

  test('a short trigger buys a put and the stop sells it', () async {
    final key = await bankSetup();
    move({'low': 60700.0, 'ltp': 60710.0});
    await auto.stepAll();
    await auto.stepAll();
    expect((await trade(key))['state'], 'OPEN');
    expect(plans.single.$2, 'PE');
    move({'ltp': 61310.0, 'high': 61320.0});
    await auto.stepAll();
    final t = await trade(key);
    expect(t['state'], 'SELLING');
    expect(t['exit_why'], startsWith('stop-loss'));
  });

  test('both levels traded before the first look is no trade', () async {
    final key = await bankSetup();
    move({'high': 61350.0, 'low': 60700.0, 'ltp': 61000.0});
    await auto.stepAll();
    expect((await trade(key))['state'], 'SKIPPED');
    expect(k.placed, isEmpty);
  });

  test('nothing is traded unless both switches are on, and only the chosen indices', () async {
    await Settings(trade: true, autoTrade: false).save();
    final key = await bankSetup();
    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    expect(await (await Db.open()).trade(key), isNull);

    await Settings(trade: true, autoTrade: true, autoIndices: ['NIFTY']).save();
    await auto.stepAll();
    expect(await (await Db.open()).trade(key), isNull);
    expect(k.placed, isEmpty);
  });

  test('an open option is still sold after auto-trade is switched off', () async {
    final key = await bankSetup();
    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    await auto.stepAll();
    await Settings(trade: true, autoTrade: false).save();
    move({'ltp': 60700.0, 'low': 60690.0});
    await auto.stepAll();
    expect((await trade(key))['state'], 'SELLING');
  });

  test('an unfilled buy is cancelled after a minute and bought again at a fresh price', () async {
    final key = await bankSetup();
    k.fill = false;
    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    expect((await trade(key))['state'], 'BUYING');
    at('2026-11-16T09:20:30');
    await auto.stepAll();
    expect((await trade(key))['state'], 'BUYING'); // still within the minute
    at('2026-11-16T09:21:30');
    await auto.stepAll();
    var t = await trade(key);
    expect(t['state'], 'WAIT');
    expect(k.book['1']!['ordSt'], 'cancelled');
    k.fill = true;
    option = {'bid': 1515.0, 'ask': 1520.0, 'ltp': 1517.0};
    await auto.stepAll();
    t = await trade(key);
    expect(t['state'], 'BUYING');
    expect(t['buy_price'], 1596.0);
  });

  test('day five exits at 15:15', () async {
    final key = await bankSetup();
    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    await auto.stepAll();
    at('2026-11-20T15:14:00'); // Friday = day 5
    move({'ltp': 61500.0});
    await auto.stepAll();
    expect((await trade(key))['state'], 'OPEN');
    at('2026-11-20T15:15:00');
    await auto.stepAll();
    final t = await trade(key);
    expect(t['state'], 'SELLING');
    expect(t['exit_why'], 'day 5 exit');
  });

  test('a lot closed by hand in Kotak is not sold again', () async {
    final key = await bankSetup();
    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    await auto.stepAll();
    k.net['BANKNIFTY25DEC61000CE'] = 0;
    move({'ltp': 62900.0});
    await auto.stepAll();
    final t = await trade(key);
    expect(t['state'], 'CLOSED');
    expect(t['note'], contains('nothing sold'));
    expect(k.placed.where((o) => o['trnsTp'] == 'S'), isEmpty);
  });

  test('the entry day passing cancels a waiting trade', () async {
    final key = await bankSetup();
    final db = await Db.open();
    await auto.stepAll();
    await db.updateSetup(key, {'trigger_day': '2026-11-16'});
    at('2026-11-17T09:20:00');
    await auto.stepAll();
    expect((await trade(key))['state'], 'CANCELLED');
  });

  test('close from the app sells an open trade now', () async {
    final key = await bankSetup();
    move({'high': 61350.0, 'ltp': 61340.0});
    await auto.stepAll();
    await auto.stepAll();
    await auto.closeTrade(key);
    final t = await trade(key);
    expect([t['state'], t['exit_why']], ['SELLING', 'closed by you']);
  });

  group('tracker', () {
    List<Bar> daily(List<double> ranges, String start) {
      var d = parseIst(start);
      final out = <Bar>[];
      for (final r in ranges) {
        while (!isWeekday(d)) {
          d = d.add(const Duration(days: 1));
        }
        out.add(Bar(d, 100, 100 + r / 2, 100 - r / 2, 100));
        d = d.add(const Duration(days: 1));
      }
      return out;
    }

    setUp(() {
      tracker.lotFor = (idx) async => indices[idx]!.defaultLot;
    });

    test('the evening scan finds an NR7 and the minute bars trade it', () async {
      // Mon 2026-11-02 .. Fri 2026-11-13: the last day is the narrowest of 7
      final bars = daily([12, 11, 10, 9, 8, 7, 6, 5, 4, 3], '2026-11-02');
      tracker.dailyBars = ({String range = '1y'}) async => {'NIFTY': bars};
      at('2026-11-13T16:05:00');
      final made = await tracker.scan();
      expect(made.single['key'], 'NIFTY|2026-11-13');
      expect([made.single['high'], made.single['low'], made.single['status']], [101.5, 98.5, 'WAIT']);
      expect(await tracker.scan(), isEmpty); // once a day

      // Monday: up through the high at 09:31, then the target (101.5 + 3 x 3 = 110.5) on day 2
      tracker.minuteBars = (idx) async => [
            Bar(parseIst('2026-11-16T09:30:00'), 100, 101, 99.5, 100.5),
            Bar(parseIst('2026-11-16T09:31:00'), 100.5, 102, 100.4, 101.8),
            Bar(parseIst('2026-11-17T09:15:00'), 105, 111, 104, 110),
          ];
      at('2026-11-16T09:40:00');
      expect(await tracker.check(forceFetch: true), 1);
      var t = (await (await Db.open()).setups()).single;
      expect([t['status'], t['side'], t['entry'], t['stop'], t['target']], ['OPEN', 'LONG', 101.5, 98.5, 110.5]);
      at('2026-11-17T09:20:00');
      await tracker.check(forceFetch: true);
      t = (await (await Db.open()).setups()).single;
      expect([t['status'], t['exit'], t['r']], ['TARGET', 110.5, 3.0]);
    });

    test('a skip day skips the setup', () async {
      await Settings(skipDays: ['2026-11-16']).save();
      tracker.dailyBars = ({String range = '1y'}) async => {'NIFTY': daily([12, 11, 10, 9, 8, 7, 6, 5, 4, 3], '2026-11-02')};
      at('2026-11-13T16:05:00');
      final made = await tracker.scan();
      expect(made.single['status'], 'SKIPPED');
    });

    test('a setup not triggered in its session expires', () async {
      tracker.dailyBars = ({String range = '1y'}) async => {'NIFTY': daily([12, 11, 10, 9, 8, 7, 6, 5, 4, 3], '2026-11-02')};
      at('2026-11-13T16:05:00');
      await tracker.scan();
      tracker.minuteBars = (idx) async => [Bar(parseIst('2026-11-16T09:30:00'), 100, 101, 99, 100)];
      at('2026-11-16T15:35:00');
      await tracker.check(forceFetch: true);
      expect((await (await Db.open()).setups()).single['status'], 'EXPIRED');
    });
  });
}
