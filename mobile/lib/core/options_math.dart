/// Black-Scholes pricing, implied volatility and delta (algotrade/strategy.py), and the marketable-limit price
/// (algotrade/manual.py protect).
library;

import 'dart:math' as math;

const riskFree = 0.065; // annual, roughly the Indian T-bill yield
const minT = 0.5 / 365; // the last half-day before expiry is the floor for time value

double _erf(double x) {
  // Abramowitz-Stegun 7.1.26 is too coarse for delta ranking near 0.6-0.8; use a high-precision series/continued
  // fraction split instead.
  final sign = x < 0 ? -1.0 : 1.0;
  x = x.abs();
  if (x < 3) {
    // Taylor series
    var sum = x, term = x;
    for (var n = 1; n < 200; n++) {
      term *= -x * x / n;
      final add = term / (2 * n + 1);
      sum += add;
      if (add.abs() < 1e-17) break;
    }
    return sign * 2 / math.sqrt(math.pi) * sum;
  }
  // erfc continued fraction (Lentz)
  const tiny = 1e-300;
  var f = x, c = x, d = 0.0;
  for (var n = 1; n < 300; n++) {
    final an = n / 2.0;
    d = x + an * d;
    d = d.abs() < tiny ? tiny : d;
    c = x + an / c;
    c = c.abs() < tiny ? tiny : c;
    d = 1 / d;
    final delta = c * d;
    f *= delta;
    if ((delta - 1).abs() < 1e-16) break;
  }
  final erfc = math.exp(-x * x) / math.sqrt(math.pi) / f;
  return sign * (1 - erfc);
}

double ncdf(double x) => 0.5 * (1 + _erf(x / math.sqrt2));
double npdf(double x) => math.exp(-x * x / 2) / math.sqrt(2 * math.pi);

double bsPrice(String opt, double s, double k, double t, double iv, [double r = riskFree]) {
  if (t <= 0 || iv <= 0) return opt == 'CE' ? math.max(s - k, 0) : math.max(k - s, 0);
  final d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t));
  final d2 = d1 - iv * math.sqrt(t);
  if (opt == 'CE') return s * ncdf(d1) - k * math.exp(-r * t) * ncdf(d2);
  return k * math.exp(-r * t) * ncdf(-d2) - s * ncdf(-d1);
}

/// Volatility that reproduces the market price (bisection), or null outside the model range.
double? impliedVol(String opt, double price, double s, double k, double t, [double r = riskFree]) {
  if (price <= 0 || t <= 0) return null;
  var lo = 1e-4, hi = 5.0;
  if (price < bsPrice(opt, s, k, t, lo, r) - 1e-9 || price > bsPrice(opt, s, k, t, hi, r)) return null;
  for (var i = 0; i < 100; i++) {
    final mid = (lo + hi) / 2;
    if (bsPrice(opt, s, k, t, mid, r) > price) {
      hi = mid;
    } else {
      lo = mid;
    }
  }
  return (lo + hi) / 2;
}

double delta(String opt, double s, double k, double t, double iv, [double r = riskFree]) {
  t = math.max(t, minT);
  if (iv <= 0) {
    final itm = opt == 'CE' ? s > k : s < k;
    return (itm ? 1.0 : 0.0) * (opt == 'CE' ? 1 : -1);
  }
  final d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t));
  return opt == 'CE' ? ncdf(d1) : ncdf(d1) - 1;
}

/// Marketable limit for a "market" order: pct % above the reference for a buy, below it for a sell, on the tick
/// grid (rounded away from the reference, so it stays marketable).
double protect(double ref, String side, double pct, [double tick = 0.05]) {
  final n0 = ref * (side == 'BUY' ? 1 + pct / 100 : 1 - pct / 100) / tick;
  final n = side == 'BUY' ? (n0 - 1e-9).ceil() : (n0 + 1e-9).floor();
  return ((math.max(n, 1) * tick) * 100).roundToDouble() / 100;
}
