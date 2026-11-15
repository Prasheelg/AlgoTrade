/// Kotak Neo Trade API over plain HTTPS, ported from the calls the Python app makes through neo_api_client:
/// unattended TOTP + MPIN login (broker.py), quotes and scrip master (kotak.py / fno.py), and the order calls
/// nr7_auto.py uses. The day's session tokens are kept in the Keystore so the app and its background watcher share
/// one login. Nothing here logs a credential.
library;

import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import '../core/time.dart';
import '../core/totp.dart';
import '../data/settings.dart';

const prodBase = 'https://mis.kotaksecurities.com';
const sessionBase = 'https://mis.kotaksecurities.com';
const finKey = 'neotradeapi';
const orderSource = 'NEOTRADEAPI';
const quoteBatch = 49; // Kotak rejects 50 per call

class BrokerError implements Exception {
  final String message;
  BrokerError(this.message);
  @override
  String toString() => message;
}

class _Session {
  String token, sid, baseUrl, day;
  String? name;
  _Session(this.token, this.sid, this.baseUrl, this.day, this.name);
  Map<String, dynamic> toJson() => {'token': token, 'sid': sid, 'base': baseUrl, 'day': day, 'name': name};
  static _Session? fromJson(Map<String, dynamic>? j) =>
      j == null ? null : _Session(j['token'], j['sid'], j['base'], j['day'], j['name']);
}

/// Kotak reports an empty list as an error ({'stCode': 5203, 'errMsg': 'No Data'} or a 424 "No ...").
bool _isEmpty(Map res) {
  if ('${res['stCode']}' == '5203' || '${res['errMsg'] ?? ''}'.toLowerCase() == 'no data') return true;
  final err = res['error'] ?? res['Error'];
  final errs = err is List ? err : [err];
  return err != null &&
      errs.every((e) => e is Map && '${e['code']}' == '424' && '${e['message'] ?? ''}'.toLowerCase().startsWith('no '));
}

/// The API returns error bodies instead of failing: turn them into BrokerError.
dynamic _check(dynamic res, String step) {
  if (res is List) return res;
  if (res is! Map) throw BrokerError('$step: unexpected response ${_cut('$res')}');
  if (_isEmpty(res)) return {'data': []};
  var err = res['Error'] ?? res['error'] ?? res['Error Message'];
  if (err == null && '${res['stat'] ?? ''}'.toLowerCase() == 'not_ok') {
    err = res['errMsg'] ?? res['emsg'] ?? res['desc'] ?? res;
  }
  if (err != null) throw BrokerError('$step failed: ${_cut('$err', 300)}');
  return res;
}

String _cut(String s, [int n = 200]) => s.length > n ? s.substring(0, n) : s;

bool _authError(String msg) {
  final m = msg.toLowerCase();
  return ['unauthori', 'authenticat', '2fa', 'invalid session', 'session expired', 'token expired', 'invalid token']
      .any(m.contains);
}

class Kotak {
  Kotak(this.creds, {http.Client? client}) : _http = client ?? http.Client();
  final Credentials creds;
  final http.Client _http;
  _Session? _s;
  static const _store = FlutterSecureStorage();
  static const _sessionKey = 'kotak_session';

  static Future<Kotak> create() async => Kotak(await Credentials.load());

  String? get accountName => _s?.name;
  String get _base => _s?.baseUrl ?? prodBase;

  Future<dynamic> _send(String method, String url, Map<String, String> headers, {Object? json, Object? form}) async {
    final h = {...headers};
    http.Response res;
    final uri = Uri.parse(url);
    const timeout = Duration(seconds: 30);
    if (method == 'GET') {
      res = await _http.get(uri, headers: h).timeout(timeout);
    } else if (form != null) {
      h['Content-Type'] = 'application/x-www-form-urlencoded';
      res = await _http.post(uri, headers: h, body: {'jData': jsonEncode(form)}).timeout(timeout);
    } else {
      h['Content-Type'] = 'application/json';
      res = await _http.post(uri, headers: h, body: jsonEncode(json ?? {})).timeout(timeout);
    }
    try {
      return jsonDecode(res.body);
    } on FormatException {
      return {'Error': 'HTTP ${res.statusCode}: ${_cut(res.body)}'};
    }
  }

  /// A logged-in session for today, logging in if needed.
  Future<void> login({bool force = false}) async {
    final today = dstr(istNow());
    if (!force) {
      if (_s?.day == today) return;
      final saved = await _store.read(key: _sessionKey);
      final s = saved == null ? null : _Session.fromJson(jsonDecode(saved));
      if (s?.day == today) {
        _s = s;
        return;
      }
    }
    if (!creds.complete) throw BrokerError('Kotak login details are missing (Settings → Kotak account)');
    final view = _check(
        await _send('POST', '$sessionBase/login/1.0/tradeApiLogin',
            {'Authorization': creds.consumerKey, 'neo-fin-key': finKey},
            json: {'mobileNumber': creds.mobile, 'ucc': creds.ucc, 'totp': totp(creds.totpSecret)}),
        'TOTP login');
    final vd = (view['data'] as Map?) ?? {};
    final edit = _check(
        await _send('POST', '$sessionBase/login/1.0/tradeApiValidate', {
          'Authorization': creds.consumerKey, 'sid': '${vd['sid']}', 'Auth': '${vd['token']}', 'neo-fin-key': finKey,
        }, json: {'mpin': creds.mpin}),
        'MPIN validation');
    final d = (edit['data'] as Map?) ?? {};
    if (d['token'] == null || d['sid'] == null) throw BrokerError('MPIN validation: no session in the reply');
    _s = _Session('${d['token']}', '${d['sid']}', (d['baseUrl'] as String?) ?? prodBase, today,
        (vd['greetingName'] ?? d['greetingName']) as String?);
    await _store.write(key: _sessionKey, value: jsonEncode(_s!.toJson()));
  }

  static Future<void> forgetSession() => _store.delete(key: _sessionKey);

  Map<String, String> get _tradeHeaders =>
      {'Authorization': creds.consumerKey, 'Sid': _s!.sid, 'Auth': _s!.token, 'accept': 'application/json'};

  static const _orderMethods = {'place_order', 'cancel_order'};

  /// A logged-in call; on failure log in again once and retry. Orders are retried only after an authentication
  /// refusal (which happens before anything executes): after a timeout the order may have gone through.
  Future<dynamic> _call(String name, Future<dynamic> Function() f) async {
    for (final attempt in [1, 2]) {
      await login(force: attempt == 2);
      try {
        return _check(await f(), name);
      } on BrokerError catch (e) {
        final auth = _authError(e.message);
        if (attempt == 2 || (_orderMethods.contains(name) && !auth)) {
          if (_orderMethods.contains(name) && auth) {
            final ip = await clientIp();
            throw BrokerError('${e.message}. Kotak accepted the login but refused order access. Usually this '
                "means this phone's internet IP${ip == null ? '' : ' ($ip)'} isn't registered for Kotak Trade API "
                'orders, or the F&O segment isn\'t active. Register the IP / activate the segment, then try again.');
          }
          rethrow;
        }
      }
    }
    throw StateError('unreachable');
  }

  // ---- market data (consumer key only) ----

  /// Raw quotes for (segment, token-or-index-name) pairs, 49 a call.
  Future<List<Map<String, dynamic>>> quotes(List<(String, String)> items) async {
    final out = <Map<String, dynamic>>[];
    for (var n = 0; n < items.length; n += quoteBatch) {
      if (n > 0) await Future.delayed(const Duration(milliseconds: 1100));
      final batch = items.sublist(n, (n + quoteBatch).clamp(0, items.length));
      final sym = batch.map((e) => '${e.$1}|${Uri.encodeComponent(e.$2)}').join(',');
      final res = await _send('GET', '$_base/script-details/1.0/quotes/neosymbol/$sym/all',
          {'Authorization': creds.consumerKey});
      if (res is! List) throw BrokerError('Kotak quotes failed: ${_cut('$res')}');
      out.addAll(res.whereType<Map>().map((e) => Map<String, dynamic>.from(e)));
    }
    return out;
  }

  /// The scrip master CSV's URL for a segment (nse_fo, bse_fo...).
  Future<String> scripMasterUrl(String segment) async {
    final res = await _send('GET', '$_base/script-details/1.0/masterscrip/file-paths',
        {'Authorization': creds.consumerKey});
    final data = res is Map ? res['data'] : null;
    final files = List<String>.from((data is Map ? data['filesPaths'] : null) ?? const []);
    final hit = files.where((f) => f.toLowerCase().contains(segment.toLowerCase()));
    if (hit.isEmpty) throw BrokerError('Kotak scrip master: no file for $segment (${_cut('$res')})');
    return hit.first;
  }

  Future<String> download(String url) async {
    final res = await _http.get(Uri.parse(url)).timeout(const Duration(seconds: 120));
    if (res.statusCode != 200) throw BrokerError('Download failed: HTTP ${res.statusCode}');
    return utf8.decode(res.bodyBytes, allowMalformed: true);
  }

  // ---- account and orders (logged in) ----

  Future<String> placeOrder({required String segment, required String tsym, required String side,
      required int qty, required double price, String product = 'NRML'}) async {
    final res = await _call('place_order', () async {
      await login();
      return _send('POST', '$_base/quick/order/rule/ms/place',
          {'Authorization': creds.consumerKey, 'Sid': _s!.sid, 'Auth': _s!.token}, form: {
        'am': 'NO', 'dq': '0', 'es': segment, 'mp': '0', 'pc': product, 'pr': price.toStringAsFixed(2),
        'pt': 'L', 'qt': '$qty', 'rt': 'DAY', 'tp': '0', 'ts': tsym, 'tt': side, 'os': orderSource,
      });
    });
    final data = res is Map && res['data'] is Map ? res['data'] as Map : const {};
    final oid = (res is Map ? res['nOrdNo'] : null) ?? data['nOrdNo'];
    if (oid == null) throw BrokerError('place_order: no order number in ${_cut('$res')}');
    return '$oid';
  }

  Future<void> cancelOrder(String orderId) async {
    try {
      await _call('cancel_order', () => _send('POST', '$_base/quick/order/cancel',
          {'Authorization': creds.consumerKey, 'Sid': _s!.sid, 'Auth': _s!.token}, form: {'on': orderId, 'am': 'NO'}));
    } on BrokerError {
      // already filled / cancelled: the order book says what happened
    }
  }

  /// Today's order book by order number.
  Future<Map<String, Map<String, dynamic>>> orderBook() async {
    final res = await _call('order_report', () => _send('GET', '$_base/quick/user/orders', _tradeHeaders));
    final rows = (res is Map ? res['data'] : null) as List? ?? const [];
    return {
      for (final o in rows.whereType<Map>())
        if (o['nOrdNo'] != null) '${o['nOrdNo']}': Map<String, dynamic>.from(o)
    };
  }

  Future<List<Map<String, dynamic>>> positions() async {
    final res = await _call('positions', () => _send('GET', '$_base/portfolio/v2/positions', _tradeHeaders));
    final rows = (res is Map ? res['data'] : null) as List? ?? const [];
    return [for (final r in rows.whereType<Map>()) Map<String, dynamic>.from(r)];
  }

  Future<Map<String, dynamic>> limits() async =>
      Map<String, dynamic>.from(await _call('limits', () => _send('GET', '$_base/quick/user/limits', _tradeHeaders)));

  /// This phone's internet IP as Kotak sees it: the one that must be registered for API orders.
  Future<String?> clientIp() async {
    try {
      await login();
      final res = await _send('GET', '$sessionBase/login/1.0/get-client-ip', _tradeHeaders);
      final data = (res is Map ? res['data'] : null) as List?;
      return data != null && data.isNotEmpty ? data.first['ip'] as String? : null;
    } catch (_) {
      return null;
    }
  }
}

// ---- quote parsing (kotak.py parse_quote, fno.py parse_option_quote) ----

double? f(Object? v) {
  if (v == null || v == '') return null;
  if (v is num) return v.toDouble();
  return double.tryParse('$v');
}

/// Last update as naive IST (epoch seconds; a text date for a contract that never traded).
DateTime? quoteTime(Map q) {
  final s = int.tryParse('${q['lstup_time'] ?? 0}') ?? 0;
  return s > 0 ? istFromEpochSec(s) : null;
}

/// {ltp, open, high, low, time} of an index or stock quote.
Map<String, dynamic>? parseQuote(Map q) {
  final ltp = f(q['ltp']);
  if (ltp == null) return null;
  final ohlc = (q['ohlc'] as Map?) ?? const {};
  return {
    'ltp': ltp, 'open': f(ohlc['open']) ?? ltp, 'high': f(ohlc['high']) ?? ltp, 'low': f(ohlc['low']) ?? ltp,
    'time': quoteTime(q),
  };
}

/// {ltp, bid, ask} of an option.
Map<String, double?> parseOptionQuote(Map q) {
  final depth = (q['depth'] as Map?) ?? const {};
  final bid = ((depth['buy'] as List?)?.firstOrNull as Map?) ?? const {};
  final ask = ((depth['sell'] as List?)?.firstOrNull as Map?) ?? const {};
  final b = f(bid['price']), a = f(ask['price']);
  return {'ltp': f(q['ltp']), 'bid': (b ?? 0) > 0 ? b : null, 'ask': (a ?? 0) > 0 ? a : null};
}

// ---- order book / positions (orders.py, portfolio.py) ----

/// FILLED / REJECTED / CANCELLED / OPEN, or GONE when the order isn't in today's book.
String orderState(Map? o) {
  if (o == null) return 'GONE';
  final s = '${o['ordSt'] ?? o['status'] ?? ''}'.toLowerCase();
  if (['complete', 'completed', 'traded', 'filled'].contains(s)) return 'FILLED';
  if (s.contains('reject')) return 'REJECTED';
  if (s.contains('cancel') || s.contains('expire') || s.contains('lapse')) return 'CANCELLED';
  return 'OPEN';
}

(int, double?) filled(Map? o) {
  if (o == null) return (0, null);
  final q = (f(o['fldQty']) ?? 0).toInt();
  final avg = f(o['avgPrc']);
  return (q, (avg ?? 0) > 0 ? avg : null);
}

String reason(Map? o) => _cut('${o?['rejRsn'] ?? o?['rejReason'] ?? o?['ordSt'] ?? 'unknown'}');

double _num(Map r, List<String> keys) {
  for (final k in keys) {
    final v = f(r[k]);
    if (v != null) return v;
  }
  return 0;
}

/// (trading symbol, net quantity) of a position row.
(String, int) positionOf(Map row) {
  var buy = _num(row, ['flBuyQty']) + _num(row, ['cfBuyQty']);
  var sell = _num(row, ['flSellQty']) + _num(row, ['cfSellQty']);
  if (buy == 0 && sell == 0) {
    buy = _num(row, ['buyQty']);
    sell = _num(row, ['sellQty']);
  }
  final net = f(row['netQty']) ?? (buy - sell);
  return ('${row['trdSym'] ?? row['tradingSymbol'] ?? ''}'.trim().toUpperCase(), net.toInt());
}
