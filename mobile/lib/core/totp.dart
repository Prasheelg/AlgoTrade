/// RFC 6238 time-based one-time password (SHA-1, 6 digits), as an authenticator app shows it (broker.py totp).
library;

import 'dart:typed_data';

import 'package:crypto/crypto.dart';

const _b32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

Uint8List base32Decode(String secret) {
  final s = secret.replaceAll(' ', '').replaceAll('=', '').toUpperCase();
  final out = <int>[];
  var buffer = 0, bits = 0;
  for (final ch in s.split('')) {
    final v = _b32.indexOf(ch);
    if (v < 0) throw const FormatException('The TOTP secret is not valid base32');
    buffer = (buffer << 5) | v;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      out.add((buffer >> bits) & 0xFF);
    }
  }
  return Uint8List.fromList(out);
}

String totp(String secret, {DateTime? at, int step = 30, int digits = 6}) {
  final key = base32Decode(secret);
  final secs = (at ?? DateTime.now()).millisecondsSinceEpoch ~/ 1000;
  final counter = secs ~/ step;
  final msg = ByteData(8)..setUint64(0, counter);
  final mac = Hmac(sha1, key).convert(msg.buffer.asUint8List()).bytes;
  final offset = mac.last & 0x0F;
  final code = ((mac[offset] & 0x7F) << 24) | (mac[offset + 1] << 16) | (mac[offset + 2] << 8) | mac[offset + 3];
  var s = (code % _pow10(digits)).toString();
  while (s.length < digits) {
    s = '0$s';
  }
  return s;
}

int _pow10(int n) {
  var x = 1;
  for (var i = 0; i < n; i++) {
    x *= 10;
  }
  return x;
}
