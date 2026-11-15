/// India time. Every timestamp in the app is a "naive IST" DateTime: a UTC-flagged DateTime whose fields are the
/// Indian wall clock (India has no daylight saving, so IST is always UTC+5:30). This mirrors the Python app's naive
/// IST timestamps, and makes the stored strings ("2026-10-20T09:15:00") identical.
library;

const istOffset = Duration(hours: 5, minutes: 30);

/// Overridable clock, for tests.
DateTime Function() clock = () => DateTime.now().toUtc();

DateTime istNow() => clock().add(istOffset);

DateTime istFromEpochSec(num sec) =>
    DateTime.fromMillisecondsSinceEpoch((sec * 1000).round(), isUtc: true).add(istOffset);

/// "YYYY-MM-DD"
String dstr(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-${_two(d.month)}-${_two(d.day)}';

/// "YYYY-MM-DDTHH:MM:SS" (Python's isoformat(timespec="seconds") of a naive datetime)
String tsstr(DateTime d) => '${dstr(d)}T${_two(d.hour)}:${_two(d.minute)}:${_two(d.second)}';

String hhmm(DateTime d) => '${_two(d.hour)}:${_two(d.minute)}';

String _two(int n) => n.toString().padLeft(2, '0');

/// Parses "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM[:SS]" as naive IST.
DateTime parseIst(String s) {
  final d = DateTime.parse(s.length == 10 ? '${s}T00:00:00Z' : (s.endsWith('Z') ? s : '${s}Z'));
  return d;
}

DateTime dateOnly(DateTime d) => DateTime.utc(d.year, d.month, d.day);

/// Minutes since midnight.
int mins(DateTime d) => d.hour * 60 + d.minute;

int hm(int h, int m) => h * 60 + m;

bool isWeekday(DateTime d) => d.weekday <= DateTime.friday;

/// The next weekday after d (pandas BDay(1)).
DateTime nextBDay(DateTime d) {
  var x = dateOnly(d).add(const Duration(days: 1));
  while (!isWeekday(x)) {
    x = x.add(const Duration(days: 1));
  }
  return x;
}

/// The weekday before d (pandas BDay(-1) from a date).
DateTime prevBDay(DateTime d) {
  var x = dateOnly(d).subtract(const Duration(days: 1));
  while (!isWeekday(x)) {
    x = x.subtract(const Duration(days: 1));
  }
  return x;
}

/// Weekdays from a up to, not including, b (numpy busday_count).
int busdayCount(DateTime a, DateTime b) {
  var x = dateOnly(a), end = dateOnly(b);
  var sign = 1;
  if (end.isBefore(x)) {
    final t = x;
    x = end;
    end = t;
    sign = -1;
  }
  var n = 0;
  while (x.isBefore(end)) {
    if (isWeekday(x)) n++;
    x = x.add(const Duration(days: 1));
  }
  return sign * n;
}

/// NSE cash session (no exchange holiday list: on a holiday the index simply doesn't move, and the NR7 rules count
/// sessions from the bars Yahoo actually has).
const sessionOpen = 9 * 60 + 15;
const sessionClose = 15 * 60 + 30;

bool marketOpen([DateTime? now]) {
  final n = now ?? istNow();
  return isWeekday(n) && mins(n) >= sessionOpen && mins(n) <= sessionClose;
}
