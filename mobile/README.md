# NR7 Trader (Android)

The NR7 index breakout (Nifty 50, Bank Nifty, Sensex) running entirely on an Android phone: no PC, no server.
The scan, the setup tracker and auto-trade run on the phone; setups, orders, settings and the log are stored on the
phone (SQLite), and the Kotak login details sit in the Android Keystore.

It is a Flutter port of `algotrade/nr7.py`, `nr7_auto.py` and `nr7_instruments.py` with the same rules. The Dart walk is
checked trade for trade against the Python app on three years of real candles (`test/parity_test.dart`).

## Install
1. `flutter build apk --release` → `build/app/outputs/flutter-apk/app-release.apk`
2. Copy it to the phone and open it (allow "install unknown apps" for your file manager once).

## First run
1. **Settings → Kotak account**: consumer key, mobile (+91…), UCC, MPIN, TOTP secret → **Save and log in**.
   It shows the account name and the IP Kotak sees for the phone.
2. **Setups → Background watcher** on. Allow notifications, and allow the app to ignore battery optimisation
   (otherwise Android may stop it with the screen off).
3. Real orders: switch on **NR7 Trade** and **Auto-trade** in Settings (Auto-trade first proves the login works).

## What runs when
- Every 15 s while the watcher runs (it restarts after a reboot):
  - the evening scan, once a day after 16:00 IST, with a morning catch-up if the phone missed it;
  - the minute-bar tracker (Yahoo, every 30 s in market hours) for entries, stops, targets, the day-5 exit and expiries;
  - auto-trade (Kotak live quotes, every 15 s in market hours).
- Every event shows as a notification and lands in the **Log** tab.

## Orders and the registered IP
Kotak accepts API orders only from a registered static IP. On home Wi-Fi the phone uses the router's public IP (the
same one the PC uses); on mobile data the IP changes and Kotak will refuse orders. Use **Save and log in** to see the
IP Kotak sees. A refused order shows in Log and in a notification.

## Tests
- `flutter test`: parity with the Python app plus the engine (fake Kotak, in-memory DB).
- `NR7_LIVE=1 KOTAK_CONSUMER_KEY=... flutter test test/live_test.dart`: read-only Yahoo and Kotak checks.
- Regenerate `test/fixtures/parity.json` from the Python side when the NR7 rules change.
