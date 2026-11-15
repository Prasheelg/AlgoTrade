import 'package:flutter/material.dart';

import '../core/nr7.dart' as nr7;
import '../data/db.dart';
import '../data/settings.dart';
import '../net/kotak.dart';
import 'common.dart';

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  Settings? _s;
  final _ck = TextEditingController(), _mobile = TextEditingController(), _ucc = TextEditingController(),
      _mpin = TextEditingController(), _totp = TextEditingController();
  final _capital = TextEditingController(), _risk = TextEditingController(), _lots = TextEditingController(),
      _minDays = TextEditingController(), _prot = TextEditingController(), _spread = TextEditingController(),
      _skip = TextEditingController();
  String? _account, _ip, _loginError;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final s = await Settings.load();
    final c = await Credentials.load();
    _ck.text = c.consumerKey;
    _mobile.text = c.mobile;
    _ucc.text = c.ucc;
    _mpin.text = c.mpin;
    _totp.text = c.totpSecret;
    _capital.text = s.capital.toStringAsFixed(0);
    _risk.text = '${s.riskPct}';
    _lots.text = '${s.autoLots}';
    _minDays.text = '${s.optionMinDays}';
    _prot.text = '${s.optionProtectionPct}';
    _spread.text = '${s.maxSpreadPct}';
    _skip.text = s.skipDays.join(' ');
    final acct = await (await Db.open()).get<String>('kotak_account');
    if (mounted) setState(() => (_s = s, _account = acct));
  }

  Future<void> _save({bool quiet = false}) async {
    final s = _s!;
    try {
      s.capital = double.parse(_capital.text);
      s.riskPct = double.parse(_risk.text);
      s.autoLots = int.parse(_lots.text).clamp(1, 50);
      s.optionMinDays = int.parse(_minDays.text);
      s.optionProtectionPct = double.parse(_prot.text);
      s.maxSpreadPct = double.parse(_spread.text);
      final days = _skip.text.replaceAll(',', ' ').split(RegExp(r'\s+')).where((d) => d.isNotEmpty).toList();
      for (final d in days) {
        if (!RegExp(r'^\d{4}-\d{2}-\d{2}$').hasMatch(d)) throw FormatException('Skip days: dates as YYYY-MM-DD');
        DateTime.parse(d);
      }
      s.skipDays = (days.toSet().toList()..sort());
    } on FormatException catch (e) {
      if (mounted) toast(context, e.message.isEmpty ? 'Check the numbers' : e.message);
      return;
    }
    await s.save();
    if (!quiet && mounted) toast(context, 'Saved');
    setState(() {});
  }

  Future<void> _saveCreds() async {
    await Credentials(_ck.text, _mobile.text, _ucc.text, _mpin.text, _totp.text).save();
    await Kotak.forgetSession();
  }

  /// Logs in to Kotak; true if it worked.
  Future<bool> _testLogin() async {
    setState(() => (_busy = true, _loginError = null));
    try {
      await _saveCreds();
      final k = await Kotak.create();
      await k.login(force: true);
      final ip = await k.clientIp();
      final acct = '${k.accountName ?? ''} (${k.creds.ucc})'.trim();
      await (await Db.open()).set('kotak_account', acct);
      setState(() => (_account = acct, _ip = ip));
      return true;
    } catch (e) {
      setState(() => _loginError = '$e');
      return false;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _setAutoTrade(bool on) async {
    final s = _s!;
    if (!on) {
      s.autoTrade = false;
      await s.save();
      await (await Db.open()).log('info', 'Auto-trade switched OFF');
      setState(() {});
      return;
    }
    if (!await _testLogin()) {
      if (mounted) toast(context, 'Kotak login failed, so auto-trade stays off');
      return;
    }
    if (!mounted) return;
    final go = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Turn on real orders?'),
        content: Text('The phone will place real Kotak orders on $_account: ${s.autoLots} lot of an in-the-money '
            'option on ${s.autoIndices.join(', ')} when an NR7 level breaks, and sell it at the stop, the target or '
            '15:15 on day ${nr7.holdDays}.\n\nKeep the background watcher running and the phone online all market '
            'day.${_ip == null ? '' : '\n\nKotak sees this phone at IP $_ip; API orders are accepted only from an IP '
                'registered with Kotak.'}'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Turn on')),
        ],
      ),
    );
    if (go != true) return;
    s.autoTrade = true;
    await s.save();
    await (await Db.open()).log('info', 'Auto-trade switched ON for $_account');
    setState(() {});
  }

  Widget _field(TextEditingController c, String label, {bool secret = false, bool number = false, String? help}) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: TextField(
          controller: c,
          obscureText: secret,
          keyboardType: number ? const TextInputType.numberWithOptions(decimal: true) : null,
          autocorrect: false,
          enableSuggestions: !secret,
          decoration: InputDecoration(labelText: label, helperText: help, border: const OutlineInputBorder(),
              isDense: true),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final s = _s;
    if (s == null) return const Center(child: CircularProgressIndicator());
    final head = Theme.of(context).textTheme.titleMedium;
    return ListView(padding: const EdgeInsets.all(16), children: [
      Text('Settings', style: Theme.of(context).textTheme.headlineSmall),
      const SizedBox(height: 16),
      Text('Switches', style: head),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('Watch NR7'),
        subtitle: const Text('Evening scan, setup and trade alerts'),
        value: s.on,
        onChanged: (v) async {
          s.on = v;
          await s.save();
          setState(() {});
        },
      ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('NR7 Trade'),
        subtitle: const Text('Let auto-trade act on NR7 setups'),
        value: s.trade,
        onChanged: (v) async {
          s.trade = v;
          await s.save();
          setState(() {});
        },
      ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('Auto-trade (real orders)'),
        subtitle: Text(s.autoTrade ? 'ON for ${_account ?? 'your Kotak account'}' : 'Off: alerts only'),
        value: s.autoTrade,
        onChanged: _busy ? null : _setAutoTrade,
      ),
      const Divider(height: 32),
      Text('Kotak account', style: head),
      const SizedBox(height: 4),
      Text('Stored encrypted on this phone (Android Keystore). The app logs in by itself each day.',
          style: TextStyle(color: Theme.of(context).colorScheme.outline)),
      const SizedBox(height: 12),
      _field(_ck, 'Consumer key', secret: true),
      _field(_mobile, 'Mobile number', help: 'With country code, e.g. +919876543210'),
      _field(_ucc, 'Client code (UCC)'),
      _field(_mpin, 'MPIN', secret: true, number: true),
      _field(_totp, 'TOTP secret', secret: true, help: 'The base32 key behind the authenticator QR code'),
      Row(children: [
        Expanded(
          child: FilledButton(
            onPressed: _busy
                ? null
                : () async {
                    if (await _testLogin() && context.mounted) toast(context, 'Logged in as $_account');
                  },
            child: _busy ? const Text('Logging in…') : const Text('Save and log in'),
          ),
        ),
      ]),
      if (_account != null) Padding(padding: const EdgeInsets.only(top: 8), child: KV('Account', _account!)),
      if (_ip != null) KV("This phone's IP (as Kotak sees it)", _ip!),
      if (_loginError != null)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text(_loginError!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
        ),
      const Divider(height: 32),
      Text('Risk and orders', style: head),
      const SizedBox(height: 12),
      _field(_capital, 'Capital (₹)', number: true),
      _field(_risk, 'Risk per trade (%)', number: true, help: 'Sizes the futures lots shown on each setup'),
      _field(_lots, 'Auto-trade lots per trade', number: true),
      _field(_minDays, 'Option expiry at least (days)', number: true),
      _field(_prot, 'Limit price protection (%)', number: true, help: 'Buy at ask + this, sell at bid − this'),
      _field(_spread, 'Skip options with a spread over (%)', number: true),
      Text('Auto-trade indices', style: Theme.of(context).textTheme.labelLarge),
      Wrap(spacing: 8, children: [
        for (final idx in nr7.indices.keys)
          FilterChip(
            label: Text(nr7.indices[idx]!.name),
            selected: s.autoIndices.contains(idx),
            onSelected: (v) async {
              s.autoIndices = v ? [...s.autoIndices, idx] : [for (final x in s.autoIndices) if (x != idx) x];
              await s.save();
              setState(() {});
            },
          ),
      ]),
      const SizedBox(height: 12),
      _field(_skip, 'Skip days', help: 'No new trade on these sessions: YYYY-MM-DD, space separated'),
      FilledButton.tonal(onPressed: _save, child: const Text('Save')),
      const SizedBox(height: 24),
    ]);
  }
}
