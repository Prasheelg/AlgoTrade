/// The NR7 tracker on the phone (nr7.py scan / check / history): the evening scan for new setups, and moving the
/// waiting / open setups on through Yahoo's 1-minute index bars. Alerts only; orders are engine/auto_trade.dart.
library;

import 'package:intl/intl.dart';

import '../core/nr7.dart';
import '../core/time.dart';
import '../data/db.dart';
import '../data/settings.dart';
import '../net/kotak.dart';
import '../net/contracts.dart' as contracts;
import '../net/yahoo.dart' as yahoo;
import 'notify.dart';

const lastScanKey = 'nr7_last_scan';
const pollSec = 30; // minute bars are fetched this often in market hours
const idlePollSec = 900; // and this often outside them

/// Injectable for tests.
Future<Map<String, List<Bar>>> Function({String range}) dailyBars = ({String range = '1y'}) => yahoo.daily(range: range);
Future<List<Bar>> Function(String idx) minuteBars = yahoo.minuteBars;
Future<int> Function(String idx) lotFor = (idx) async {
  try {
    return await contracts.lotSize(await Kotak.create(), idx);
  } catch (_) {
    return indices[idx]!.defaultLot;
  }
};

final _fetched = <String, DateTime>{};
final _num = NumberFormat('#,##0.00');
final _rs = NumberFormat('#,##0');
String pts(num x) => _num.format(x);
String rupees(num x) => '₹${_rs.format(x)}';

String setupText(Map t) {
  final name = indices[t['idx']]!.name;
  if (t['status'] == 'SKIPPED') return '📐 NR7 $name (${t['nr7_date']}) skipped\n${t['note'] ?? ''}';
  final r = (t['risk'] as num).toDouble(), hi = (t['high'] as num).toDouble(), lo = (t['low'] as num).toDouble();
  final lots = t['lots'] as int, lot = t['lot'] as int;
  return '📐 NR7 $name (${t['nr7_date']}) · next session only\n'
      'Buy above ${pts(hi)} · stop ${pts(lo)} · target ${pts(hi + targetR * r)}\n'
      'Sell below ${pts(lo)} · stop ${pts(hi)} · target ${pts(lo - targetR * r)}\n'
      'Risk ${pts(r)} pts · $lots lot${lots == 1 ? '' : 's'} of $lot'
      '${lots > 0 ? '' : ' (one lot risks ${rupees(r * lot)}, over your risk per trade)'}';
}

String? eventText(Map t) {
  final name = indices[t['idx']]!.name;
  if (t['status'] == open) {
    return '📐 NR7 $name ${t['side']} at ${pts(t['entry'])}\n'
        'Stop ${pts(t['stop'])} · target ${pts(t['target'])} · close by 15:15 on day $holdDays';
  }
  if (closed.contains(t['status'])) {
    final how = {'STOP': 'stopped out', 'TARGET': 'target hit', 'TIME': 'closed on day $holdDays'}[t['status']];
    return '📐 NR7 $name ${t['side']} $how\nat ${pts(t['exit'])} · ${(t['r'] as num) >= 0 ? '+' : ''}'
        '${(t['r'] as num).toStringAsFixed(2)}R';
  }
  if (t['status'] == 'EXPIRED') return '📐 NR7 $name (${t['nr7_date']}) expired\n${t['note'] ?? ''}';
  return null;
}

/// New setups from the latest completed session; once a day after 16:00 IST unless forced (Scan now). On a trading
/// day whose previous session was never scanned (the phone was off that evening) it catches up at once, so the
/// setup is there for this session. Returns the setups created.
Future<List<Map<String, dynamic>>> scan({DateTime? now, bool force = false}) async {
  final db = await Db.open();
  final s = await Settings.load();
  now ??= istNow();
  final today = dateOnly(now);
  final todayS = dstr(today), prev = dstr(prevBDay(today));
  final last = await db.get<String>(lastScanKey);
  var catchUp = false;
  if (!force) {
    if (!s.on || !isSessionDay(today)) return [];
    catchUp = mins(now) < scanAfter && (last == null || last.compareTo(prev) < 0);
    if (!catchUp && (mins(now) < scanAfter || last == todayS)) return [];
  }
  final data = await dailyBars(range: '1y');
  if (data.isEmpty) {
    await notify('error', 'NR7 scan: no index data from Yahoo', alert: false);
    return [];
  }
  if (!force && !catchUp && data.values.every((df) => df.last.day.compareTo(todayS) < 0) && mins(now) < scanGiveUp) {
    return []; // today's candles aren't in yet: try again on the next loop
  }
  final rows = await db.setups();
  final activeIdx = {for (final t in rows) if (active.contains(t['status'])) t['idx']};
  final made = <Map<String, dynamic>>[];
  for (final e in data.entries) {
    final idx = e.key;
    var df = e.value;
    if (catchUp || (isSessionDay(today) && mins(now) <= sessionEnd)) {
      df = [for (final b in df) if (b.day.compareTo(todayS) < 0) b]; // today's session isn't complete
    }
    if (!isNr7(df)) continue;
    final bar = df.last;
    final t = newSetup(idx, bar.ts, bar.h, bar.l);
    final (lots, lot) = lotsFor((t['risk'] as num).toDouble(), await lotFor(idx), s.capital, s.riskPct);
    t['lots'] = lots;
    t['lot'] = lot;
    final nxt = dstr(nextBDay(bar.ts));
    // one trade per index: a trade that closed on the NR7 day itself still counts
    final closedThatDay = rows.any((x) =>
        x['idx'] == idx && closed.contains(x['status']) && ((x['exit_at'] as String?) ?? '').compareTo(t['nr7_date']) >= 0);
    // and the price history: a trade the playbook would hold although the app missed it (the phone was off)
    final replay = walk(df, idx, skipDays: s.skipDays.toSet()).where((x) => x['nr7_date'] == t['nr7_date']).firstOrNull;
    if (activeIdx.contains(idx)) {
      t.addAll({'status': 'SKIPPED', 'note': 'already a setup or trade on this index'});
    } else if (closedThatDay) {
      t.addAll({'status': 'SKIPPED', 'note': 'a trade on this index closed that day'});
    } else if (s.skipDays.contains(nxt)) {
      t.addAll({'status': 'SKIPPED', 'note': '$nxt is a skip day'});
    } else if (replay != null && replay['status'] == 'SKIPPED') {
      t.addAll({'status': 'SKIPPED', 'note': 'by the price history, ${replay['note']}'});
    }
    if (await db.setupExists(t['key'])) continue;
    await db.insertSetup(t);
    made.add(t);
    await notify('setup', setupText(t), alert: t['status'] != 'SKIPPED');
  }
  if (catchUp) {
    await db.set(lastScanKey, prev);
  } else if (!force) {
    await db.set(lastScanKey, todayS);
  }
  return made;
}

/// Moves the WAIT / OPEN setups on through the minute bars since they were last looked at: entries, stops,
/// targets, time exits and expiries, each alerted once. Returns how many setups changed.
Future<int> check({DateTime? now, bool forceFetch = false}) async {
  final s = await Settings.load();
  if (!s.on) return 0;
  final db = await Db.open();
  now ??= istNow();
  final live = [for (final t in await db.setups()) if (active.contains(t['status'])) t];
  if (live.isEmpty) return 0;
  final waitSec = marketOpen(now) || (isWeekday(now) && mins(now) <= scanAfter) ? pollSec : idlePollSec;
  var changed = 0;
  for (final idx in ({for (final t in live) t['idx'] as String}.toList()..sort())) {
    List<Bar>? bars;
    final lastFetch = _fetched[idx];
    if (forceFetch || lastFetch == null || now.difference(lastFetch).inSeconds >= waitSec) {
      _fetched[idx] = now;
      try {
        bars = await minuteBars(idx);
      } catch (e) {
        await db.log('error', 'NR7: minute bars for $idx failed ($e)');
      }
    }
    if (bars != null && bars.isNotEmpty) {
      bars = [for (final b in bars) if (!b.ts.add(const Duration(minutes: 1)).isAfter(now)) b]; // minute not done
    }
    for (final t in live.where((x) => x['idx'] == idx)) {
      final lb = t['last_bar'] as String?;
      final fresh = bars != null && lb != null ? [for (final b in bars) if (b.ts.isAfter(parseIst(lb))) b] : bars;
      final u = fresh != null && fresh.isNotEmpty ? advance(t, fresh) : Map<String, dynamic>.from(t);
      final td = u['trigger_day'] as String?;
      if (u['status'] == wait && td != null &&
          (td.compareTo(dstr(now)) < 0 || (td == dstr(now) && mins(now) > sessionEnd))) {
        u.addAll({'status': 'EXPIRED', 'note': 'Neither level traded'});
      }
      final diff = {for (final e in u.entries) if (t[e.key] != e.value) e.key: e.value};
      if (diff.isEmpty) continue;
      await db.updateSetup(t['key'], diff);
      changed++;
      if (diff.containsKey('status')) {
        if (closed.contains(diff['status']) && t['status'] == wait) {
          await notify('trade', eventText({...u, 'status': open})!); // in and out between two looks
        }
        final text = eventText(u);
        if (text != null) await notify(u['status'] == 'EXPIRED' ? 'setup' : 'trade', text);
      }
    }
  }
  return changed;
}

/// Every NR7 setup of the last [months] months on the three indices, replayed on daily candles under the live
/// rules, with each index's and the combined results (R after costs; rupees for one futures lot).
Future<Map<String, dynamic>> history({int months = 3}) async {
  final s = await Settings.load();
  final now = istNow();
  final today = dateOnly(now);
  final start = dstr(DateTime.utc(today.year, today.month - months, today.day));
  final raw = await dailyBars(range: months > 12 ? '5y' : '2y');
  final budget = s.capital * s.riskPct / 100;
  final rows = <Map<String, dynamic>>[], closedAll = <Map<String, dynamic>>[];
  final out = <String, Map<String, dynamic>>{};
  for (final e in raw.entries) {
    var df = e.value;
    if (isWeekday(now) && mins(now) <= sessionEnd) df = [for (final b in df) if (b.day.compareTo(dstr(today)) < 0) b];
    if (df.isEmpty) continue;
    final lot = await lotFor(e.key);
    final mine = [
      for (final t in walk(df, e.key, skipDays: s.skipDays.toSet())) if ((t['nr7_date'] as String).compareTo(start) >= 0) t
    ];
    for (final t in mine) {
      final risk = t['entry'] != null ? tradeRisk(t) : (t['risk'] as num).toDouble();
      t['lot'] = lot;
      t['lots'] = risk * lot > 0 ? (budget / (risk * lot)).floor() : 0;
      if (closed.contains(t['status'])) t['rs_per_lot'] = ((t['net_pts'] as num) * lot).round();
    }
    final cl = [for (final t in mine) if (closed.contains(t['status'])) t];
    out[e.key] = {
      ...summarize(cl), 'setups': mine.length, 'lot': lot,
      'rs_per_lot': cl.fold<int>(0, (a, t) => a + (t['rs_per_lot'] as int)),
    };
    rows.addAll(mine);
    closedAll.addAll(cl);
  }
  closedAll.sort((a, b) => (a['exit_at'] as String).compareTo(b['exit_at']));
  rows.sort((a, b) => '${b['nr7_date']}${b['idx']}'.compareTo('${a['nr7_date']}${a['idx']}'));
  return {
    'months': months, 'from': start, 'indices': out, 'trades': rows,
    'total': {...summarize(closedAll), 'setups': rows.length,
      'rs_per_lot': closedAll.fold<int>(0, (a, t) => a + (t['rs_per_lot'] as int))},
  };
}
