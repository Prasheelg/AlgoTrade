import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';

import 'engine/notify.dart';
import 'engine/runner.dart';
import 'ui/history_page.dart';
import 'ui/home_page.dart';
import 'ui/log_page.dart';
import 'ui/settings_page.dart';
import 'ui/trades_page.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  FlutterForegroundTask.initCommunicationPort();
  runApp(const NR7App());
}

class NR7App extends StatelessWidget {
  const NR7App({super.key});

  @override
  Widget build(BuildContext context) {
    const seed = Color(0xFF1E6B52);
    return MaterialApp(
      title: 'NR7 Trader',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: seed, useMaterial3: true),
      darkTheme: ThemeData(colorSchemeSeed: seed, useMaterial3: true, brightness: Brightness.dark),
      home: const Shell(),
    );
  }
}

/// Lets any screen ask for a refresh after the watcher reports something.
final refreshBus = StreamController<Map>.broadcast();

class Shell extends StatefulWidget {
  const Shell({super.key});

  @override
  State<Shell> createState() => _ShellState();
}

class _ShellState extends State<Shell> {
  int _tab = 0;

  void _onTaskData(Object data) {
    if (data is Map) {
      refreshBus.add(data);
      if (data['event'] == 'error' && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${data['cmd']}: ${data['error']}')));
      }
    }
  }

  @override
  void initState() {
    super.initState();
    FlutterForegroundTask.addTaskDataCallback(_onTaskData);
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      initService();
      await initNotifications();
    });
  }

  @override
  void dispose() {
    FlutterForegroundTask.removeTaskDataCallback(_onTaskData);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    const pages = [HomePage(), TradesPage(), HistoryPage(), LogPage(), SettingsPage()];
    return WithForegroundTask(
      child: Scaffold(
        body: SafeArea(child: IndexedStack(index: _tab, children: pages)),
        bottomNavigationBar: NavigationBar(
          selectedIndex: _tab,
          onDestinationSelected: (i) => setState(() => _tab = i),
          destinations: const [
            NavigationDestination(icon: Icon(Icons.radar), label: 'Setups'),
            NavigationDestination(icon: Icon(Icons.receipt_long), label: 'Orders'),
            NavigationDestination(icon: Icon(Icons.history), label: 'History'),
            NavigationDestination(icon: Icon(Icons.notes), label: 'Log'),
            NavigationDestination(icon: Icon(Icons.settings), label: 'Settings'),
          ],
        ),
      ),
    );
  }
}
