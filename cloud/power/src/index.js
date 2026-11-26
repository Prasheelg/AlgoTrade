// AlgoTrade power switch: a Cloudflare Worker in front of https://algo.trendywork.in, behind the same Cloudflare
// Access login as the dashboard.
//
// - Every request goes on to the server (the Cloudflare Tunnel) unchanged.
// - When the server is off (the tunnel is down), a page load gets a small page instead: the server's state with a
//   Start button. It refreshes by itself and opens the dashboard once it answers.
// - /_power is that page at any time, with Start and Stop.
//
// The Worker talks to EC2 with an access key (Worker secrets AWS_KEY_ID / AWS_SECRET) that can only start, stop
// and describe; start and stop are limited (in its IAM policy) to instances tagged Name = SERVER_NAME (wrangler.toml).
// The server is found by that tag, not a fixed id, so a replacement server needs no change here.
import { AwsClient } from "aws4fetch";

const DOWN = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530]);   // 530 = tunnel down (error 1033)
const STATES = ["pending", "running", "stopping", "stopped", "shutting-down", "terminated", "none"];

async function call(env, action, params) {
  const aws = new AwsClient({ accessKeyId: env.AWS_KEY_ID, secretAccessKey: env.AWS_SECRET, region: env.REGION, service: "ec2" });
  const body = new URLSearchParams({ Action: action, Version: "2016-11-15", ...params });
  const res = await aws.fetch(`https://ec2.${env.REGION}.amazonaws.com/`, { method: "POST", body });
  const text = await res.text();
  if (!res.ok) throw new Error(`${action}: ${(/<Message>([^<]*)/.exec(text) || [, res.status])[1]}`);
  return text;
}

// The server: the one instance tagged Name = SERVER_NAME that isn't deleted. Describe answers with it (or "none");
// Start / Stop act on its id
async function ec2(env, action) {
  const xml = await call(env, "DescribeInstances", {
    "Filter.1.Name": "tag:Name", "Filter.1.Value.1": env.SERVER_NAME,
    "Filter.2.Name": "instance-state-name", ...Object.fromEntries(
      ["pending", "running", "stopping", "stopped"].map((s, i) => [`Filter.2.Value.${i + 1}`, s])) });
  const ids = [...new Set([...xml.matchAll(/<instanceId>(i-[0-9a-f]+)<\/instanceId>/g)].map(m => m[1]))];
  if (ids.length > 1) throw new Error(`There are ${ids.length} ${env.SERVER_NAME} servers; keep one`);
  if (action === "DescribeInstances") return ids.length ? xml : "<instanceState><code>0</code><name>none</name>";
  if (!ids.length) throw new Error(`No ${env.SERVER_NAME} server: launch one with "Start AlgoTrade.bat"`);
  return call(env, action, { "InstanceId.1": ids[0] });
}

// The instance's state as EC2 reports it (Describe: its own state; Start / Stop: the state it is now moving to)
const stateOf = xml => {
  const m = /<(?:instanceState|currentState)>\s*<code>\d+<\/code>\s*<name>([a-z-]+)<\/name>/.exec(xml);
  return m && STATES.includes(m[1]) ? m[1] : "unknown";
};

// Does the dashboard answer? (A subrequest to our own hostname goes to the tunnel, not through this Worker.)
async function dashboardUp(url) {
  try {
    const res = await fetch(new URL("/login", url), { redirect: "manual", cf: { cacheTtl: 0 } });
    return !DOWN.has(res.status);
  } catch {
    return false;
  }
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function power(request, env, url) {
  const op = url.pathname.slice("/_power".length);
  if (op === "" || op === "/") return page(200);
  if (op === "/state" && request.method === "GET") {
    const [state, up] = await Promise.all([ec2(env, "DescribeInstances").then(stateOf), dashboardUp(url)]);
    return json({ state, up });
  }
  if ((op === "/start" || op === "/stop") && request.method === "POST") {
    // Only a logged-in Access session, and only from this page: a custom header can't be sent cross-site
    if (!request.headers.get("cf-access-jwt-assertion")) return json({ error: "Not logged in through Cloudflare Access" }, 403);
    if (request.headers.get("x-power") !== "1") return json({ error: "Bad request" }, 400);
    const state = stateOf(await ec2(env, op === "/start" ? "StartInstances" : "StopInstances"));
    return json({ state });
  }
  return json({ error: "Not found" }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/_power" || url.pathname.startsWith("/_power/")) {
      try {
        return await power(request, env, url);
      } catch (err) {
        return json({ error: String(err.message || err) }, 502);
      }
    }
    let res;
    try {
      res = await fetch(request);
    } catch {
      res = null;
    }
    // A page load while the server is off: the power page instead of Cloudflare's error. API calls get the error as is.
    const pageLoad = request.method === "GET" && (request.headers.get("accept") || "").includes("text/html");
    if ((!res || DOWN.has(res.status)) && pageLoad) return page(503);
    return res || new Response("Server unreachable", { status: 502 });
  },
};

function page(status) {
  return new Response(HTML, {
    status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "retry-after": "20" },
  });
}

const HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlgoTrade server</title>
<style>
  :root { color-scheme: dark; --bg: #0b0e14; --panel: #141923; --line: #262d3b; --ink: #e8ecf3; --muted: #8b95a7;
          --up: #22c55e; --down: #ef4444; --warn: #f59e0b; --accent: #7c8cff; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding: 20px; }
  .card { width: min(420px, 100%); background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 24px; }
  h1 { font-size: 17px; margin: 0 0 18px; }
  .state { display: flex; align-items: center; gap: 10px; font-size: 22px; font-weight: 650; }
  .dot { width: 12px; height: 12px; border-radius: 50%; background: var(--muted); flex: none; }
  .running .dot { background: var(--up); } .stopped .dot { background: var(--down); }
  .pending .dot, .stopping .dot { background: var(--warn); animation: pulse 1s infinite alternate; }
  @keyframes pulse { to { opacity: .3; } }
  .note { color: var(--muted); margin: 8px 0 20px; min-height: 3em; }
  .acts { display: flex; gap: 10px; flex-wrap: wrap; }
  button, a.btn { font: inherit; font-weight: 600; border-radius: 9px; padding: 10px 18px; cursor: pointer; border: 1px solid var(--line);
                  background: none; color: var(--ink); text-decoration: none; }
  button.start { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.stop { color: var(--down); border-color: color-mix(in srgb, var(--down) 50%, transparent); }
  button[disabled] { opacity: .45; cursor: default; }
  .err { color: var(--down); margin-top: 14px; min-height: 1.5em; font-size: 13px; }
  .foot { color: var(--muted); font-size: 12.5px; margin-top: 18px; border-top: 1px solid var(--line); padding-top: 12px; }
</style></head>
<body><main class="card">
  <h1>AlgoTrade server</h1>
  <div class="state" id="st"><span class="dot"></span><span id="stTxt">Checking…</span></div>
  <div class="note" id="note"></div>
  <div class="acts">
    <button type="button" class="start" id="start" hidden>Start server</button>
    <a class="btn" id="open" href="/" hidden>Open dashboard</a>
    <button type="button" class="stop" id="stop" hidden>Stop server</button>
  </div>
  <div class="err" id="err" role="alert"></div>
  <div class="foot">Starts by itself at 08:30 IST and stops at 00:05 IST, Monday to Friday.
    A start takes about a minute; the dashboard opens when it answers.</div>
</main>
<script>
const $ = id => document.getElementById(id);
const onPowerPage = location.pathname.startsWith("/_power");
let busy = false, timer = null;
const TEXT = {
  running: ["Running", "The server is on."],
  pending: ["Starting…", "The server is booting; the dashboard follows in about a minute."],
  stopping: ["Stopping…", "The apps are shutting down and the databases are being backed up."],
  none: ["No server", "There is no AlgoTrade server to start (it was deleted). Launch one from the PC with \"Start AlgoTrade.bat\"."],
  stopped: ["Stopped", "The server is off: no scans, no alerts and no Auto-trade until it is started."],
};
async function refresh() {
  clearTimeout(timer);
  let s;
  try {
    const r = await fetch("/_power/state", { cache: "no-store" });
    s = await r.json();
    if (!r.ok) throw new Error(s.error || r.status);
    $("err").textContent = "";
  } catch (e) {
    $("err").textContent = "Couldn't read the server's state: " + e.message;
    timer = setTimeout(refresh, 10000);
    return;
  }
  // Came here because the dashboard was down, and now it answers: go back to it
  if (!onPowerPage && s.state === "running" && s.up) { location.reload(); return; }
  const [label, note] = TEXT[s.state] || [s.state, ""];
  $("st").className = "state " + s.state;
  $("stTxt").textContent = label;
  $("note").textContent = s.state === "running" && !s.up ? "The server is on; the dashboard is still starting up." : note;
  $("start").hidden = s.state !== "stopped";
  $("stop").hidden = !onPowerPage || s.state !== "running";
  $("open").hidden = !(s.state === "running" && s.up);
  timer = setTimeout(refresh, s.state === "stopped" || (s.state === "running" && s.up) ? 30000 : 4000);
}
async function act(op) {
  if (busy) return;
  if (op === "stop" && !confirm("Stop the server? Scans, alerts and Auto-trade stop until it is started again.")) return;
  busy = true; $("start").disabled = $("stop").disabled = true;
  try {
    const r = await fetch("/_power/" + op, { method: "POST", headers: { "X-Power": "1" } });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
  } catch (e) {
    $("err").textContent = "Couldn't " + op + " the server: " + e.message;
  } finally {
    busy = false; $("start").disabled = $("stop").disabled = false;
    refresh();
  }
}
$("start").onclick = () => act("start");
$("stop").onclick = () => act("stop");
refresh();
</script></body></html>`;
