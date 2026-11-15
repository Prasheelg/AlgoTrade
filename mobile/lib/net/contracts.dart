/// Index futures and options from Kotak's scrip master (fno.py parse_index_master), and the in-the-money option an
/// NR7 trade buys (nr7_instruments.py option_plan). The day's contracts are cached on the phone.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:math' as math;

import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import '../core/nr7.dart';
import '../core/options_math.dart';
import '../core/time.dart';
import 'kotak.dart';

/// index -> (F&O segment, cash segment, the index's quote name there)
const quoteNames = {
  'NIFTY': ('nse_fo', 'nse_cm', 'Nifty 50'),
  'BANKNIFTY': ('nse_fo', 'nse_cm', 'Nifty Bank'),
  'SENSEX': ('bse_fo', 'bse_cm', 'SENSEX'),
};
const epoch1980 = 315532800; // NSE expiries count seconds from 1980-01-01; BSE's are Unix seconds
const deltaRange = (0.6, 0.8);
const deltaAim = 0.7;
const itmStrikes = 20;

class Contract {
  final String underlying, opt, token, tsym; // opt: CE / PE / FUT
  final DateTime expiry; // date, naive IST
  final double? strike;
  final int lot;
  final double tick;
  const Contract(this.underlying, this.opt, this.expiry, this.strike, this.lot, this.token, this.tsym, this.tick);

  Map<String, dynamic> toJson() =>
      {'u': underlying, 'o': opt, 'e': dstr(expiry), 's': strike, 'l': lot, 't': token, 'y': tsym, 'k': tick};
  factory Contract.fromJson(Map j) => Contract(j['u'], j['o'], parseIst(j['e']), (j['s'] as num?)?.toDouble(),
      j['l'], j['t'], j['y'], (j['k'] as num).toDouble());
}

List<String> _splitCsv(String line) {
  final out = <String>[];
  final b = StringBuffer();
  var q = false;
  for (var i = 0; i < line.length; i++) {
    final ch = line[i];
    if (ch == '"') {
      if (q && i + 1 < line.length && line[i + 1] == '"') {
        b.write('"');
        i++;
      } else {
        q = !q;
      }
    } else if (ch == ',' && !q) {
      out.add(b.toString());
      b.clear();
    } else {
      b.write(ch);
    }
  }
  out.add(b.toString());
  return out;
}

/// Index futures (opt FUT) and options (CE / PE) of [underlyings] from a segment's scrip master CSV.
List<Contract> parseIndexMaster(String csv, String segment, Set<String> underlyings) {
  final kinds = segment == 'bse_fo' ? {'IF': 'FUT', 'IO': 'OPT'} : {'FUTIDX': 'FUT', 'OPTIDX': 'OPT'};
  final epoch0 = segment == 'bse_fo' ? 0 : epoch1980;
  final lines = const LineSplitter().convert(csv);
  if (lines.isEmpty) return [];
  final head = _splitCsv(lines.first).map((c) => c.trim().replaceAll(RegExp(r';+$'), '')).toList();
  int col(String n) => head.indexOf(n);
  final iSym = col('pSymbolName'), iInst = col('pInstType'), iOpt = col('pOptionType'), iExp = col('pExpiryDate'),
      iStrike = col('dStrikePrice'), iLot = col('lLotSize'), iTok = col('pSymbol'), iTs = col('pTrdSymbol'),
      iTick = col('dTickSize');
  if ([iSym, iInst, iExp, iTok, iTs].contains(-1)) throw FormatException('Unexpected scrip master columns');
  final out = <Contract>[];
  for (final line in lines.skip(1)) {
    if (line.isEmpty) continue;
    final r = _splitCsv(line);
    String at(int i) => i >= 0 && i < r.length ? r[i].trim() : '';
    final u = at(iSym);
    if (!underlyings.contains(u)) continue;
    final inst = kinds[at(iInst)];
    if (inst == null) continue;
    final exp = num.tryParse(at(iExp));
    if (exp == null) continue;
    final expiry = dateOnly(istFromEpochSec(exp + epoch0));
    final strike = inst == 'OPT' ? (num.tryParse(at(iStrike)) ?? 0) / 100 : null;
    out.add(Contract(u, inst == 'OPT' ? at(iOpt) : 'FUT', expiry, strike?.toDouble(),
        (num.tryParse(at(iLot)) ?? 0).toInt(), at(iTok), at(iTs), (num.tryParse(at(iTick)) ?? 5) / 100));
  }
  return out;
}

final _mem = <String, (String, List<Contract>)>{};

/// An index's live futures and options (NIFTY / BANKNIFTY on nse_fo, SENSEX on bse_fo), refreshed once a day.
Future<List<Contract>> indexContracts(Kotak k, String idx) async {
  final seg = quoteNames[idx]!.$1;
  final today = dstr(istNow());
  var hit = _mem[seg];
  if (hit == null || hit.$1 != today) {
    final dir = await getApplicationSupportDirectory();
    final file = File(p.join(dir.path, 'contracts_${seg}_$today.json'));
    List<Contract> all;
    if (await file.exists()) {
      all = [for (final j in jsonDecode(await file.readAsString()) as List) Contract.fromJson(j)];
    } else {
      final csv = await k.download(await k.scripMasterUrl(seg));
      final wanted = {for (final e in quoteNames.entries) if (e.value.$1 == seg) e.key};
      all = await Isolate.run(() => parseIndexMaster(csv, seg, wanted));
      await file.writeAsString(jsonEncode([for (final c in all) c.toJson()]));
      for (final old in dir.listSync().whereType<File>()) {
        final n = p.basename(old.path);
        if (n.startsWith('contracts_${seg}_') && old.path != file.path) old.deleteSync();
      }
    }
    final d0 = parseIst(today);
    hit = _mem[seg] = (today, [for (final c in all) if (!c.expiry.isBefore(d0)) c]);
  }
  return [for (final c in hit.$2) if (c.underlying == idx) c];
}

/// Lot size from Kotak's contract list, else the default.
Future<int> lotSize(Kotak? k, String idx) async {
  if (k != null && k.creds.consumerKey.isNotEmpty) {
    try {
      final cs = await indexContracts(k, idx);
      if (cs.isNotEmpty) return cs.map((c) => c.lot).reduce(math.max);
    } catch (_) {}
  }
  return indices[idx]!.defaultLot;
}

/// Calendar days (fractional) until 15:30 IST on expiry day.
double daysToExpiry(DateTime expiry, DateTime now) {
  final end = expiry.add(const Duration(hours: 15, minutes: 30));
  return math.max(end.difference(now).inSeconds / 86400, 0.0);
}

/// The nearest monthly expiry (the last of its month) at least [minDays] calendar days away.
DateTime? pickExpiry(Iterable<DateTime> expiries, DateTime today, int minDays) {
  final last = <String, DateTime>{};
  for (final e in (expiries.toSet().toList()..sort())) {
    last['${e.year}-${e.month}'] = e;
  }
  final monthly = last.values.toList()..sort();
  for (final e in monthly) {
    if (dateOnly(e).difference(dateOnly(today)).inDays >= minDays) return e;
  }
  return null;
}

/// Mid of the best bid and ask, else the last price, else the previous close.
double? midPrice(Map q) {
  final o = parseOptionQuote(q);
  if (o['bid'] != null && o['ask'] != null) return r2((o['bid']! + o['ask']!) / 2);
  return o['ltp'] ?? f((q['ohlc'] as Map?)?['close']);
}

class OptionPlan {
  final String tsym, token, segment;
  final DateTime expiry;
  final double strike, premium, delta, tick;
  final double? iv;
  final int lot;
  const OptionPlan(this.tsym, this.token, this.segment, this.expiry, this.strike, this.premium, this.delta,
      this.iv, this.lot, this.tick);
}

/// The in-the-money monthly call (CE, for the buy level) or put (PE, the sell level) with delta at the level
/// nearest [deltaAim], expiring at least [minDays] days away.
Future<OptionPlan?> optionPlan(Kotak k, Map setup, String opt, double? spot, DateTime now, int minDays) async {
  final idx = setup['idx'] as String;
  final level = ((opt == 'CE' ? setup['high'] : setup['low']) as num).toDouble();
  final seg = quoteNames[idx]!.$1;
  final all = [for (final c in await indexContracts(k, idx)) if (c.opt == opt) c];
  final exp = pickExpiry(all.map((c) => c.expiry), now, minDays);
  if (exp == null) return null;
  var rows = [for (final c in all) if (c.expiry == exp) c];
  final strikes = rows.map((c) => c.strike!).toSet().toList()..sort();
  final itm = opt == 'CE'
      ? strikes.reversed.where((s) => s <= level).take(itmStrikes).toSet()
      : strikes.where((s) => s >= level).take(itmStrikes).toSet();
  rows = [for (final c in rows) if (itm.contains(c.strike)) c];
  if (rows.isEmpty) return null;
  final qs = {for (final q in await k.quotes([for (final c in rows) (seg, c.token)])) '${q['exchange_token']}': q};
  final years = daysToExpiry(exp, now) / 365;
  (bool, double)? bestRank;
  OptionPlan? best;
  for (final c in rows) {
    final q = qs[c.token];
    final px = q == null ? null : midPrice(q);
    if (px == null || px <= 0) continue;
    final iv = years > 0 ? impliedVol(opt, px, spot ?? level, c.strike!, years) : null;
    final d = delta(opt, level, c.strike!, years, iv ?? 0).abs();
    final rank = (!(deltaRange.$1 <= d && d <= deltaRange.$2), (d - deltaAim).abs());
    final better = bestRank == null ||
        (rank.$1 != bestRank.$1 ? !rank.$1 : rank.$2 < bestRank.$2); // inside the range first, then nearest
    if (better) {
      bestRank = rank;
      best = OptionPlan(c.tsym, c.token, seg, exp, c.strike!, px, r2(d), iv == null ? null : r2(iv * 100), c.lot,
          c.tick);
    }
  }
  return best;
}

/// Kotak's live index quote: ltp and the day's open / high / low; null if unavailable or not from today.
Future<Map<String, dynamic>?> indexQuote(Kotak k, String idx) async {
  final (_, cash, name) = quoteNames[idx]!;
  try {
    final res = await k.quotes([(cash, name)]);
    final q = res.isEmpty ? null : parseQuote(res.first);
    if (q == null) return null;
    final t = q['time'] as DateTime?;
    if (t != null && dstr(t) != dstr(istNow())) return null;
    return q;
  } catch (_) {
    return null;
  }
}

Future<Map<String, double?>> optionQuote(Kotak k, String segment, String token, String tsym) async {
  final res = await k.quotes([(segment, token)]);
  if (res.isEmpty) throw BrokerError('no quote for $tsym');
  return parseOptionQuote(res.first);
}
