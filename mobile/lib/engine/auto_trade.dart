/// NR7 auto-trade on the phone: real Kotak orders for the NR7 index breakout, bought as in-the-money options.
/// A port of algotrade/nr7_auto.py; the rules are NR7_Breakout_Playbook.html's.
///
/// - Entry, on the setup's next session only: the first NR7 level the index trades past on Kotak's live index quote
///   (its day high / low, so a spike between two looks still counts) buys an in-the-money call (buy level) or put
///   (sell level): the monthly expiry at least optionMinDays away, delta nearest 0.7. Both levels already traded
///   when the bot first looks: no trade. The index already back at the stop or at the target: no trade.
/// - Exit (the option is sold): the index at the stop or the target; 15:15 on the 5th session; or the tracker
///   closing the same trade. Nothing rests at Kotak, so the background watcher must be running.
/// - Orders: limits at the ask + protection for a buy, the bid - it for a sell (no market orders via the API),
///   product NRML; an unfilled order is re-priced after [repriceSec]. Before a sell, Kotak's positions are read so a
///   lot already closed by hand in the Kotak app is not sold again (that would open a short option).
/// - Runs only with the Auto-trade master switch AND the NR7 Trade switch on. Switching either off cancels trades
///   still waiting to buy; options already bought are still sold by the rules.
library;

import 'dart:math' as math;

import '../core/nr7.dart' as nr7;
import '../core/options_math.dart';
import '../core/time.dart';
import '../data/db.dart';
import '../data/settings.dart';
import '../net/contracts.dart';
import '../net/kotak.dart';
import 'notify.dart';
import 'tracker.dart' show pts, rupees;

const repriceSec = 60;
const wait = 'WAIT', buying = 'BUYING', open = 'OPEN', selling = 'SELLING';
const closedState = 'CLOSED', cancelled = 'CANCELLED', rejected = 'REJECTED', skipped = 'SKIPPED';
const activeStates = [wait, buying, open, selling];
const closeNote = 'closed by you';

/// Injectable for tests.
Future<Kotak> Function() kotakFactory = Kotak.create;
Future<OptionPlan?> Function(Kotak k, Map setup, String opt, double? spot, DateTime now, int minDays) planFor =
    optionPlan;
Future<Map<String, dynamic>?> Function(Kotak k, String idx) indexQuoteFor = indexQuote;
Future<Map<String, double?>> Function(Kotak k, Map t) optionQuoteFor =
    (k, t) => optionQuote(k, t['segment'], t['token'], t['tsym']);

Future<void> _n(String text) => notify('order', '📐 NR7 · $text');

String _name(Map t) => nr7.indices[t['idx']]!.name;

double _age(String? ts) => ts == null ? 1e9 : istNow().difference(parseIst(ts)).inSeconds.toDouble();

double _d(Object? v) => (v as num).toDouble();

bool enabled(Settings s) => s.on && s.trade && s.autoTrade;

/// The index has reached a level: the target side (up for a LONG) or the stop side.
bool _past(double level, double px, String side, bool towardTarget) =>
    (side == 'LONG') == towardTarget ? px >= level : px <= level;

bool _busy = false;

/// Creates trades for new setups, then moves every live one on. True if there were live trades.
Future<bool> stepAll() async {
  if (_busy) return true;
  _busy = true;
  try {
    final db = await Db.open();
    final s = await Settings.load();
    final on = enabled(s);
    final setups = {for (final x in await db.setups()) x['key'] as String: x};
    if (on) await _create(db, s, setups);
    final live = [for (final t in await db.trades()) if (activeStates.contains(t['state'])) t];
    if (live.isEmpty) return false;
    final k = await kotakFactory();
    Map<String, Map<String, dynamic>> book = {};
    if (live.any((t) => t['state'] == buying || t['state'] == selling)) book = await k.orderBook();
    for (final t in live) {
      try {
        await _step(db, k, s, t, book, setups[t['key']], on);
      } on BrokerError catch (e) {
        await db.saveTrade(t['key'], {'note': e.message.length > 300 ? e.message.substring(0, 300) : e.message});
        await db.log('error', 'NR7 trade ${t['key']}: ${e.message}');
      } catch (e) {
        await db.log('error', 'NR7 trade ${t['key']} crashed: $e');
      }
    }
    return true;
  } finally {
    _busy = false;
  }
}

Future<void> _create(Db db, Settings s, Map<String, Map<String, dynamic>> setups) async {
  final today = dstr(istNow());
  for (final x in setups.values) {
    if (!s.autoIndices.contains(x['idx']) || await db.trade(x['key']) != null) continue;
    // a setup still waiting, or one the tracker entered today before the bot first looked
    if (x['status'] == nr7.wait || (x['status'] == nr7.open && x['trigger_day'] == today)) {
      await db.saveTrade(x['key'],
          {'idx': x['idx'], 'nr7_date': x['nr7_date'], 'high': x['high'], 'low': x['low'], 'state': wait});
      await db.log('info', 'NR7 auto: watching ${x['key']}');
    }
  }
}

Future<void> _step(Db db, Kotak k, Settings s, Map t, Map book, Map? setup, bool on) async {
  final st = t['state'];
  if (st == open && t['expiry'] != null && !parseIst(t['expiry']).isAfter(dateOnly(istNow()))) {
    await _n('⚠️ ${t['tsym']} reaches its expiry while the bot holds it; selling it');
    await _sell(db, k, s, t, 'expiry day');
    return;
  }
  switch (st) {
    case wait:
      await _stepWait(db, k, s, t, setup, on);
    case buying:
      await _stepBuying(db, k, t, book, on);
    case open:
      await _stepOpen(db, k, s, t, setup);
    case selling:
      await _stepSelling(db, k, s, t, book);
  }
}

// ---- entry ----

Future<void> _cancel(Db db, Map t, String why, {String state = cancelled, bool tell = false}) async {
  await db.saveTrade(t['key'], {'state': state, 'note': why});
  await db.log('info', 'NR7 auto ${t['key']}: ${state.toLowerCase()} ($why)');
  if (tell) await _n('✖️ ${_name(t)} (${t['nr7_date']}): no trade\n$why');
}

Future<void> _stepWait(Db db, Kotak k, Settings s, Map t0, Map? setup, bool on) async {
  var t = t0;
  final today = dstr(istNow());
  if (!on) return _cancel(db, t, 'auto-trade switched off');
  if (setup == null || ['EXPIRED', 'SKIPPED'].contains(setup['status'])) {
    return _cancel(db, t, 'the setup is ${('${setup?['status'] ?? 'gone'}').toLowerCase()}');
  }
  final td = setup['trigger_day'] as String?, tday = t['trade_day'] as String?;
  if ((td != null && td.compareTo(today) < 0) || (tday != null && tday.compareTo(today) < 0)) {
    return _cancel(db, t, 'its entry day is over', tell: t['side'] != null);
  }
  if (!marketOpen()) return;
  final q = await indexQuoteFor(k, t['idx']);
  if (q == null) return;
  final ltp = _d(q['ltp']);
  if (t['side'] == null) {
    final hi = _d(t['high']), lo = _d(t['low']);
    final up = _d(q['high']) > hi, down = _d(q['low']) < lo;
    if (up && down) {
      return _cancel(db, t, 'both levels had traded before the bot saw either (the playbook counts it as a stop-out)',
          state: skipped, tell: true);
    }
    if (!(up || down)) return;
    final side = up ? 'LONG' : 'SHORT';
    final entry = up ? math.max(_d(q['open']), hi) : math.min(_d(q['open']), lo); // a gap past the level: the open
    final stop = up ? lo : hi;
    final target = entry + (up ? 1 : -1) * nr7.targetR * (entry - stop).abs();
    await db.saveTrade(t['key'], {
      'side': side, 'opt': up ? 'CE' : 'PE', 'u_entry': nr7.r2(entry), 'u_stop': stop, 'u_target': nr7.r2(target),
      'trade_day': today,
    });
    t = (await db.trade(t['key']))!;
    await _n('🔔 ${_name(t)} $side: ${up ? 'above' : 'below'} ${pts(up ? hi : lo)} (now ${pts(ltp)})\n'
        'Stop ${pts(stop)} · target ${pts(t['u_target'])} · buying a ${up ? 'call' : 'put'}');
  }
  if (_past(_d(t['u_stop']), ltp, t['side'], false)) {
    return _cancel(db, t, '${_name(t)} back at the stop (${pts(ltp)}) before the option was bought', tell: true);
  }
  if (_past(_d(t['u_target']), ltp, t['side'], true)) {
    return _cancel(db, t, '${_name(t)} already at the target (${pts(ltp)}) before the option was bought', tell: true);
  }
  await _buy(db, k, s, t, ltp);
}

/// Picks the contract and sends the buy. A missing contract or a bad quote waits for the next loop.
Future<void> _buy(Db db, Kotak k, Settings s, Map t0, double spot) async {
  var t = t0;
  final key = t['key'] as String;
  if (t['tsym'] == null) {
    final setup = {'key': key, 'idx': t['idx'], 'high': t['high'], 'low': t['low']};
    final plan = await planFor(k, setup, t['opt'], spot, istNow(), s.optionMinDays);
    if (plan == null) {
      await db.saveTrade(key, {'note': 'no ${t['opt']} contract found ${s.optionMinDays}+ days out; retrying'});
      return;
    }
    await db.saveTrade(key, {
      'tsym': plan.tsym, 'token': plan.token, 'segment': plan.segment, 'expiry': dstr(plan.expiry),
      'strike': plan.strike, 'lot': plan.lot, 'tick': plan.tick,
    });
    t = (await db.trade(key))!;
  }
  final q = await optionQuoteFor(k, t);
  final bid = q['bid'], ask = q['ask'];
  if (bid == null || ask == null) {
    await db.saveTrade(key, {'note': '${t['tsym']} has no two-sided quote (bid $bid, ask $ask); retrying'});
    return;
  }
  if ((ask - bid) / ((ask + bid) / 2) * 100 > s.maxSpreadPct) {
    await db.saveTrade(key, {'note': '${t['tsym']} spread too wide (₹${pts(bid)} / ₹${pts(ask)}); retrying'});
    return;
  }
  // re-check the switches right before the order: a switch-off takes effect even mid-loop
  if (!enabled(await Settings.load())) return _cancel(db, t, 'auto-trade switched off');
  final px = protect(ask, 'BUY', s.optionProtectionPct, (t['tick'] as num?)?.toDouble() ?? 0.05);
  final lot = t['lot'] as int;
  final qty = math.max(1, s.autoLots) * lot;
  final oid = await k.placeOrder(segment: t['segment'], tsym: t['tsym'], side: 'B', qty: qty, price: px);
  await db.saveTrade(key, {'state': buying, 'qty': qty, 'buy_order': oid, 'buy_price': px, 'buy_at': tsstr(istNow()),
    'note': null});
  await _n('🛒 BUY ${t['tsym']} × $qty (${qty ~/ lot} lot)\nLimit ₹${pts(px)} (≈${rupees(px * qty)}) · '
      '${_name(t)} ${pts(spot)} · exit at ${pts(t['u_stop'])} / ${pts(t['u_target'])} or 15:15 on day '
      '${nr7.holdDays} · order $oid');
}

Future<void> _stepBuying(Db db, Kotak k, Map t, Map book, bool on) async {
  final key = t['key'] as String;
  Map? o = book[t['buy_order']];
  var os = orderState(o);
  if (os == 'OPEN' && (!on || _age(t['buy_at']) > repriceSec || !marketOpen())) {
    await k.cancelOrder(t['buy_order']); // not filled at that price: back to waiting, bought again at a fresh one
    o = (await k.orderBook())[t['buy_order']];
    os = orderState(o);
    if (os == 'OPEN') return;
  }
  final (qty, avg) = filled(o);
  if (os == 'FILLED' || qty > 0) {
    if (os == 'OPEN') return; // partly filled and still working
    await _opened(db, k, t, qty > 0 ? qty : t['qty'], avg ?? _d(t['buy_price']));
    return;
  }
  if (os == 'REJECTED') {
    await db.saveTrade(key, {'state': rejected, 'note': reason(o)});
    await _n('❌ ${t['tsym']} buy rejected by Kotak\n${reason(o)}');
    return;
  }
  if (os == 'CANCELLED' || os == 'GONE') {
    await db.saveTrade(key, {
      'state': on ? wait : cancelled, 'buy_order': null, 'buy_price': null, 'buy_at': null,
      'note': on ? 'buy not filled; buying again at a fresh price' : 'auto-trade switched off',
    });
  }
}

/// Net quantity of the option in Kotak's positions; null if they don't list it.
Future<int?> _held(Kotak k, Map t) async {
  for (final row in await k.positions()) {
    final (tsym, net) = positionOf(row);
    if (tsym == '${t['tsym'] ?? ''}'.toUpperCase()) return net;
  }
  return null;
}

Future<void> _opened(Db db, Kotak k, Map t, int qty, double fill) async {
  int? held;
  try {
    held = await _held(k, t);
  } on BrokerError {
    held = null;
  }
  final seen = held != null && held != 0;
  await db.saveTrade(t['key'], {'state': open, 'qty': qty, 'fill_price': fill, 'opened_at': tsstr(istNow()),
    'pos_seen': seen ? 1 : 0, 'note': null});
  await _n('✅ ${t['tsym']} bought × $qty @ ₹${pts(fill)}\n${rupees(fill * qty)} premium · ${_name(t)} ${t['side']} '
      'from ${pts(t['u_entry'])}, stop ${pts(t['u_stop'])}, target ${pts(t['u_target'])}'
      '${seen ? '' : ' · ⚠️ not yet seen in Kotak positions'}');
}

// ---- exit ----

/// Session of the trade (entry day = 1): the tracker's count for the same trade, else weekdays.
int _dayNo(Map t, Map? setup, DateTime today) {
  if (setup != null && setup['side'] == t['side'] && setup['day_no'] != null &&
      ('${setup['entry_at'] ?? ''}'.length >= 10 && '${setup['entry_at']}'.substring(0, 10) == t['trade_day'])) {
    return setup['day_no'] as int;
  }
  return busdayCount(parseIst(t['trade_day']), today) + 1;
}

String? exitReason(Map t, Map? setup, double? px, DateTime now) {
  if (px != null && _past(_d(t['u_stop']), px, t['side'], false)) return 'stop-loss (${_name(t)} ${pts(px)})';
  if (px != null && _past(_d(t['u_target']), px, t['side'], true)) return 'target (${_name(t)} ${pts(px)})';
  final day = _dayNo(t, setup, now);
  if (day > nr7.holdDays || (day == nr7.holdDays && mins(now) >= nr7.exitAt)) return 'day ${nr7.holdDays} exit';
  if (setup != null && nr7.closed.contains(setup['status']) && setup['side'] == t['side'] &&
      '${setup['entry_at'] ?? ''}'.startsWith('${t['trade_day']}')) {
    return 'the NR7 tracker closed it (${'${setup['status']}'.toLowerCase()})';
  }
  return null;
}

Future<void> _stepOpen(Db db, Kotak k, Settings s, Map t, Map? setup) async {
  if (!marketOpen()) return;
  final q = await indexQuoteFor(k, t['idx']);
  final why = exitReason(t, setup, q == null ? null : _d(q['ltp']), istNow());
  if (why != null) await _sell(db, k, s, t, why);
}

Future<void> _sell(Db db, Kotak k, Settings s, Map t, String why) async {
  final held = await _held(k, t);
  if ((held != null && held <= 0) || (held == null && t['pos_seen'] == 1)) {
    await db.saveTrade(t['key'], {'state': closedState, 'closed_at': tsstr(istNow()), 'exit_why': why,
      'note': 'no open position at Kotak (closed outside the app?); nothing sold'});
    await _n('⚠️ ${t['tsym']}: time to exit ($why)\nKotak shows no open position, so nothing was sold. '
        'Check the Kotak app.');
    return;
  }
  final qty = held != null && held > 0 ? math.min(held, t['qty'] as int) : t['qty'] as int;
  final q = await optionQuoteFor(k, t);
  final ref = q['bid'] ?? q['ltp'];
  if (ref == null || ref <= 0) throw BrokerError("no live price for ${t['tsym']}, so the sell can't be priced");
  final px = protect(ref, 'SELL', s.optionProtectionPct, (t['tick'] as num?)?.toDouble() ?? 0.05);
  final oid = await k.placeOrder(segment: t['segment'], tsym: t['tsym'], side: 'S', qty: qty, price: px);
  await db.saveTrade(t['key'], {'state': selling, 'qty': qty, 'sell_order': oid, 'sell_at': tsstr(istNow()),
    'exit_why': why, 'note': null});
  await _n('📤 ${t['tsym']} selling × $qty\n$why · limit ₹${pts(px)}');
}

Future<void> _stepSelling(Db db, Kotak k, Settings s, Map t, Map book) async {
  Map? o = book[t['sell_order']];
  var os = orderState(o);
  if (os == 'OPEN') {
    if (!(marketOpen() && _age(t['sell_at']) > repriceSec)) return;
    await k.cancelOrder(t['sell_order']); // the price moved away: sell again at a fresh one
    o = (await k.orderBook())[t['sell_order']];
    os = orderState(o);
    if (os == 'OPEN') return;
  }
  if (os == 'FILLED') return _closed(db, t, filled(o).$2);
  if (os == 'REJECTED' || os == 'CANCELLED' || (os == 'GONE' && marketOpen())) {
    if (os == 'REJECTED') await _n('⚠️ ${t['tsym']} sell rejected (${reason(o)}); trying again');
    final sold = filled(o).$1;
    if (sold > 0) await db.saveTrade(t['key'], {'qty': (t['qty'] as int) - sold});
    await _sell(db, k, s, (await db.trade(t['key']))!, (t['exit_why'] as String?) ?? 'exit');
  }
}

Future<void> _closed(Db db, Map t, double? price) async {
  await db.saveTrade(t['key'], {'state': closedState, 'exit_price': price, 'closed_at': tsstr(istNow())});
  final fill = (t['fill_price'] as num?)?.toDouble();
  final pnl = price != null && fill != null ? (price - fill) * (t['qty'] as int) : null;
  await _n('🏁 ${t['tsym']} sold (${t['exit_why'] ?? 'exit'}) @ ₹${pts(price ?? 0)}'
      '${pnl == null ? '' : '\nP&L ${rupees(pnl)} before charges'}');
}

/// From the app: a waiting trade is cancelled, a buy in progress cancelled (anything bought is sold), an open one
/// sold now. Sells only while NSE is open.
Future<void> closeTrade(String key) async {
  final db = await Db.open();
  final s = await Settings.load();
  var t = await db.trade(key);
  if (t == null) throw StateError('Unknown NR7 trade');
  if (t['state'] == selling) throw StateError('${t['tsym']} is already being sold');
  if (![wait, buying, open].contains(t['state'])) {
    throw StateError('This NR7 trade is not open (${'${t['state']}'.toLowerCase()})');
  }
  final k = await kotakFactory();
  if (t['state'] == buying) {
    var o = (await k.orderBook())[t['buy_order']];
    if (orderState(o) == 'OPEN') {
      await k.cancelOrder(t['buy_order']);
      o = (await k.orderBook())[t['buy_order']];
    }
    final (qty, avg) = filled(o);
    if (qty > 0) {
      await _opened(db, k, t, qty, avg ?? _d(t['buy_price']));
      t = (await db.trade(key))!;
    }
  }
  if (t['state'] != open) {
    await db.saveTrade(key, {'state': cancelled, 'note': closeNote});
    await _n('✖️ ${_name(t)} NR7 trade cancelled ($closeNote)');
    return;
  }
  if (!marketOpen()) throw StateError('Options are sold only while NSE is open (the price would be stale)');
  await _sell(db, k, s, t, closeNote);
}
