package com.algotrade.dashboard;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Rings while the app is closed: polls the server's activity feed (/api/activity) and shows each new event as a
 * notification on its kind's channel, each channel with its own sound (res/raw/at_<kind>.wav, the same sounds the
 * dashboard plays while it is open). While the app is on screen it only keeps its place in the feed: the page
 * plays the sounds then.
 *
 * It runs as a foreground service (Android shows a quiet "watching" notification) and holds a partial wake lock,
 * so the polls keep going with the screen off. Turn it off in the dashboard: Settings > Sounds.
 */
public class ActivityService extends Service {

    private static final String TAG = "AlgoTradeActivity";
    static final String[] KINDS = {"signal", "entry", "placed", "filled", "profit", "loss", "closed", "cancelled",
            "problem", "alert", "info"};
    private static final String[] NAMES = {"New signal", "Signal entry reached", "Order placed", "Trade opened",
            "Closed in profit", "Closed at a loss", "Closed about even", "Cancelled", "Needs attention",
            "Price alert", "Other updates"};
    // channel ids carry a version: a channel's sound can't change once made, so a new sound needs a new id
    private static final String CH_PREFIX = "act1_";
    private static final String CH_SERVICE = "watching";
    private static final int ID_SERVICE = 1;
    private static final long POLL_MS = 15_000;
    private static final int MAX_PER_POLL = 4;    // after a long gap, ring for the newest few, not a backlog

    static final String PREFS = "algotrade";
    static final String KEY_ENABLED = "background_alerts";
    private static final String KEY_LAST = "activity_last";
    private static final String KEY_LAST_SERVER = "activity_server";

    /** MainActivity sets this while it is on screen. */
    static volatile boolean appVisible;

    private HandlerThread thread;
    private Handler handler;
    private PowerManager.WakeLock wake;
    private int nextId = 100;

    static boolean enabled(Context c) {
        return c.getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean(KEY_ENABLED, true);
    }

    /** Starts the service if background alerts are on and a server is set. Call while the app is on screen. */
    static void startIfEnabled(Context c) {
        if (!enabled(c) || c.getSharedPreferences(PREFS, MODE_PRIVATE).getString("server_url", null) == null) return;
        try {
            Intent i = new Intent(c, ActivityService.class);
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
        } catch (Exception e) {
            Log.w(TAG, "Couldn't start the background alerts", e);
        }
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, ActivityService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        makeChannels(this);
        Intent open = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        Notification n = new Notification.Builder(this, CH_SERVICE)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("AlgoTrade")
                .setContentText("Watching for signals and trades")
                .setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE))
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_SERVICE, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(ID_SERVICE, n);
        }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AlgoTrade:activity");
        wake.setReferenceCounted(false);
        wake.acquire();
        thread = new HandlerThread("activity-poll");
        thread.start();
        handler = new Handler(thread.getLooper());
        handler.post(this::loop);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (handler != null) handler.removeCallbacksAndMessages(null);
        if (thread != null) thread.quitSafely();
        if (wake != null && wake.isHeld()) wake.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void loop() {
        try {
            poll();
        } catch (Exception e) {
            Log.w(TAG, "Activity poll failed: " + e);
        }
        handler.postDelayed(this::loop, POLL_MS);
    }

    private void poll() throws Exception {
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        String server = p.getString("server_url", null);
        if (server == null) return;
        long last = server.equals(p.getString(KEY_LAST_SERVER, null)) ? p.getLong(KEY_LAST, -1) : -1;
        String url = server + "api/activity?after=" + last;
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10_000);
        c.setReadTimeout(10_000);
        c.setInstanceFollowRedirects(false);   // a redirect means the login expired: nothing to read
        String cookie = CookieManager.getInstance().getCookie(server);
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        c.setRequestProperty("User-Agent", "AlgoTradeApp/" + MainActivity.BuildConfigCompat.versionName(this));
        c.setRequestProperty("Accept", "application/json");
        try {
            if (c.getResponseCode() != 200) return;   // 401 when logged out: open the app and log in again
            JSONObject j = new JSONObject(read(c.getInputStream()));
            JSONArray evs = j.optJSONArray("events");
            if (last >= 0 && evs != null && !appVisible) {
                for (int i = Math.max(0, evs.length() - MAX_PER_POLL); i < evs.length(); i++) {
                    notifyEvent(evs.getJSONObject(i), server);
                }
            }
            p.edit().putLong(KEY_LAST, j.optLong("last", last)).putString(KEY_LAST_SERVER, server).apply();
        } finally {
            c.disconnect();
        }
    }

    private void notifyEvent(JSONObject e, String server) {
        String kind = e.optString("kind", "info");
        String path = e.optString("url", "/");
        Intent open = new Intent(this, MainActivity.class)
                .setAction(MainActivity.ACTION_OPEN_PATH)
                .putExtra(MainActivity.EXTRA_PATH, path)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int id = nextId++;
        Notification n = new Notification.Builder(this, CH_PREFIX + (indexOf(kind) >= 0 ? kind : "info"))
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle(e.optString("title", "AlgoTrade"))
                .setContentText(e.optString("body", ""))
                .setStyle(new Notification.BigTextStyle().bigText(e.optString("body", "")))
                .setAutoCancel(true)
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true)
                .setContentIntent(PendingIntent.getActivity(this, id, open,
                        PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT))
                .build();
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(id, n);
    }

    /** One channel per kind, each with its own sound; Android's settings can mute or change any of them. */
    static void makeChannels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) c.getSystemService(NOTIFICATION_SERVICE);
        NotificationChannel svc = new NotificationChannel(CH_SERVICE, "Background watching", NotificationManager.IMPORTANCE_MIN);
        svc.setDescription("Shown while the app checks for new signals and trades with the app closed");
        svc.setShowBadge(false);
        nm.createNotificationChannel(svc);
        AudioAttributes aa = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();
        for (int i = 0; i < KINDS.length; i++) {
            String kind = KINDS[i];
            int importance = kind.equals("info") || kind.equals("cancelled") ? NotificationManager.IMPORTANCE_DEFAULT
                    : NotificationManager.IMPORTANCE_HIGH;
            NotificationChannel ch = new NotificationChannel(CH_PREFIX + kind, NAMES[i], importance);
            int raw = c.getResources().getIdentifier("at_" + kind, "raw", c.getPackageName());
            ch.setSound(Uri.parse("android.resource://" + c.getPackageName() + "/" + raw), aa);
            ch.enableVibration(!kind.equals("info"));
            nm.createNotificationChannel(ch);
        }
    }

    private static int indexOf(String kind) {
        for (int i = 0; i < KINDS.length; i++) if (KINDS[i].equals(kind)) return i;
        return -1;
    }

    private static String read(InputStream in) throws Exception {
        try (InputStream s = in; ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            for (int n; (n = s.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }
}
