package com.algotrade.dashboard;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowInsets;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.JsPromptResult;
import android.webkit.JsResult;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import android.window.OnBackInvokedDispatcher;

/**
 * AlgoTrade on the phone: the dashboard running on your server, full screen, in a WebView.
 *
 * The server does all the work (login with DASHBOARD_PIN, Cloudflare Access, who may trade); this app
 * only remembers the server address, keeps the login cookies, shows the dashboard's confirm()/alert()
 * dialogs (every real order is confirmed through them) and saves downloads (the tax Excel).
 */
public class MainActivity extends Activity {

    static final String ACTION_CHANGE_SERVER = "com.algotrade.dashboard.CHANGE_SERVER";
    private static final String PREFS = "algotrade";
    private static final String KEY_SERVER = "server_url";

    private WebView web;
    private ProgressBar progress;
    private View errorPanel;
    private TextView errorText;
    private View setupPanel;
    private EditText serverUrl;
    private TextView setupMessage;
    private Button connect;
    private Button cancelSetup;
    private boolean httpWarned;
    private boolean mainFrameFailed;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.web);
        progress = findViewById(R.id.progress);
        errorPanel = findViewById(R.id.errorPanel);
        errorText = findViewById(R.id.errorText);
        setupPanel = findViewById(R.id.setupPanel);
        serverUrl = findViewById(R.id.serverUrl);
        setupMessage = findViewById(R.id.setupMessage);
        connect = findViewById(R.id.connect);
        cancelSetup = findViewById(R.id.cancelSetup);

        applySystemBarInsets(findViewById(R.id.root));
        setUpWebView();
        setUpPanels();
        setUpBack();

        String server = savedServer();
        if (server == null || ACTION_CHANGE_SERVER.equals(getIntent().getAction())) {
            showSetup();
        } else if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(server);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (ACTION_CHANGE_SERVER.equals(intent.getAction())) showSetup();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onPause() {
        web.onPause();
        CookieManager.getInstance().flush();   // keep the login across app restarts
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }

    // ---- WebView ---------------------------------------------------------------------------

    private void setUpWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setTextZoom(100);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setUserAgentString(s.getUserAgentString() + " AlgoTradeApp/" + BuildConfigCompat.versionName(this));

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        // Cloudflare Access sets its cookie from the team domain during login.
        cookies.setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleNavigation(request.getUrl(), request.hasGesture());
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                mainFrameFailed = false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!mainFrameFailed) errorPanel.setVisibility(View.GONE);
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (!request.isForMainFrame()) return;
                mainFrameFailed = true;
                showError(String.valueOf(error.getDescription()));
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int p) {
                progress.setProgress(p);
                progress.setVisibility(p < 100 ? View.VISIBLE : View.GONE);
            }

            // The dashboard confirms every real order with confirm(), so these must be real dialogs.
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                if (isFinishing()) { result.cancel(); return true; }
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                if (isFinishing()) { result.cancel(); return true; }
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton(android.R.string.ok, (d, w) -> result.confirm())
                        .setNegativeButton(android.R.string.cancel, (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsPrompt(WebView view, String url, String message, String defaultValue,
                                      JsPromptResult result) {
                if (isFinishing()) { result.cancel(); return true; }
                EditText input = new EditText(MainActivity.this);
                input.setSingleLine(true);
                if (defaultValue != null) input.setText(defaultValue);
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setView(input)
                        .setPositiveButton(android.R.string.ok,
                                (d, w) -> result.confirm(input.getText().toString()))
                        .setNegativeButton(android.R.string.cancel, (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }
        });

        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, length) ->
                download(url, userAgent, contentDisposition, mimeType));
    }

    /** True = handled here (not loaded in the WebView). */
    private boolean handleNavigation(Uri uri, boolean userTapped) {
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        if (!scheme.equals("http") && !scheme.equals("https")) {
            openExternally(uri);              // tel:, mailto:, intent: ...
            return true;
        }
        if (isOwnHost(uri)) return false;
        // Links the user taps that leave the dashboard (TradingView charts) open in the browser.
        // Redirects (the Cloudflare Access login) stay in the app so its cookie lands here.
        if (userTapped) {
            openExternally(uri);
            return true;
        }
        return false;
    }

    private boolean isOwnHost(Uri uri) {
        String host = uri.getHost();
        if (host == null) return false;
        host = host.toLowerCase();
        String server = savedServer();
        String serverHost = server == null ? null : Uri.parse(server).getHost();
        return (serverHost != null && host.equals(serverHost.toLowerCase()))
                || host.endsWith(".cloudflareaccess.com");
    }

    private void openExternally(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_app_for_link, Toast.LENGTH_SHORT).show();
        }
    }

    /** Saves a file the dashboard offers (the tax Excel) to Downloads, with the login cookies. */
    private void download(String url, String userAgent, String contentDisposition, String mimeType) {
        try {
            String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
            if (url.contains("/api/tax.xlsx") && !name.endsWith(".xlsx")) {
                Uri u = Uri.parse(url);
                String fy = u.getQueryParameter("fy");
                name = "AlgoTrade-tax" + (fy == null ? "" : "-" + fy) + ".xlsx";
            }
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) req.addRequestHeader("Cookie", cookie);
            req.addRequestHeader("User-Agent", userAgent);
            if (mimeType != null) req.setMimeType(mimeType);
            req.setTitle(name);
            req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            dm.enqueue(req);
            Toast.makeText(this, getString(R.string.download_started, name), Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, R.string.download_failed, Toast.LENGTH_LONG).show();
        }
    }

    // ---- Panels ----------------------------------------------------------------------------

    private void setUpPanels() {
        findViewById(R.id.retry).setOnClickListener(v -> {
            errorPanel.setVisibility(View.GONE);
            String current = web.getUrl();
            if (current == null || current.startsWith("about:")) web.loadUrl(savedServer());
            else web.reload();
        });
        findViewById(R.id.changeServer).setOnClickListener(v -> showSetup());

        connect.setOnClickListener(v -> submitServer());
        cancelSetup.setOnClickListener(v -> hideSetup());
        serverUrl.setOnEditorActionListener((v, actionId, event) -> {
            boolean enter = event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                    && event.getAction() == KeyEvent.ACTION_DOWN;
            if (actionId == EditorInfo.IME_ACTION_GO || enter) { submitServer(); return true; }
            return false;
        });
        serverUrl.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable s) {
                httpWarned = false;
                connect.setText(R.string.connect);
                setupMessage.setVisibility(View.GONE);
            }
        });
    }

    private void showSetup() {
        String server = savedServer();
        serverUrl.setText(server == null ? "" : server);
        serverUrl.setSelection(serverUrl.getText().length());
        cancelSetup.setVisibility(server == null ? View.GONE : View.VISIBLE);
        errorPanel.setVisibility(View.GONE);
        setupPanel.setVisibility(View.VISIBLE);
        serverUrl.requestFocus();
    }

    private void hideSetup() {
        setupPanel.setVisibility(View.GONE);
        hideKeyboard();
        if (web.getUrl() == null && savedServer() != null) web.loadUrl(savedServer());
    }

    private void submitServer() {
        String url = normalize(serverUrl.getText().toString());
        if (url == null) {
            setupMessage.setText(R.string.bad_url);
            setupMessage.setVisibility(View.VISIBLE);
            return;
        }
        if (url.startsWith("http://") && !httpWarned) {
            httpWarned = true;
            setupMessage.setText(R.string.setup_http_warning);
            setupMessage.setVisibility(View.VISIBLE);
            connect.setText("Connect anyway");
            return;
        }
        prefs().edit().putString(KEY_SERVER, url).apply();
        setupPanel.setVisibility(View.GONE);
        hideKeyboard();
        web.clearHistory();
        web.loadUrl(url);
    }

    /** "algo.example.com" -> "https://algo.example.com/"; null when it isn't a web address. */
    static String normalize(String raw) {
        String s = raw == null ? "" : raw.trim();
        if (s.isEmpty()) return null;
        if (!s.matches("(?i)^https?://.*")) s = "https://" + s;
        Uri u = Uri.parse(s);
        String host = u.getHost();
        if (host == null || host.isEmpty() || host.contains(" ")) return null;
        String scheme = u.getScheme().toLowerCase();
        String port = u.getPort() > 0 ? ":" + u.getPort() : "";
        String path = u.getPath() == null || u.getPath().isEmpty() ? "/" : u.getPath();
        if (!path.endsWith("/")) path += "/";
        return scheme + "://" + host.toLowerCase() + port + path;
    }

    private void showError(String detail) {
        String server = savedServer();
        errorText.setText((server == null ? "" : server + "\n\n") + detail
                + "\n\nCheck that the server is running and your phone is online.");
        errorPanel.setVisibility(View.VISIBLE);
    }

    private void hideKeyboard() {
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null) imm.hideSoftInputFromWindow(serverUrl.getWindowToken(), 0);
    }

    // ---- Back button, system bars ----------------------------------------------------------

    private void setUpBack() {
        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::goBack);
        }
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        goBack();
    }

    private void goBack() {
        if (setupPanel.getVisibility() == View.VISIBLE && savedServer() != null) {
            hideSetup();
        } else if (setupPanel.getVisibility() != View.VISIBLE && web.canGoBack()) {
            web.goBack();
        } else {
            moveTaskToBack(true);   // keep the page (and the live prices) as it was
        }
    }

    /** Android 15+ draws apps edge to edge; keep the dashboard clear of the status and nav bars. */
    private static void applySystemBarInsets(View root) {
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int l, t, r, b;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets i = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                                | WindowInsets.Type.ime());
                l = i.left; t = i.top; r = i.right; b = i.bottom;
            } else {
                l = insets.getSystemWindowInsetLeft();
                t = insets.getSystemWindowInsetTop();
                r = insets.getSystemWindowInsetRight();
                b = insets.getSystemWindowInsetBottom();
            }
            v.setPadding(l, t, r, b);
            return insets;
        });
    }

    // ---- Settings --------------------------------------------------------------------------

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private String savedServer() {
        return prefs().getString(KEY_SERVER, null);
    }

    /** versionName without needing BuildConfig generation. */
    static final class BuildConfigCompat {
        static String versionName(Context c) {
            try {
                return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName;
            } catch (Exception e) {
                return "1";
            }
        }
    }
}
