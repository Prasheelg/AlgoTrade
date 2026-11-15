/// NR7 Breakout on Nifty, Bank Nifty and Sensex (Toby Crabel): the pattern and the trade rules, ported one for one
/// from algotrade/nr7.py (pure functions; storage and fetching live in engine/).
///
/// - A session whose high-low range is the narrowest of the last [nrDays] gives a setup for the next session only:
///   buy above the NR7 high, sell below its low. The first level traded is the trade, the other is cancelled.
/// - Stop at the NR7 bar's other end; 1R = entry to stop; target [targetR] from the entry; closed at [exitAt] on
///   the [holdDays]-th session (entry day = 1). Not triggered in that session: expired. One trade per index.
library;

import 'dart:math' as math;

import 'time.dart';

class IndexInfo {
  final String name, yahoo;
  final int defaultLot;
  final double costPct; // round trip, % of the index (backtest)
  const IndexInfo(this.name, this.yahoo, this.defaultLot, this.costPct);
}

const indices = {
  'NIFTY': IndexInfo('Nifty 50', '^NSEI', 65, 0.017),
  'BANKNIFTY': IndexInfo('Bank Nifty', '^NSEBANK', 30, 0.022),
  'SENSEX': IndexInfo('Sensex', '^BSESN', 20, 0.015),
};

const nrDays = 7;
const targetR = 3.0;
const holdDays = 5;
final exitAt = hm(15, 15); // time exit on the last day
final sessionEnd = hm(15, 30);
final scanAfter = hm(16, 0); // the day's candle is final by then
final scanGiveUp = hm(21, 0); // no candle for today by then: a holiday

const wait = 'WAIT', open = 'OPEN';
const active = [wait, open];
const closed = ['STOP', 'TARGET', 'TIME'];

class Bar {
  final DateTime ts; // naive IST
  final double o, h, l, c;
  const Bar(this.ts, this.o, this.h, this.l, this.c);
  String get day => dstr(ts);
}

/// Diwali Muhurat sessions: an hour of trading, so a fake narrow day (the playbook leaves them out).
const muhurat = {
  '2000-10-26', '2001-11-14', '2002-11-04', '2003-10-25', '2004-11-12', '2005-11-01', '2006-10-21',
  '2007-11-09', '2008-10-28', '2009-10-17', '2010-11-05', '2011-10-26', '2012-11-13', '2013-11-03',
  '2014-10-23', '2015-11-11', '2016-10-30', '2017-10-19', '2018-11-07', '2019-10-27', '2020-11-14',
  '2021-11-04', '2022-10-24', '2023-11-12', '2024-11-01', '2025-10-21', '2026-11-08',
};

/// Weekend sessions that were full trading days (Union Budget).
const fullWeekendSessions = {'2015-02-28', '2020-02-01', '2025-02-01', '2026-02-01'};

bool isSessionDay(DateTime d) {
  final s = dstr(d);
  return !muhurat.contains(s) && (isWeekday(d) || fullWeekendSessions.contains(s));
}

/// Daily candles of real full sessions only: no Muhurat, no short weekend specials, no close-only records.
List<Bar> sessions(List<Bar> df) => [for (final b in df) if (isSessionDay(b.ts) && b.h > b.l) b];

/// Minute bars on real trading days only.
List<Bar> realDays(List<Bar> df) => [for (final b in df) if (isSessionDay(b.ts)) b];

/// The last bar's range is the narrowest of the last [nrDays] (ties count).
bool isNr7(List<Bar> df) {
  if (df.length < nrDays) return false;
  final rng = [for (final b in df.sublist(df.length - nrDays)) b.h - b.l];
  final last = rng.last;
  return last > 0 && last <= rng.reduce(math.min);
}

double r2(num x) => (x * 100).roundToDouble() / 100;
double r3(num x) => (x * 1000).roundToDouble() / 1000;

Map<String, dynamic> newSetup(String idx, DateTime barDate, double high, double low) => {
      'key': '$idx|${dstr(barDate)}', 'idx': idx, 'nr7_date': dstr(barDate), 'trigger_day': null,
      'high': r2(high), 'low': r2(low), 'risk': r2(high - low),
      'status': wait, 'side': null, 'entry': null, 'entry_at': null, 'stop': null, 'target': null,
      'day_no': null, 'exit': null, 'exit_at': null, 'r': null, 'note': null, 'last_bar': null,
    };

double tradeRisk(Map t) => ((t['entry'] as num) - (t['stop'] as num)).abs().toDouble();

void _close(Map<String, dynamic> t, double px, String how, DateTime ts) {
  final sign = t['side'] == 'LONG' ? 1 : -1;
  t['status'] = how;
  t['exit'] = r2(px);
  t['exit_at'] = tsstr(ts);
  t['r'] = r2(sign * (px - (t['entry'] as num)) / tradeRisk(t));
}

/// Walks a WAIT or OPEN setup through bars (in order) and returns it updated. Daily bars: the time exit is the
/// close of day [holdDays]; minute bars: the first bar from [exitAt]. [forceSide]: only that level can trigger.
Map<String, dynamic> advance(Map t0, List<Bar> bars, {bool daily = false, String? forceSide}) {
  final t = Map<String, dynamic>.from(t0);
  for (final b in bars) {
    if (!active.contains(t['status'])) break;
    final d = b.day;
    if (d.compareTo(t['nr7_date']) <= 0) continue;
    final prevDay = ((t['last_bar'] as String?) ?? '').padRight(10).substring(0, 10).trim();
    t['last_bar'] = tsstr(b.ts);
    final high = (t['high'] as num).toDouble(), low = (t['low'] as num).toDouble();
    if (t['status'] == wait) {
      if (t['trigger_day'] == null) {
        t['trigger_day'] = d; // the next session, whatever the calendar says
      } else if (d.compareTo(t['trigger_day']) > 0) {
        t['status'] = 'EXPIRED';
        t['note'] = 'Neither level traded';
        break;
      }
      var up = b.h > high, down = b.l < low;
      if (forceSide != null) {
        if (forceSide == 'LONG') {
          down = false;
        } else {
          up = false;
        }
      }
      if (!(up || down)) continue;
      if (up && down && low <= b.o && b.o <= high) up = high - b.o <= b.o - low; // the nearer one came first
      final long = up;
      final entry = long ? math.max(b.o, high) : math.min(b.o, low); // a gap past the level fills at the open
      final sign = long ? 1 : -1;
      final stop = long ? low : high;
      t.addAll({
        'status': open, 'side': long ? 'LONG' : 'SHORT', 'entry': r2(entry), 'entry_at': tsstr(b.ts),
        'stop': stop, 'target': r2(entry + sign * targetR * (entry - stop).abs()), 'day_no': 1,
      });
      if (long ? b.l <= stop : b.h >= stop) _close(t, stop, 'STOP', b.ts); // the stop traded in the entry bar too
      continue;
    }
    // OPEN (the entry bar never reaches the target: the order of moves inside a bar is unknown)
    if (d != prevDay) t['day_no'] = (t['day_no'] as int) + 1;
    final long = t['side'] == 'LONG';
    final stop = (t['stop'] as num).toDouble(), target = (t['target'] as num).toDouble();
    if (long ? b.o <= stop : b.o >= stop) {
      _close(t, b.o, 'STOP', b.ts);
    } else if (long ? b.o >= target : b.o <= target) {
      _close(t, b.o, 'TARGET', b.ts);
    } else if (long ? b.l <= stop : b.h >= stop) {
      _close(t, stop, 'STOP', b.ts);
    } else if (long ? b.h >= target : b.l <= target) {
      _close(t, target, 'TARGET', b.ts);
    } else if ((t['day_no'] as int) >= holdDays) {
      if (daily) {
        _close(t, b.c, 'TIME', b.ts);
      } else if (mins(b.ts) >= exitAt) {
        _close(t, b.o, 'TIME', b.ts);
      }
    }
  }
  return t;
}

/// Every NR7 setup on daily candles, oldest first, under the live rules: one trade per index at a time (an NR7
/// while a trade is open, or on the day it closed, is SKIPPED), an NR7 whose next session is a skip day is
/// SKIPPED, the rest walked through the following sessions. A closed trade's R is after costs.
Iterable<Map<String, dynamic>> walk(List<Bar> df, String idx, {double? costPct, Set<String> skipDays = const {}}) sync* {
  final cost = costPct ?? indices[idx]!.costPct;
  if (df.isEmpty) return;
  final lastDay = df.last.day;
  String? busyUntil; // the exit timestamp of the last trade on this index
  for (var i = nrDays - 1; i < df.length; i++) {
    final rng = df[i].h - df[i].l;
    var mn = double.infinity;
    for (var j = i - nrDays + 1; j <= i; j++) {
      mn = math.min(mn, df[j].h - df[j].l);
    }
    if (!(rng > 0 && rng <= mn)) continue;
    final d = df[i];
    var t = newSetup(idx, d.ts, d.h, d.l);
    if ((t['risk'] as num) <= 0) continue;
    final nxt = dstr(nextBDay(d.ts));
    if (busyUntil != null && tsstr(d.ts).compareTo(busyUntil) <= 0) {
      t.addAll({'status': 'SKIPPED', 'note': 'already a trade on this index'});
    } else if (skipDays.contains(nxt)) {
      t.addAll({'status': 'SKIPPED', 'note': '$nxt is a skip day'});
    } else {
      t = advance(t, df.sublist(i + 1, math.min(df.length, i + 2 + holdDays + 1)), daily: true);
      if (t['status'] == wait && t['trigger_day'] != null && (t['trigger_day'] as String).compareTo(lastDay) <= 0) {
        t.addAll({'status': 'EXPIRED', 'note': 'Neither level traded'});
      }
      if (closed.contains(t['status'])) {
        final c = cost / 100 * (t['entry'] as num);
        t['r'] = r3((t['r'] as num) - c / tradeRisk(t));
        t['net_pts'] = r2((t['side'] == 'LONG' ? 1 : -1) * ((t['exit'] as num) - (t['entry'] as num)) - c);
        t['days'] = t['day_no'];
        busyUntil = t['exit_at'];
      } else if (t['status'] == open) {
        busyUntil = '9999';
      }
    }
    yield t;
  }
}

Map<String, dynamic> summarize(List<Map> trades) {
  if (trades.isEmpty) return {'trades': 0};
  final r = [for (final t in trades) (t['r'] as num).toDouble()];
  final wins = r.where((x) => x > 0).toList(), losses = r.where((x) => x <= 0).toList();
  double sum(List<double> l) => l.fold(0.0, (a, b) => a + b);
  var eq = 0.0, peak = 0.0, dd = 0.0, run = 0, streak = 0;
  for (final x in r) {
    eq += x;
    peak = math.max(peak, eq);
    dd = math.max(dd, peak - eq);
    run = x <= 0 ? run + 1 : 0;
    streak = math.max(streak, run);
  }
  final avgWin = wins.isEmpty ? 0.0 : sum(wins) / wins.length;
  final avgLoss = losses.isEmpty ? 0.0 : -sum(losses) / losses.length;
  final exits = <String, int>{};
  for (final t in trades) {
    exits[t['status']] = (exits[t['status']] ?? 0) + 1;
  }
  return {
    'trades': r.length, 'wins': wins.length, 'losses': losses.length,
    'win_pct': (wins.length / r.length * 1000).roundToDouble() / 10,
    'avg_r': r3(sum(r) / r.length), 'total_r': (sum(r) * 10).roundToDouble() / 10,
    'best_r': r2(r.reduce(math.max)), 'worst_r': r2(r.reduce(math.min)), 'max_losing_streak': streak,
    'profit_factor': sum(losses) < 0 ? r2(sum(wins) / -sum(losses)) : null,
    'avg_win_r': r2(avgWin), 'avg_loss_r': r2(avgLoss), 'max_dd_r': (dd * 10).roundToDouble() / 10,
    'exits': exits,
  };
}

/// (lots, lot size): futures lots that risk at most riskPct of capital at the NR7 range.
(int, int) lotsFor(double risk, int lot, double capital, double riskPct) {
  final budget = capital * riskPct / 100;
  return (risk > 0 ? (budget / (risk * lot)).floor() : 0, lot);
}
