# Copyright 2026 Kemal Yaylali
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The dashboard: five tabs, one viewport, no scrolling.

Layout contract. The page is a fixed three-row grid pinned to the
viewport height: header, tab bar, then a content region that takes the
remaining space and never overflows the fold. Each tab lays its panels
out on an internal grid sized in fractions of that region, so nothing
depends on content height. Where a panel genuinely holds more than fits
(the daily records table, the methods prose) that individual panel
scrolls internally while the page frame stays put. Below 1024 px the
constraint is released, because pinning five panels into a phone
viewport produces unreadable eight-pixel type, and a phone user expects
to scroll anyway.

Visual language carries over unchanged from the previous station page:
slate-950 ground, glass panels, Jakarta for prose and JetBrains Mono for
anything numeric. The one new structural device is the tab bar, and it
earns its place. Five distinct questions (what is it doing, what will it
do, what did it do, is the model any good, how does it work) were
previously one long scroll where the important things sat below the
fold.

The signature element is the estimator internals panel on the Live tab.
Most weather dashboards show numbers. This one shows the state estimator
working: self-heating coefficient, Kalman innovation, novelty distance
and drift pressure, all ticking at 2 Hz. It is the part of the system
that is normally invisible, and watching a filter converge is the most
honest possible demonstration that there is real machinery underneath.
"""

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ashvale Station</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap">
<style>
  body { font-family:'Plus Jakarta Sans',sans-serif; }
  .font-mono { font-family:'JetBrains Mono',monospace; }
  .glass {
    background: radial-gradient(130% 130% at 50% 0%, rgba(30,41,59,.5) 0%, rgba(15,23,42,.75) 100%);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border: 1px solid rgba(255,255,255,.08);
    box-shadow: 0 10px 30px -10px rgba(0,0,0,.5);
  }
  .tick { transition: width .5s cubic-bezier(.4,0,.2,1); }
  .tabbtn { transition: all .18s ease; }
  .tabbtn[aria-selected="true"] {
    background: rgba(99,102,241,.16); color:#c7d2fe; border-color: rgba(99,102,241,.4);
  }
  .pane { display:none; }
  .pane.active { display:grid; }
  .scroller { overflow-y:auto; scrollbar-width:thin; }
  .scroller::-webkit-scrollbar { width:7px; }
  .scroller::-webkit-scrollbar-thumb { background:rgba(148,163,184,.28); border-radius:8px; }
  .flash { animation: flash .5s ease-out; }
  @keyframes flash { from { color:#a5b4fc; } to { color:inherit; } }
  :focus-visible { outline:2px solid #818cf8; outline-offset:2px; border-radius:6px; }
  @media (prefers-reduced-motion: reduce) { *{animation:none!important;transition:none!important} }
  @media (min-width:1024px) {
    html,body { height:100%; overflow:hidden; }
    #shell { height:100dvh; }
  }
</style>
</head>
<body class="bg-slate-950 text-slate-100 antialiased selection:bg-indigo-500 selection:text-white">

<div class="fixed inset-0 pointer-events-none overflow-hidden -z-10">
  <div class="absolute -top-32 left-1/4 w-[500px] h-[500px] bg-indigo-600/15 rounded-full blur-[120px]"></div>
  <div class="absolute top-1/3 -right-32 w-[500px] h-[500px] bg-emerald-600/10 rounded-full blur-[120px]"></div>
  <div class="absolute bottom-10 left-10 w-[400px] h-[400px] bg-amber-600/10 rounded-full blur-[100px]"></div>
</div>

<div id="shell" class="max-w-[1600px] mx-auto px-3 sm:px-5 py-3 grid grid-rows-[auto_auto_1fr] gap-3 min-h-0">

  <header class="glass rounded-2xl px-4 py-2.5 flex flex-wrap items-center justify-between gap-3">
    <div class="flex items-center gap-3">
      <div class="p-2 bg-gradient-to-tr from-indigo-500/20 to-emerald-500/20 border border-white/10 rounded-xl">
        <svg class="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8"
            d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 00-9.78 2.096A4.001 4.001 0 003 15z"/>
        </svg>
      </div>
      <div>
        <h1 class="text-lg font-extrabold tracking-tight leading-none bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">Ashvale Station</h1>
        <p class="text-[10px] text-slate-500 font-mono mt-0.5">
          <span id="hd-hw">-</span> &middot; <span id="hd-days">0</span> d logged &middot; k=<span id="hd-k">-</span>
        </p>
      </div>
    </div>
    <div class="flex items-center gap-2 font-mono text-[11px]">
      <span id="hd-health" class="px-2 py-1 rounded-lg border bg-slate-500/15 text-slate-300 border-slate-500/20 uppercase font-semibold">health</span>
      <span class="flex items-center gap-1.5 bg-slate-900/80 px-2.5 py-1 rounded-lg border border-slate-800">
        <span id="hd-pulse" class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
        <span id="hd-time" class="text-white font-semibold">--:--:--</span>
      </span>
    </div>
  </header>

  <nav role="tablist" class="glass rounded-2xl p-1.5 flex gap-1.5 overflow-x-auto">
    <button role="tab" data-tab="live"     aria-selected="true"  class="tabbtn shrink-0 px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 border border-transparent hover:text-slate-200">Live</button>
    <button role="tab" data-tab="history"  aria-selected="false" class="tabbtn shrink-0 px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 border border-transparent hover:text-slate-200">History</button>
    <button role="tab" data-tab="models"   aria-selected="false" class="tabbtn shrink-0 px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 border border-transparent hover:text-slate-200">Models and Calibration</button>
    <button role="tab" data-tab="nerd"     aria-selected="false" class="tabbtn shrink-0 px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 border border-transparent hover:text-slate-200">Stats for Nerds</button>
    <button role="tab" data-tab="methods"  aria-selected="false" class="tabbtn shrink-0 px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 border border-transparent hover:text-slate-200">Methods</button>
  </nav>

  <main class="min-h-0">

  <!-- ---------------- LIVE ---------------- -->
  <section id="pane-live" class="pane active h-full min-h-0 gap-3 grid-cols-1 lg:grid-cols-4 lg:grid-rows-[auto_auto_1fr_auto]">

    <div class="glass rounded-2xl p-4 lg:col-span-4 shrink-0">
      <div class="flex items-center justify-between mb-2">
        <div>
          <h2 class="text-sm font-bold">Seven day outlook</h2>
          <p class="text-[10px] text-slate-500 font-mono">climatology plus decaying anomaly, not a synoptic forecast</p>
        </div>
        <span id="o-badge" class="px-2 py-0.5 rounded-lg bg-amber-500/10 text-amber-300 border border-amber-500/20 text-[9px] font-mono uppercase font-semibold">warming up</span>
      </div>
      <div id="o-strip" class="grid grid-cols-4 sm:grid-cols-7 gap-2"></div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-amber-400">
        <span class="flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>Temperature</span>
        <span class="text-slate-600 font-mono">KALMAN</span>
      </div>
      <div class="flex items-baseline gap-1 my-1"><span id="l-temp" class="text-5xl font-extrabold tracking-tight">--</span><span class="text-lg text-amber-400/70 font-semibold">&deg;C</span></div>
      <div class="font-mono text-[10px] text-slate-500 space-y-0.5">
        <div class="flex justify-between"><span>rate</span><span id="l-temp-rate" class="text-amber-300">--</span></div>
        <div class="flex justify-between"><span>raw / cpu</span><span id="l-temp-raw" class="text-slate-400">--</span></div>
      </div>
      <!-- Chart.js with maintainAspectRatio:false fills its parent, so a
           sparkline needs an explicitly sized relative wrapper or it eats the card. -->
      <div class="h-9 mt-1.5 shrink-0 relative"><canvas id="spark-temp"></canvas></div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-cyan-400">
        <span class="flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span>Humidity</span>
        <span class="text-slate-600 font-mono">HTS221</span>
      </div>
      <div class="flex items-baseline gap-1 my-1"><span id="l-hum" class="text-5xl font-extrabold tracking-tight">--</span><span class="text-lg text-cyan-400/70 font-semibold">%</span></div>
      <div class="font-mono text-[10px] text-slate-500 space-y-0.5">
        <div class="flex justify-between"><span>dew point</span><span id="l-dew" class="text-cyan-300">--</span></div>
        <div class="flex justify-between"><span>depression</span><span id="l-dep" class="text-slate-400">--</span></div>
      </div>
      <!-- Chart.js with maintainAspectRatio:false fills its parent, so a
           sparkline needs an explicitly sized relative wrapper or it eats the card. -->
      <div class="h-9 mt-1.5 shrink-0 relative"><canvas id="spark-hum"></canvas></div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-violet-400">
        <span class="flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-violet-400"></span>Barometer</span>
        <span class="text-slate-600 font-mono">MSL</span>
      </div>
      <div class="flex items-baseline gap-1 my-1"><span id="l-press" class="text-5xl font-extrabold tracking-tight">--</span><span class="text-lg text-violet-400/70 font-semibold">hPa</span></div>
      <div class="font-mono text-[10px] text-slate-500 space-y-0.5">
        <div class="flex justify-between"><span>tendency</span><span id="l-press-rate" class="text-violet-300">--</span></div>
        <div class="flex justify-between"><span>character</span><span id="l-press-char" class="text-slate-400 truncate ml-2">--</span></div>
      </div>
      <!-- Chart.js with maintainAspectRatio:false fills its parent, so a
           sparkline needs an explicitly sized relative wrapper or it eats the card. -->
      <div class="h-9 mt-1.5 shrink-0 relative"><canvas id="spark-press"></canvas></div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-emerald-400">
        <span class="flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>Sky</span>
        <div id="l-swatch" class="w-3 h-3 rounded-full border border-white/30"></div>
      </div>
      <div class="flex items-baseline gap-1 my-1"><span id="l-lux" class="text-5xl font-extrabold tracking-tight">--</span><span class="text-lg text-emerald-400/70 font-semibold">clr</span></div>
      <div class="font-mono text-[10px] text-slate-500 space-y-0.5">
        <div class="flex justify-between"><span>cloud index</span><span id="l-cloud" class="text-emerald-300">--</span></div>
        <div class="flex justify-between"><span>sun / cct</span><span id="l-sun" class="text-slate-400">--</span></div>
      </div>
      <!-- Chart.js with maintainAspectRatio:false fills its parent, so a
           sparkline needs an explicitly sized relative wrapper or it eats the card. -->
      <div class="h-9 mt-1.5 shrink-0 relative"><canvas id="spark-lux"></canvas></div>
    </div>

    <div class="glass rounded-2xl p-4 lg:col-span-3 flex flex-col min-h-0">
      <div class="flex flex-wrap items-center justify-between gap-2 pb-2 mb-2 border-b border-slate-800 shrink-0">
        <div class="flex items-center gap-2">
          <h2 class="text-sm font-bold">Observed and forecast</h2>
          <span class="px-1.5 py-0.5 text-[9px] font-mono rounded bg-indigo-500/10 text-indigo-300 border border-indigo-500/20 uppercase">90% band</span>
        </div>
        <div class="flex items-center gap-1.5">
          <div class="flex gap-1" id="live-span">
            <button data-min="60" class="px-2 py-1 rounded-lg text-[10px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">1 h</button>
            <button data-min="360" class="px-2 py-1 rounded-lg text-[10px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">6 h</button>
            <button data-min="1440" class="px-2 py-1 rounded-lg text-[10px] font-mono border border-slate-800 bg-indigo-600/20 text-indigo-300">24 h</button>
          </div>
          <select id="fc-target" class="bg-slate-900/90 border border-slate-800 text-slate-300 text-[11px] font-mono rounded-lg px-2 py-1">
            <option value="temperature">temperature</option><option value="humidity">humidity</option><option value="pressure">pressure</option>
          </select>
          <button id="fc-reset" class="px-2.5 py-1 rounded-lg text-[11px] font-mono bg-slate-800/60 text-slate-300 border border-slate-700 hover:bg-slate-800">Reset</button>
        </div>
      </div>
      <div class="flex-1 min-h-0 relative"><canvas id="fanChart"></canvas></div>
      <div id="fc-table" class="shrink-0 mt-2 pt-2 border-t border-slate-800 grid grid-cols-3 sm:grid-cols-6 gap-2 font-mono text-[10px]"></div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col min-h-0">
      <div class="pb-2 mb-2 border-b border-slate-800 shrink-0">
        <h2 class="text-sm font-bold">Conditions ahead</h2>
        <p class="text-[10px] text-indigo-400 font-mono">Zambretti prior + online logistic</p>
      </div>
      <div class="flex-1 min-h-0 space-y-2 pr-1">
        <div class="text-center">
          <div id="c-icon" class="flex justify-center mb-0.5"></div>
          <div id="c-label" class="text-base font-extrabold leading-tight">--</div>
          <div id="c-sky" class="text-[10px] text-slate-400 font-medium">--</div>
          <div class="text-[10px] text-slate-500 font-mono mt-0.5">Z=<span id="c-z">-</span> &middot; <span id="c-trend">-</span></div>
        </div>
        <div>
          <div class="flex justify-between items-baseline mb-1"><span class="text-[10px] text-slate-500 font-mono">rain probability</span><span id="c-pct" class="text-base font-bold text-cyan-300 font-mono">--</span></div>
          <div class="h-2 bg-slate-900 rounded-full overflow-hidden border border-slate-800"><div id="c-bar" class="tick h-full bg-gradient-to-r from-cyan-500 to-blue-600" style="width:0%"></div></div>
          <div class="flex justify-between text-[9px] font-mono text-slate-600 mt-1"><span>prior <span id="c-prior">-</span></span><span>learner <span id="c-model">-</span></span><span>trust <span id="c-trust">-</span></span></div>
        </div>
        <div class="bg-slate-900/70 rounded-xl border border-slate-800/80 p-2 space-y-1.5">
          <div class="text-[10px] text-slate-400 font-mono">Was it wet in the last hour?</div>
          <div class="flex gap-2">
            <button data-label="1" class="rain-label flex-1 px-2 py-1.5 rounded-lg bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 border border-blue-500/30 text-[11px] font-medium">Yes, rain</button>
            <button data-label="0" class="rain-label flex-1 px-2 py-1.5 rounded-lg bg-slate-800/60 hover:bg-slate-800 text-slate-300 border border-slate-700 text-[11px] font-medium">No, dry</button>
          </div>
          <div id="c-labstat" class="text-[9px] text-slate-600 font-mono"><span id="c-labn">0</span> confirmed observations</div>
        </div>
        <div>
          <div class="flex justify-between items-baseline mb-1">
            <span class="text-[10px] text-slate-500 font-mono">pressure, last 24 h</span>
            <span id="c-tend" class="text-[10px] text-violet-300 font-mono">--</span>
          </div>
          <div class="h-14 relative"><canvas id="tendChart"></canvas></div>
          <p class="text-[9px] text-slate-600 font-mono mt-1 leading-snug">The only signal that sees past your walls. Its slope drives the forecast.</p>
        </div>
      </div>
    </div>


    <div class="glass rounded-2xl px-4 py-2.5 lg:col-span-4 grid grid-cols-3 sm:grid-cols-5 lg:grid-cols-10 gap-x-4 gap-y-1.5 font-mono text-[10px]">
      <div><div class="text-slate-600 uppercase">wet bulb</div><div id="d-wb" class="text-slate-200 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">vpd</div><div id="d-vpd" class="text-slate-200 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">abs hum</div><div id="d-ah" class="text-slate-200 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">heat idx</div><div id="d-hi" class="text-slate-200 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">solar el</div><div id="d-el" class="text-slate-200 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">pitch</div><div id="d-pitch" class="text-amber-300 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">roll</div><div id="d-roll" class="text-cyan-300 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">yaw</div><div id="d-yaw" class="text-indigo-300 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">compass</div><div id="d-comp" class="text-emerald-300 font-semibold">--</div></div>
      <div><div class="text-slate-600 uppercase">accel z</div><div id="d-az" class="text-slate-200 font-semibold">--</div></div>
    </div>
  </section>

  <!-- ---------------- HISTORY ---------------- -->
  <section id="pane-history" class="pane h-full min-h-0 gap-3 grid-cols-1 lg:grid-cols-4 lg:grid-rows-[auto_1fr]">
    <div class="glass rounded-2xl px-4 py-3 lg:col-span-4 flex flex-wrap items-end gap-3">
      <div class="flex gap-1 flex-wrap" id="h-presets">
        <button data-h="6"    class="px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">6 h</button>
        <button data-h="24"   class="px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 bg-indigo-600/20 text-indigo-300">24 h</button>
        <button data-h="168"  class="px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">7 d</button>
        <button data-h="720"  class="px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">30 d</button>
        <button data-h="2160" class="px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">90 d</button>
        <button data-h="8760" class="px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200">1 y</button>
      </div>
      <div class="flex items-end gap-2">
        <label class="block"><span class="block text-[9px] text-slate-600 font-mono uppercase mb-0.5">from</span>
          <input id="h-from" type="datetime-local" class="bg-slate-950/70 border border-slate-800 rounded-lg px-2 py-1.5 text-[11px] font-mono text-white"></label>
        <label class="block"><span class="block text-[9px] text-slate-600 font-mono uppercase mb-0.5">to</span>
          <input id="h-to" type="datetime-local" class="bg-slate-950/70 border border-slate-800 rounded-lg px-2 py-1.5 text-[11px] font-mono text-white"></label>
        <button id="h-apply" class="px-3 py-1.5 rounded-lg text-[11px] font-medium bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30">Apply range</button>
      </div>
      <div class="flex items-center gap-2 ml-auto">
        <div id="h-series" class="flex gap-1"></div>
        <a id="h-csv" href="#" class="px-3 py-1.5 rounded-lg text-[11px] font-medium bg-slate-800/60 hover:bg-slate-800 text-slate-300 border border-slate-700">Export CSV</a>
      </div>
      <p id="h-meta" class="w-full text-[10px] text-slate-600 font-mono">-</p>
    </div>

    <div class="glass rounded-2xl p-4 lg:col-span-3 flex flex-col min-h-0">
      <div class="flex-1 min-h-0 relative"><canvas id="histChart"></canvas></div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col min-h-0">
      <div class="pb-2 mb-2 border-b border-slate-800 shrink-0 flex items-center justify-between">
        <h2 class="text-sm font-bold">Records</h2>
        <div class="flex gap-1">
          <button id="rec-tab-daily" class="px-2 py-0.5 rounded text-[10px] font-mono border bg-indigo-600/20 text-indigo-300 border-indigo-500/30">daily</button>
          <button id="rec-tab-all" class="px-2 py-0.5 rounded text-[10px] font-mono border border-slate-800 text-slate-500">all time</button>
        </div>
      </div>
      <div id="rec-daily" class="flex-1 min-h-0 scroller pr-1"></div>
      <div id="rec-all" class="flex-1 min-h-0 scroller pr-1 hidden space-y-1.5"></div>
    </div>

  </section>

  <!-- ---------------- MODELS AND CALIBRATION ---------------- -->
  <!-- Four rows, all sized by content except the log, which absorbs the slack.
       Nothing here uses an internal scroller: the scorecard is split into one
       column per target so all 18 heads are visible at once rather than hidden
       behind a scrollbar. -->
  <section id="pane-models" class="pane h-full min-h-0 gap-3 grid-cols-1 lg:grid-cols-4 lg:grid-rows-[auto_auto_1fr]">

    <div class="glass rounded-2xl p-4 lg:col-span-4 flex flex-col min-h-0">
      <div class="flex items-center justify-between pb-2 mb-2 border-b border-slate-800 shrink-0">
        <div>
          <h2 class="text-sm font-bold">Verification scorecard</h2>
          <p class="text-[10px] text-slate-500 font-mono">skill above zero means it beats persistence &middot; p/c/l is the ensemble weight</p>
        </div>
        <div class="flex gap-1.5">
          <button id="m-verify" class="px-2.5 py-1 rounded-lg text-[11px] font-mono bg-slate-800/60 hover:bg-slate-800 text-slate-300 border border-slate-700">Score now</button>
          <button id="m-train" class="px-2.5 py-1 rounded-lg text-[11px] font-mono bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30">Retrain</button>
        </div>
      </div>
      <div id="m-score" class="grid grid-cols-1 md:grid-cols-3 gap-3"></div>
      <p id="m-empty" class="text-[10px] text-slate-600 font-mono mt-2 leading-relaxed">No matured forecasts yet. Rows appear as each horizon reaches its validity time: 15 minutes first, 24 hours tomorrow.</p>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col">
      <div class="pb-2 mb-2 border-b border-slate-800 flex items-baseline justify-between">
        <h2 class="text-sm font-bold">Temperature</h2>
        <span class="text-[9px] font-mono text-slate-600 uppercase">self-heating</span>
      </div>
      <div class="font-mono text-[10px] space-y-1.5">
        <div class="flex justify-between text-slate-400"><span>coefficient k</span><span id="e-k" class="text-emerald-300 font-bold">--</span></div>
        <div class="flex justify-between text-slate-500"><span>cpu offset</span><span id="e-off">--</span></div>
        <div class="flex gap-1.5 pt-1">
          <input id="m-calin" type="number" step="0.1" placeholder="20.5" class="flex-1 min-w-0 bg-slate-950/70 border border-slate-800 rounded-lg px-2 py-1.5 text-white">
          <button id="m-calgo" class="px-2.5 py-1.5 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30">Set</button>
          <button id="m-calrst" class="px-2.5 py-1.5 rounded-lg bg-slate-800/60 hover:bg-slate-800 text-slate-400 border border-slate-700">Reset</button>
        </div>
        <div id="m-calstat" class="text-[9px] text-slate-600 leading-snug">Enter a trusted thermometer reading in &deg;C. One good reading is enough.</div>
      </div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col">
      <div class="pb-2 mb-2 border-b border-slate-800 flex items-baseline justify-between">
        <h2 class="text-sm font-bold">Humidity</h2>
        <span class="text-[9px] font-mono text-slate-600 uppercase">element bias</span>
      </div>
      <div class="font-mono text-[10px] space-y-1.5">
        <div class="flex justify-between text-slate-400"><span>offset</span><span id="e-hoff" class="text-cyan-300 font-bold">--</span></div>
        <div class="flex justify-between text-slate-500"><span>psychrometric</span><span id="e-hpsy">--</span></div>
        <div class="flex gap-1.5 pt-1">
          <input id="m-hcalin" type="number" step="0.5" min="0" max="100" placeholder="55" class="flex-1 min-w-0 bg-slate-950/70 border border-slate-800 rounded-lg px-2 py-1.5 text-white">
          <button id="m-hcalgo" class="px-2.5 py-1.5 rounded-lg bg-cyan-600/20 hover:bg-cyan-600/30 text-cyan-300 border border-cyan-500/30">Set</button>
          <button id="m-hcalrst" class="px-2.5 py-1.5 rounded-lg bg-slate-800/60 hover:bg-slate-800 text-slate-400 border border-slate-700">Reset</button>
        </div>
        <div id="m-hcalstat" class="text-[9px] text-slate-600 leading-snug">Enter a trusted hygrometer reading in %. The psychrometric term is derived, not fitted.</div>
      </div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col">
      <div class="pb-2 mb-2 border-b border-slate-800"><h2 class="text-sm font-bold">Estimator</h2></div>
      <div class="font-mono text-[10px] space-y-1">
        <div class="flex justify-between text-slate-400"><span>novelty d&sup2;</span><span id="e-nov" class="text-slate-200 font-bold">--</span></div>
        <div class="h-1.5 bg-slate-950 rounded-full overflow-hidden border border-slate-800"><div id="e-nov-bar" class="tick h-full bg-gradient-to-r from-emerald-500 to-amber-500" style="width:0%"></div></div>
        <div class="flex justify-between text-slate-400 pt-1"><span>drift pressure</span><span id="e-drift" class="text-slate-200 font-bold">--</span></div>
        <div class="h-1.5 bg-slate-950 rounded-full overflow-hidden border border-slate-800"><div id="e-drift-bar" class="tick h-full bg-gradient-to-r from-indigo-500 to-rose-500" style="width:0%"></div></div>
        <div id="e-health" class="pt-1 space-y-0.5"></div>
      </div>
    </div>

    <div class="glass rounded-2xl p-4 flex flex-col">
      <div class="pb-2 mb-2 border-b border-slate-800"><h2 class="text-sm font-bold">Storage and weights</h2></div>
      <div class="font-mono text-[10px] space-y-1">
        <div id="m-storage" class="space-y-1"></div>
        <div id="m-coef" class="space-y-0.5 pt-1.5 mt-1.5 border-t border-slate-800"></div>
      </div>
    </div>

    <div class="glass rounded-2xl p-4 lg:col-span-4 flex flex-col min-h-0 overflow-hidden">
      <h2 class="text-sm font-bold mb-2 shrink-0">Station log</h2>
      <div id="m-log" class="flex-1 min-h-0 overflow-hidden space-y-1 font-mono text-[10px]"></div>
    </div>
  </section>

  <!-- ---------------- STATS FOR NERDS ---------------- -->
  <!-- Everything the estimator and the 18 learners are actually carrying, read
       straight off the live objects. No internal scrollers: the head bank is a
       fixed 18 rows and the attribution list is capped at what fits. -->
  <section id="pane-nerd" class="pane h-full min-h-0 gap-3 grid-cols-1 lg:grid-cols-4 lg:grid-rows-[auto_1fr]">

    <div class="glass rounded-2xl p-3 lg:col-span-3">
      <div class="flex items-baseline justify-between mb-1.5">
        <h2 class="text-sm font-bold">Kalman bank</h2>
        <span class="text-[9px] font-mono text-slate-600 uppercase">NIS near 1 means honestly tuned</span>
      </div>
      <div id="n-filters" class="grid grid-cols-1 sm:grid-cols-3 gap-2 font-mono text-[10px]"></div>
    </div>

    <div class="glass rounded-2xl p-3">
      <h2 class="text-sm font-bold mb-1.5">Compensators</h2>
      <div id="n-comp" class="font-mono text-[10px] space-y-1"></div>
    </div>

    <div class="glass rounded-2xl p-3 lg:col-span-2 flex flex-col min-h-0">
      <div class="flex items-baseline justify-between mb-1.5 shrink-0">
        <h2 class="text-sm font-bold">Learner bank</h2>
        <span class="text-[9px] font-mono text-slate-600 uppercase">18 independent RLS heads</span>
      </div>
      <div id="n-heads" class="flex-1 min-h-0"></div>
    </div>

    <div class="glass rounded-2xl p-3 flex flex-col min-h-0">
      <div class="flex items-baseline justify-between mb-1.5 shrink-0">
        <h2 class="text-sm font-bold">Feature attribution</h2>
        <select id="n-head-sel" class="bg-slate-900/90 border border-slate-800 text-slate-300 text-[10px] font-mono rounded px-1.5 py-0.5"></select>
      </div>
      <p class="text-[9px] text-slate-600 font-mono mb-1 shrink-0">largest |&theta;| after standardisation, so these are comparable</p>
      <div id="n-theta" class="flex-1 min-h-0 font-mono text-[10px] space-y-0.5"></div>
    </div>

    <div class="glass rounded-2xl p-3 flex flex-col min-h-0 gap-2">
      <div>
        <h2 class="text-sm font-bold mb-1">Detectors</h2>
        <div id="n-mon" class="font-mono text-[10px] space-y-1"></div>
      </div>
      <div>
        <h2 class="text-sm font-bold mb-1">Climatology</h2>
        <div id="n-clim" class="font-mono text-[10px] space-y-0.5"></div>
      </div>
      <div>
        <h2 class="text-sm font-bold mb-1">Precipitation</h2>
        <div id="n-precip" class="font-mono text-[10px] space-y-0.5"></div>
      </div>
    </div>
  </section>

  <!-- ---------------- METHODS ---------------- -->
  <section id="pane-methods" class="pane h-full min-h-0 gap-3 grid-cols-1 lg:grid-cols-5">
    <div class="glass rounded-2xl p-4 lg:col-span-2 flex flex-col min-h-0">
      <div class="pb-2 mb-2 border-b border-slate-800 shrink-0">
        <h2 class="text-sm font-bold">How it is wired</h2>
        <p class="text-[10px] text-slate-500 font-mono">select a stage to read its rationale</p>
      </div>
      <div class="flex-1 min-h-0 scroller pr-1"><div id="me-diagram"></div></div>
    </div>
    <div class="glass rounded-2xl p-4 lg:col-span-3 flex flex-col min-h-0">
      <div id="me-head" class="pb-2 mb-2 border-b border-slate-800 shrink-0"></div>
      <div id="me-body" class="flex-1 min-h-0 scroller pr-1 space-y-3"></div>
    </div>
  </section>

  </main>
</div>

<script>
const el = (id) => document.getElementById(id);
let lastDerived = {};
const esc = (t) => String(t).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
  .replace(/</g,'&lt;').replace(/>/g,'&gt;');
// KaTeX renders after the pane is populated. If the CDN is unreachable the
// raw TeX stays visible, which is ugly but still readable, rather than blank.
function typeset(root) {
  if (typeof katex === 'undefined') return;
  (root||document).querySelectorAll('.tex[data-tex],.tex-inline[data-tex]').forEach(n => {
    if (n.dataset.done) return;
    const display = n.classList.contains('tex');
    try { katex.render(n.dataset.tex, n, {displayMode:display, throwOnError:false,
                                          output:'html', trust:false}); n.dataset.done='1'; }
    catch (e) { n.textContent = n.dataset.tex; }
  });
}
const fmt = (v,d=1) => (v===null||v===undefined||Number.isNaN(v)) ? '--' : Number(v).toFixed(d);
const SEV = { info:'text-slate-400', warn:'text-amber-300', error:'text-rose-300' };
const tsFmt = (t) => new Date(t*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const GRID = 'rgba(255,255,255,.04)';
const MONO = { family:'JetBrains Mono', size:10 };
const charts = {}, loaders = {};
let activeTab = 'live';

document.querySelectorAll('[role=tab]').forEach(b => b.addEventListener('click', () => selectTab(b.dataset.tab)));
function selectTab(name) {
  activeTab = name;
  document.querySelectorAll('[role=tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab===name)));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id==='pane-'+name));
  if (loaders[name]) loaders[name]();
  // Chart.js cannot measure a canvas inside display:none, so resize on reveal.
  setTimeout(() => Object.values(charts).forEach(c => c && c.resize()), 40);
}

/* ---------------- LIVE ---------------- */
const sparks = {};
function makeSpark(id, colour) {
  return new Chart(el(id).getContext('2d'), {
    type:'line',
    data:{ labels:[], datasets:[{ data:[], borderColor:colour, borderWidth:1.5, pointRadius:0, tension:.35, fill:false }] },
    options:{ responsive:true, maintainAspectRatio:false, animation:false,
      scales:{ x:{display:false}, y:{display:false} },
      plugins:{ legend:{display:false}, tooltip:{enabled:false} } }
  });
}
['temp','hum','press','lux'].forEach((k,i) =>
  sparks[k] = makeSpark('spark-'+k, ['#f59e0b','#06b6d4','#a78bfa','#34d399'][i]));
function pushSpark(k,v) {
  if (v===null || v===undefined) return;
  const d = sparks[k].data;
  d.labels.push(''); d.datasets[0].data.push(v);
  if (d.labels.length > 90) { d.labels.shift(); d.datasets[0].data.shift(); }
  sparks[k].update('none');
}

// The Live tab now carries the forecast chart, which already plots observed
// history alongside the prediction, so a separate rolling-window chart would
// only duplicate its left half. These buttons resize the observed portion.
let liveMin = 1440;
el('live-span').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  liveMin = Number(b.dataset.min);
  [...el('live-span').children].forEach(x => x.className =
    'px-2 py-1 rounded-lg text-[10px] font-mono border border-slate-800 ' +
    (x===b ? 'bg-indigo-600/20 text-indigo-300' : 'text-slate-400 hover:text-slate-200'));
  loadForecast();
});
loaders.live = () => { loadForecast(); loadOutlook(); };

function setFlash(id,val) {
  const node = el(id); if (!node || node.innerText===val) return;
  node.innerText = val; node.classList.remove('flash'); void node.offsetWidth; node.classList.add('flash');
}
function applyTelemetry(d) {
  if (!d) return;
  el('hd-time').innerText = d.timestamp || '--:--:--';
  el('hd-hw').innerText = d.simulated ? 'simulator' : 'sense hat v2';
  el('hd-k').innerText = fmt(d.compensator_k,3);
  setFlash('l-temp', fmt(d.temperature,2));
  setFlash('l-hum', fmt(d.humidity,1));
  setFlash('l-press', fmt(d.pressure,1));
  setFlash('l-lux', d.color ? String(d.color.clear) : '--');

  const rt = d.rates||{}, dv = d.derived||{};
  // Kept module-level so the precipitation panel can pick an icon from what
  // the sensors actually see, not just from the barometric class.
  lastDerived = Object.assign({}, dv, {temperature: d.temperature});
  el('l-temp-rate').innerText = (rt.temperature_c_per_h>=0?'+':'')+fmt(rt.temperature_c_per_h,2)+' \u00b0C/h';
  el('l-press-rate').innerText = (rt.pressure_hpa_per_h>=0?'+':'')+fmt(rt.pressure_hpa_per_h,2)+' hPa/h';
  el('l-temp-raw').innerText = fmt(d.temperature_raw,1)+' / '+fmt(d.cpu_temp,0)+'\u00b0';
  el('l-dew').innerText = fmt(dv.dew_point,1)+' \u00b0C';
  el('l-dep').innerText = fmt(dv.dew_depression,1)+' K';
  el('l-cloud').innerText = fmt(dv.cloud_index,2);
  el('l-sun').innerText = fmt(dv.solar_elevation,0)+'\u00b0 / '+(d.color&&d.color.cct?Math.round(d.color.cct)+'K':'n/a');
  if (d.color && d.color.hex) el('l-swatch').style.backgroundColor = d.color.hex;

  el('d-wb').innerText = fmt(dv.wet_bulb,1)+'\u00b0';
  el('d-vpd').innerText = fmt(dv.vpd_hpa,2);
  el('d-ah').innerText = fmt(dv.absolute_humidity_g_m3,1);
  el('d-hi').innerText = fmt(dv.heat_index,1)+'\u00b0';
  el('d-el').innerText = fmt(dv.solar_elevation,0)+'\u00b0';
  el('d-pitch').innerText = fmt(d.pitch,1)+'\u00b0';
  el('d-roll').innerText = fmt(d.roll,1)+'\u00b0';
  el('d-yaw').innerText = fmt(d.yaw,1)+'\u00b0';
  el('d-comp').innerText = fmt(d.compass,1)+'\u00b0';
  el('d-az').innerText = fmt(d.accel && d.accel.z,2);

  el('e-k').innerText = fmt(d.compensator_k,4);
  el('e-hoff').innerText = (d.hum_offset===undefined||d.hum_offset===null) ? '--' : (d.hum_offset>=0?'+':'')+fmt(d.hum_offset,2)+'%';
  el('e-hpsy').innerText = (d.hum_psychrometric===undefined||d.hum_psychrometric===null) ? '--' : (d.hum_psychrometric>=0?'+':'')+fmt(d.hum_psychrometric,2)+'%';
  el('e-off').innerText = fmt(d.cpu_offset,1)+' K';
  el('e-nov').innerText = fmt(d.novelty_d2,1);
  el('e-nov-bar').style.width = Math.min((d.novelty_d2||0)/24,1)*100+'%';

  pushSpark('temp', d.temperature); pushSpark('hum', d.humidity);
  pushSpark('press', d.pressure); pushSpark('lux', d.color && d.color.clear);
}
function applyPrecip(p) {
  if (!p || p.rain_probability===undefined) return;
  el('c-label').innerText = p.label||'--';
  // The barometer gives the class; the light sensor, sun angle and thermometer
  // decide which glyph honestly represents it.
  const wx = wxPick(p.condition, lastDerived.cloud_index, lastDerived.solar_elevation,
                    lastDerived.temperature, p.rain_probability);
  el('c-icon').innerHTML = wxSvg(wx[0], 56);
  el('c-sky').innerText = wx[1];
  el('c-z').innerText = p.zambretti_z!==undefined ? p.zambretti_z : '-';
  el('c-trend').innerText = p.pressure_characteristic||'-';
  if (p.tendency!==undefined) el('c-tend').innerText = (p.tendency>=0?'+':'')+fmt(p.tendency,2)+' hPa/h';
  el('l-press-char').innerText = p.pressure_characteristic||'--';
  const pct = Math.round(p.rain_probability*100);
  el('c-pct').innerText = pct+'%'; el('c-bar').style.width = pct+'%';
  el('c-prior').innerText = Math.round((p.prior_probability||0)*100)+'%';
  el('c-model').innerText = Math.round((p.model_probability||0)*100)+'%';
  el('c-trust').innerText = Math.round((p.learner_trust||0)*100)+'%';
  el('c-labn').innerText = p.strong_labels||0;
}
function connectStream() {
  const es = new EventSource('/api/stream');
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    applyTelemetry(d.telemetry); applyPrecip(d.precipitation);
    const dr = Math.round((d.drift_stress||0)*100);
    el('e-drift').innerText = dr+'%'; el('e-drift-bar').style.width = dr+'%';
    const b = el('hd-health'); b.innerText = d.health||'unknown';
    b.className = 'px-2 py-1 rounded-lg border uppercase font-semibold ' + (
      d.health==='ok' ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/20'
      : d.health==='warn' ? 'bg-amber-500/15 text-amber-300 border-amber-500/20'
      : 'bg-rose-500/15 text-rose-300 border-rose-500/20');
    el('hd-pulse').className = 'w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse';
  };
  es.onerror = () => { el('hd-pulse').className='w-1.5 h-1.5 rounded-full bg-rose-500'; es.close(); setTimeout(connectStream,5000); };
}

/* ---------------- FORECAST ---------------- */
charts.fan = new Chart(el('fanChart').getContext('2d'), {
  type:'line',
  data:{ datasets:[
    { label:'observed', data:[], borderColor:'#f59e0b', backgroundColor:'rgba(245,158,11,.12)',
      fill:true, borderWidth:2, pointRadius:0, tension:.3, parsing:false },
    { label:'upper', data:[], borderColor:'rgba(99,102,241,.25)', backgroundColor:'rgba(99,102,241,.14)',
      borderWidth:1, pointRadius:0, tension:.3, fill:'+1', parsing:false },
    { label:'lower', data:[], borderColor:'rgba(99,102,241,.25)', borderWidth:1,
      pointRadius:0, tension:.3, fill:false, parsing:false },
    { label:'forecast', data:[], borderColor:'#818cf8', borderDash:[6,4], borderWidth:2,
      pointRadius:3, pointBackgroundColor:'#818cf8', tension:.3, parsing:false } ]},
  options:{ responsive:true, maintainAspectRatio:false, animation:false,
    interaction:{ mode:'nearest', axis:'x', intersect:false },
    scales:{ x:{ type:'linear', grid:{color:GRID}, ticks:{ color:'#64748b', font:MONO, maxTicksLimit:7,
        callback:v=>new Date(v*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) } },
      y:{ grid:{color:GRID}, ticks:{ color:'#cbd5e1', font:MONO } } },
    plugins:{ legend:{display:false},
      tooltip:{ backgroundColor:'rgba(15,23,42,.95)', borderColor:'rgba(255,255,255,.1)', borderWidth:1,
        filter:i=>i.dataset.label!=='lower', titleFont:MONO, callbacks:{ title:i=>tsFmt(i[0].parsed.x) } },
      zoom:{ pan:{enabled:true,mode:'xy'}, zoom:{ wheel:{enabled:true,speed:.08}, pinch:{enabled:true}, mode:'xy' } } } }
});
charts.tend = new Chart(el('tendChart').getContext('2d'), {
  type:'line',
  data:{ datasets:[{ data:[], borderColor:'#a78bfa', backgroundColor:'rgba(167,139,250,.12)',
    fill:true, borderWidth:1.6, pointRadius:0, tension:.3, parsing:false }] },
  options:{ responsive:true, maintainAspectRatio:false, animation:false,
    scales:{ x:{ type:'linear', grid:{display:false}, ticks:{ color:'#475569', font:{family:'JetBrains Mono',size:8}, maxTicksLimit:4,
        callback:v=>new Date(v*1000).toLocaleTimeString([], {hour:'2-digit'}) } },
      y:{ grid:{color:GRID}, ticks:{ color:'#a78bfa', font:{family:'JetBrains Mono',size:8}, maxTicksLimit:4, callback:v=>v.toFixed(0) } } },
    plugins:{ legend:{display:false}, tooltip:{ backgroundColor:'rgba(15,23,42,.95)', titleFont:MONO,
      callbacks:{ title:i=>tsFmt(i[0].parsed.x) } } } }
});
el('fc-reset').addEventListener('click', ()=>charts.fan.resetZoom());
el('fc-target').addEventListener('change', loadForecast);

async function loadForecast() {
  const target = el('fc-target').value;
  const key = {temperature:'temp', humidity:'hum', pressure:'press'}[target];
  const res = await Promise.all([
    fetch('/api/history/range?hours='+(liveMin/60)).then(r=>r.json()),
    fetch('/api/forecast').then(r=>r.json()) ]);
  const hist = res[0], fc = res[1], s = hist.series||{};
  charts.fan.data.datasets[0].data = (s.ts||[]).map((t,i)=>({x:t,y:s[key][i]})).filter(p=>p.y!=null);
  // the pressure trace is the same fetch, reused: one request, two panels
  charts.tend.data.datasets[0].data = (s.ts||[]).map((t,i)=>({x:t,y:s.press[i]})).filter(p=>p.y!=null);
  charts.tend.update('none');
  const series = (fc.targets&&fc.targets[target])||[];
  const anchor = fc.anchors ? fc.anchors[target] : undefined;
  const head = (anchor!==undefined && fc.issued_ts) ? [{x:fc.issued_ts,y:anchor}] : [];
  charts.fan.data.datasets[3].data = head.concat(series.map(p=>({x:p.valid_ts,y:p.mu})));
  charts.fan.data.datasets[1].data = head.concat(series.map(p=>({x:p.valid_ts,y:p.hi})));
  charts.fan.data.datasets[2].data = head.concat(series.map(p=>({x:p.valid_ts,y:p.lo})));
  charts.fan.update('none');
  el('fc-table').innerHTML = series.length ? series.map(p=>
    '<div class="bg-slate-900/60 rounded-lg border border-slate-800/70 px-2 py-1.5">'+
    '<div class="text-slate-600 uppercase text-[9px]">'+p.horizon_label+'</div>'+
    '<div class="text-slate-100 font-bold text-xs">'+p.mu.toFixed(1)+'</div>'+
    '<div class="text-indigo-400/80 text-[9px]">&plusmn;'+((p.hi-p.lo)/2).toFixed(2)+'</div></div>').join('')
    : '<p class="col-span-full text-[10px] text-slate-600">Awaiting the first training pass.</p>';
}
async function loadOutlook() {
  const d = await fetch('/api/outlook').then(r=>r.json());
  el('o-badge').innerText = d.ready ? (d.annual_terms?'seasonal terms on':'diurnal only') : 'warming up';
  const rows = (d.targets&&d.targets.temperature)||[];
  if (!rows.length) { el('o-strip').innerHTML = '<p class="col-span-full text-[10px] text-slate-600 font-mono">Needs about two days of history before the harmonic fit means anything.</p>'; return; }
  const byDay = {};
  rows.forEach(r => { const k = new Date(r.ts*1000).toLocaleDateString([], {weekday:'short'}); (byDay[k]=byDay[k]||[]).push(r); });
  el('o-strip').innerHTML = Object.keys(byDay).slice(0,7).map(day=>{
    const v = byDay[day];
    const hi = Math.max.apply(null, v.map(x=>x.mu)), lo = Math.min.apply(null, v.map(x=>x.mu));
    const sp = Math.max.apply(null, v.map(x=>x.hi-x.lo))/2;
    return '<div class="bg-slate-900/70 rounded-xl border border-slate-800/80 p-2 text-center">'+
      '<div class="text-[9px] uppercase text-slate-600 font-mono">'+day+'</div>'+
      '<div class="text-base font-bold font-mono mt-0.5">'+hi.toFixed(1)+'\u00b0</div>'+
      '<div class="text-[10px] text-slate-500 font-mono">'+lo.toFixed(1)+'\u00b0</div>'+
      '<div class="text-[9px] text-indigo-400/80 font-mono">&plusmn;'+sp.toFixed(1)+'</div></div>';
  }).join('');
}
document.querySelectorAll('.rain-label').forEach(b => b.addEventListener('click', async () => {
  const res = await fetch('/api/label', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({kind:'rain', value:Number(b.dataset.label)}) }).then(r=>r.json());
  el('c-labstat').innerHTML = '<span class="text-emerald-300">Recorded.</span> '+(res.strong_labels||0)+' confirmed, loss '+fmt(res.loss,3);
}));

/* ---------------- HISTORY ---------------- */
let hRange = { hours:24, start:null, end:null }, histData = null;
const SERIES_META = {
  temp:{ label:'temperature', colour:'#f59e0b', axis:'y', on:true },
  hum:{ label:'humidity', colour:'#06b6d4', axis:'y1', on:false },
  press:{ label:'pressure', colour:'#a78bfa', axis:'y2', on:true },
  dew:{ label:'dew point', colour:'#34d399', axis:'y', on:false }
};
el('h-series').innerHTML = Object.keys(SERIES_META).map(k=>
  '<button data-s="'+k+'" class="px-2 py-1 rounded-lg text-[10px] font-mono border"></button>').join('');
function paintSeriesButtons() {
  document.querySelectorAll('#h-series button').forEach(b => {
    const m = SERIES_META[b.dataset.s];
    b.innerText = b.dataset.s;
    b.style.borderColor = m.on ? m.colour+'66' : 'rgb(30,41,59)';
    b.style.backgroundColor = m.on ? m.colour+'22' : 'transparent';
    b.style.color = m.on ? m.colour : 'rgb(100,116,139)';
  });
}
paintSeriesButtons();

charts.hist = new Chart(el('histChart').getContext('2d'), {
  type:'line', data:{ datasets:[] },
  options:{ responsive:true, maintainAspectRatio:false, animation:false,
    interaction:{ mode:'index', intersect:false },
    scales:{
      x:{ type:'linear', grid:{color:GRID}, ticks:{ color:'#64748b', font:MONO, maxTicksLimit:9,
        callback:v=>{ const d=new Date(v*1000); const span=(hRange.end||0)-(hRange.start||0);
          return span > 3*86400 ? d.toLocaleDateString([], {month:'short',day:'numeric'})
                                : d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); } } },
      y:{ position:'left', grid:{color:GRID}, ticks:{ color:'#f59e0b', font:MONO, callback:v=>v.toFixed(1)+'\u00b0' } },
      y1:{ position:'right', display:false, grid:{drawOnChartArea:false}, ticks:{ color:'#06b6d4', font:MONO } },
      y2:{ position:'right', grid:{drawOnChartArea:false}, ticks:{ color:'#a78bfa', font:MONO, callback:v=>v.toFixed(0) } } },
    plugins:{ legend:{display:false},
      tooltip:{ backgroundColor:'rgba(15,23,42,.95)', borderColor:'rgba(255,255,255,.1)', borderWidth:1,
        titleFont:MONO, bodyFont:{family:'Plus Jakarta Sans',size:11},
        filter:i=>i.dataset.label.charAt(0)!=='_',
        callbacks:{ title:i=>tsFmt(i[0].parsed.x) } },
      zoom:{ pan:{enabled:true,mode:'x'}, zoom:{ wheel:{enabled:true,speed:.08}, pinch:{enabled:true}, mode:'x' } } } }
});

el('h-presets').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  [...el('h-presets').children].forEach(x => x.className =
    'px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 ' +
    (x===b ? 'bg-indigo-600/20 text-indigo-300' : 'text-slate-400 hover:text-slate-200'));
  hRange = { hours:Number(b.dataset.h), start:null, end:null };
  loadHistory();
});
el('h-series').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  SERIES_META[b.dataset.s].on = !SERIES_META[b.dataset.s].on;
  paintSeriesButtons(); drawHistory();
});
el('h-apply').addEventListener('click', () => {
  const a = el('h-from').value, z = el('h-to').value;
  if (!a || !z) { el('h-meta').innerText = 'Pick both a from and a to date.'; return; }
  const s = new Date(a).getTime()/1000, e = new Date(z).getTime()/1000;
  if (e <= s) { el('h-meta').innerText = 'The to date must be after the from date.'; return; }
  hRange = { hours:null, start:s, end:e };
  [...el('h-presets').children].forEach(x => x.className =
    'px-2.5 py-1.5 rounded-lg text-[11px] font-mono border border-slate-800 text-slate-400 hover:text-slate-200');
  loadHistory();
});
function rangeQuery() {
  return hRange.hours!=null ? 'hours='+hRange.hours : 'start='+hRange.start+'&end='+hRange.end;
}
async function loadHistory() {
  el('h-meta').innerText = 'Loading...';
  const r = await fetch('/api/history/range?'+rangeQuery()).then(r=>r.json());
  histData = r;
  if (r.start) hRange.start = r.start;
  if (r.end) hRange.end = r.end;
  el('h-csv').href = '/api/export.csv?'+rangeQuery();
  const bs = r.bucket_s||0;
  const bl = bs>=86400 ? (bs/86400)+' d' : bs>=3600 ? (bs/3600)+' h' : (bs/60)+' min';
  el('h-meta').innerText = r.n
    ? r.n+' points at '+bl+' resolution, '+tsFmt(r.start)+' to '+tsFmt(r.end)
    : 'No data in that range. The station may not have been running then.';
  drawHistory(); loadRecords();
}
function drawHistory() {
  if (!histData || !histData.n) { charts.hist.data.datasets = []; charts.hist.update('none'); return; }
  const s = histData.series, ds = [];
  // Range band: min and max within each bucket, so an hourly view still shows
  // that the hour spanned four degrees rather than implying a flat mean.
  if (SERIES_META.temp.on && s.temp_hi) {
    ds.push({ label:'_hi', data:s.ts.map((t,i)=>({x:t,y:s.temp_hi[i]})), borderColor:'transparent',
      backgroundColor:'rgba(245,158,11,.10)', fill:'+1', pointRadius:0, parsing:false, yAxisID:'y', order:9 });
    ds.push({ label:'_lo', data:s.ts.map((t,i)=>({x:t,y:s.temp_lo[i]})), borderColor:'transparent',
      fill:false, pointRadius:0, parsing:false, yAxisID:'y', order:9 });
  }
  Object.keys(SERIES_META).forEach(k => {
    const m = SERIES_META[k];
    if (!m.on || !s[k]) return;
    ds.push({ label:m.label, data:s.ts.map((t,i)=>({x:t,y:s[k][i]})).filter(p=>p.y!=null),
      borderColor:m.colour, borderWidth:1.8, pointRadius:0, tension:.25, parsing:false, yAxisID:m.axis, order:1 });
  });
  charts.hist.options.scales.y1.display = SERIES_META.hum.on;
  charts.hist.options.scales.y2.display = SERIES_META.press.on;
  charts.hist.data.datasets = ds;
  charts.hist.update('none');
}
async function loadRecords() {
  const span = (hRange.end-hRange.start)||86400;
  const days = Math.max(2, Math.min(400, Math.ceil(span/86400)));
  const res = await Promise.all([
    fetch('/api/history/daily?days='+days).then(r=>r.json()),
    fetch('/api/records').then(r=>r.json()) ]);
  const daily = res[0].days||[], all = res[1];
  el('rec-daily').innerHTML = daily.length ?
    '<table class="w-full text-[10px] font-mono"><thead class="text-slate-600 uppercase text-[9px] sticky top-0 bg-slate-950/90 backdrop-blur">'+
    '<tr class="border-b border-slate-800"><th class="text-left py-1">day</th><th class="text-right">min</th>'+
    '<th class="text-right">max</th><th class="text-right">hPa</th></tr></thead><tbody>'+
    daily.map(d=>'<tr class="border-b border-slate-800/40"><td class="py-1 text-slate-400">'+d.day.slice(5)+'</td>'+
      '<td class="text-right text-cyan-300">'+fmt(d.temp_min,1)+'</td>'+
      '<td class="text-right text-amber-300">'+fmt(d.temp_max,1)+'</td>'+
      '<td class="text-right text-slate-500">'+fmt(d.press_mean,0)+'</td></tr>').join('')+
    '</tbody></table>'
    : '<p class="text-[10px] text-slate-600 font-mono">No completed days yet.</p>';
  const R = (k,label,unit,dec) => all[k] ?
    '<div class="flex justify-between items-baseline bg-slate-900/60 rounded-lg border border-slate-800/70 px-2 py-1.5">'+
    '<span class="text-slate-500 text-[10px]">'+label+'</span><span class="text-right">'+
    '<span class="text-slate-100 font-bold text-[11px]">'+fmt(all[k].value,dec===undefined?1:dec)+unit+'</span>'+
    '<span class="block text-slate-600 text-[9px]">'+tsFmt(all[k].ts)+'</span></span></div>' : '';
  el('rec-all').innerHTML = [
    R('temp_max','warmest','\u00b0'), R('temp_min','coldest','\u00b0'),
    R('press_max','highest pressure',''), R('press_min','lowest pressure',''),
    R('hum_max','most humid','%'), R('hum_min','driest','%'),
    R('rate_fall','fastest fall','/h',2), R('rate_rise','fastest rise','/h',2)
  ].join('') || '<p class="text-[10px] text-slate-600 font-mono">No records yet.</p>';
}
function toggleRec(daily) {
  el('rec-daily').classList.toggle('hidden', !daily);
  el('rec-all').classList.toggle('hidden', daily);
  el('rec-tab-daily').className = 'px-2 py-0.5 rounded text-[10px] font-mono border '+(daily?'bg-indigo-600/20 text-indigo-300 border-indigo-500/30':'border-slate-800 text-slate-500');
  el('rec-tab-all').className = 'px-2 py-0.5 rounded text-[10px] font-mono border '+(!daily?'bg-indigo-600/20 text-indigo-300 border-indigo-500/30':'border-slate-800 text-slate-500');
}
el('rec-tab-daily').addEventListener('click', ()=>toggleRec(true));
el('rec-tab-all').addEventListener('click', ()=>toggleRec(false));
loaders.history = () => { if (!histData) loadHistory(); };

/* ---------------- MODELS ---------------- */
async function loadModels() {
  const res = await Promise.all([
    fetch('/api/scorecard').then(r=>r.json()), fetch('/api/models').then(r=>r.json()),
    fetch('/api/status').then(r=>r.json()), fetch('/api/storage').then(r=>r.json()) ]);
  const sc = res[0], md = res[1], st = res[2], sg = res[3];
  el('hd-days').innerText = fmt(st.history_days,2);

  const wmap = {};
  (md.nowcast||[]).forEach(h => { wmap[h.target+'@'+h.horizon_s] = h.weights; });
  const rows = sc.rows||[];
  el('m-empty').style.display = rows.length ? 'none' : 'block';
  // One column per target rather than one 18-row table: every head stays
  // visible instead of hiding behind a scrollbar.
  const byTarget = {};
  rows.forEach(r => { (byTarget[r.target] = byTarget[r.target] || []).push(r); });
  el('m-score').innerHTML = Object.keys(byTarget).map(target => {
    const body = byTarget[target].map(r => {
      const sk = r.skill||0;
      const cls = sk>0.05?'text-emerald-300':sk<-0.05?'text-rose-300':'text-slate-400';
      const lead = r.horizon_s<3600 ? (r.horizon_s/60)+'m' : r.horizon_s<86400 ? (r.horizon_s/3600)+'h' : (r.horizon_s/86400)+'d';
      const w = wmap[r.target+'@'+r.horizon_s];
      const ws = w ? Math.round(w.persistence*100)+'/'+Math.round(w.climatology*100)+'/'+Math.round(w.learned*100) : '-';
      return '<tr class="border-b border-slate-800/40">'+
        '<td class="py-1 text-slate-500">'+lead+'</td>'+
        '<td class="text-right text-slate-200">'+fmt(r.mae,2)+'</td>'+
        '<td class="text-right text-slate-600">'+fmt(r.mae_persistence,2)+'</td>'+
        '<td class="text-right font-bold '+cls+'">'+Math.round(sk*100)+'%</td>'+
        '<td class="text-right text-slate-400">'+Math.round((r.coverage||0)*100)+'%</td>'+
        '<td class="text-right text-slate-600 pl-2">'+ws+'</td></tr>';
    }).join('');
    return '<div class="bg-slate-900/40 rounded-xl border border-slate-800/70 p-2.5">'+
      '<div class="text-[10px] font-semibold uppercase tracking-wider text-slate-300 mb-1">'+target+'</div>'+
      '<table class="w-full text-[10px] font-mono"><thead class="text-slate-600 uppercase text-[9px]">'+
      '<tr class="border-b border-slate-800"><th class="text-left py-1">lead</th><th class="text-right">MAE</th>'+
      '<th class="text-right">pers</th><th class="text-right">skill</th><th class="text-right">cov</th>'+
      '<th class="text-right pl-2">p/c/l</th></tr></thead><tbody>'+body+'</tbody></table></div>';
  }).join('');

  el('m-storage').innerHTML = (sg.tiers||[]).map(t=>
    '<div class="flex justify-between text-slate-500"><span>'+t.label+'</span>'+
    '<span class="text-slate-300">'+t.rows.toLocaleString()+' rows</span></div>').join('')+
    '<div class="flex justify-between text-slate-600 pt-1 mt-1 border-t border-slate-800"><span>database</span>'+
    '<span>'+(sg.bytes/1e6).toFixed(2)+' MB</span></div>';

  const coef = ((md.precipitation||{}).coefficients||[]).slice()
    .sort((a,b)=>Math.abs(b.weight)-Math.abs(a.weight)).slice(0,6);
  const mx = Math.max.apply(null, coef.map(c=>Math.abs(c.weight)).concat([1e-6]));
  el('m-coef').innerHTML = coef.map(c=>
    '<div class="flex items-center gap-1.5"><span class="w-24 truncate text-slate-600 text-[9px]">'+c.feature+'</span>'+
    '<div class="flex-1 h-1 bg-slate-950 rounded-full overflow-hidden"><div class="h-full '+
    (c.weight>=0?'bg-emerald-500':'bg-rose-500')+'" style="width:'+(Math.abs(c.weight)/mx*100)+'%"></div></div>'+
    '<span class="w-10 text-right text-slate-500 text-[9px]">'+c.weight.toFixed(2)+'</span></div>').join('');

  el('m-log').innerHTML = (st.events||[]).map(e=>
    '<div class="flex gap-2 border-b border-slate-800/40 pb-1">'+
    '<span class="text-slate-700 shrink-0">'+new Date(e.ts*1000).toLocaleTimeString()+'</span>'+
    '<span class="text-slate-600 shrink-0 w-16">'+e.kind+'</span>'+
    '<span class="'+(SEV[e.severity]||'text-slate-400')+'">'+e.detail+'</span></div>').join('')
    || '<span class="text-slate-700">Nothing logged yet.</span>';

  const an = await fetch('/api/anomaly').then(r=>r.json());
  el('e-health').innerHTML = Object.keys(an.health||{}).map(k=>{
    const v = an.health[k];
    const dot = v.status==='ok'?'bg-emerald-400':v.status==='warn'?'bg-amber-400':'bg-rose-400';
    return '<div class="flex justify-between items-center text-slate-500">'+
      '<span class="flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full '+dot+'"></span>'+k+'</span>'+
      '<span class="text-slate-600 text-[9px] truncate ml-2">'+v.detail+'</span></div>';
  }).join('');
}
loaders.models = loadModels;
el('m-train').addEventListener('click', async () => {
  el('m-train').innerText = 'Training...';
  const r = await fetch('/api/train', {method:'POST'}).then(r=>r.json());
  el('m-train').innerText = r.trained ? 'Trained '+r.grid_rows : 'Not enough data';
  setTimeout(()=>{ el('m-train').innerText='Retrain'; }, 4000);
  loadModels();
});
el('m-verify').addEventListener('click', async () => { await fetch('/api/verify',{method:'POST'}); loadModels(); });
el('m-calgo').addEventListener('click', async () => {
  const v = parseFloat(el('m-calin').value);
  if (Number.isNaN(v)) { el('m-calstat').innerText = 'Enter a temperature in degrees Celsius.'; return; }
  const r = await fetch('/api/calibrate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reference_c:v})}).then(r=>r.json());
  el('m-calstat').innerHTML = r.k!==undefined
    ? 'k is now <span class="text-emerald-300">'+r.k.toFixed(4)+'</span>, residual '+r.residual.toFixed(2)+' \u00b0C'
    : 'Rejected: no live reading yet.';
});
el('m-calrst').addEventListener('click', async () => {
  const r = await fetch('/api/calibrate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reset:true})}).then(r=>r.json());
  el('m-calstat').innerHTML = 'Reset to prior k = <span class="text-emerald-300">'+r.k+'</span>';
});
el('m-hcalgo').addEventListener('click', async () => {
  const v = parseFloat(el('m-hcalin').value);
  if (Number.isNaN(v) || v < 0 || v > 100) { el('m-hcalstat').innerText = 'Enter a relative humidity between 0 and 100%.'; return; }
  const r = await fetch('/api/calibrate/humidity', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reference_pct:v})}).then(r=>r.json());
  el('m-hcalstat').innerHTML = r.offset!==undefined
    ? 'offset is now <span class="text-cyan-300">'+r.offset.toFixed(2)+'%</span>, residual '+r.residual.toFixed(2)+'%'
    : 'Rejected: no live reading yet.';
});
el('m-hcalrst').addEventListener('click', async () => {
  const r = await fetch('/api/calibrate/humidity', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reset:true})}).then(r=>r.json());
  el('m-hcalstat').innerHTML = 'Reset to prior offset = <span class="text-cyan-300">'+r.offset+'%</span>';
});

/* ---------------- STATS FOR NERDS ---------------- */
let nerdDoc = null, nerdHead = null;
const HL = (h) => h<3600 ? (h/60)+'m' : h<86400 ? (h/3600)+'h' : (h/86400)+'d';
function bar(frac, colour) {
  const w = Math.max(0, Math.min(1, frac||0))*100;
  return '<div class="h-1 bg-slate-950 rounded-full overflow-hidden border border-slate-800/70">'+
         '<div class="h-full" style="width:'+w+'%;background:'+colour+'"></div></div>';
}
async function loadNerd() {
  const d = await fetch('/api/nerd').then(r=>r.json());
  nerdDoc = d;

  const COL = {temperature:'#f59e0b', humidity:'#06b6d4', pressure:'#a78bfa'};
  el('n-filters').innerHTML = Object.keys(d.filters||{}).map(k=>{
    const f = d.filters[k];
    // NIS is chi-square(1) distributed when consistent, so 1 is the target and
    // the bar is scaled to 3 as a "clearly wrong" ceiling.
    return '<div class="bg-slate-900/50 rounded-lg border border-slate-800/70 p-2">'+
      '<div class="flex justify-between mb-1"><span style="color:'+COL[k]+'">'+k+'</span>'+
      '<span class="text-slate-500">'+(f.initialised?'':'warming')+'</span></div>'+
      '<div class="flex justify-between text-slate-500">NIS<span class="text-slate-200 font-bold">'+fmt(f.nis,3)+'</span></div>'+
      bar(f.nis/3, COL[k])+
      '<div class="flex justify-between text-slate-500 mt-1">&sigma; level<span class="text-slate-300">'+fmt(f.sigma_level,4)+'</span></div>'+
      '<div class="flex justify-between text-slate-500">P rate<span class="text-slate-300">'+f.p_rate.toExponential(2)+'</span></div>'+
      '<div class="flex justify-between text-slate-600">q / r<span>'+f.q.toExponential(1)+' / '+fmt(f.r,3)+'</span></div>'+
      '</div>';
  }).join('');

  const th = (d.compensators||{}).thermal||{}, hu = (d.compensators||{}).humidity||{};
  el('n-comp').innerHTML =
    '<div class="flex justify-between text-slate-500">k<span class="text-emerald-300 font-bold">'+fmt(th.k,4)+'</span></div>'+
    '<div class="flex justify-between text-slate-600">P / n<span>'+fmt(th.P,3)+' / '+(th.n||0)+'</span></div>'+
    '<div class="flex justify-between text-slate-600">clamp<span>'+fmt(th.k_min,2)+' .. '+fmt(th.k_max,2)+'</span></div>'+
    '<div class="border-t border-slate-800 my-1"></div>'+
    '<div class="flex justify-between text-slate-500">RH offset<span class="text-cyan-300 font-bold">'+fmt(hu.offset,2)+'%</span></div>'+
    '<div class="flex justify-between text-slate-600">P / n<span>'+fmt(hu.P,3)+' / '+(hu.n||0)+'</span></div>'+
    '<div class="flex justify-between text-slate-600">psychrometric<span>'+(hu.psychrometric?'on':'off')+'</span></div>';

  const heads = d.heads||[];
  el('n-heads').innerHTML = heads.length ? '<table class="w-full font-mono text-[10px]">'+
    '<thead class="text-slate-600 uppercase text-[9px]"><tr class="border-b border-slate-800">'+
    '<th class="text-left py-0.5">head</th><th class="text-right">n</th><th class="text-right">tr P</th>'+
    '<th class="text-right">|&theta;|</th><th class="text-right">rmse</th><th class="text-right">&alpha;</th>'+
    '<th class="text-right">cov</th><th class="text-right">&plusmn;</th><th class="text-right pl-2">p/c/l</th></tr></thead><tbody>'+
    heads.map(h=>{
      const sat = h.trace_p/h.p_max;
      const cov = h.coverage===null||h.coverage===undefined ? '-' : Math.round(h.coverage*100)+'%';
      const covCls = (h.coverage!==null && Math.abs(h.coverage-(1-h.alpha_target))<0.03) ? 'text-emerald-300' : 'text-amber-300';
      const w = h.weights||{};
      return '<tr class="border-b border-slate-800/40">'+
        '<td class="py-0.5 text-slate-400">'+h.target.slice(0,4)+' <span class="text-slate-600">'+HL(h.horizon_s)+'</span></td>'+
        '<td class="text-right text-slate-600">'+h.n_updates+'</td>'+
        '<td class="text-right '+(sat>0.95?'text-rose-300':'text-slate-400')+'">'+h.trace_p.toExponential(1)+'</td>'+
        '<td class="text-right text-slate-300">'+fmt(h.theta_norm,1)+'</td>'+
        '<td class="text-right text-slate-300">'+fmt(h.rmse_ewma,3)+'</td>'+
        '<td class="text-right text-indigo-300">'+fmt(h.alpha,3)+'</td>'+
        '<td class="text-right '+covCls+'">'+cov+'</td>'+
        '<td class="text-right text-slate-500">'+(h.halfwidth===null?'-':fmt(h.halfwidth,2))+'</td>'+
        '<td class="text-right text-slate-600 pl-2">'+Math.round((w.persistence||0)*100)+'/'+
        Math.round((w.climatology||0)*100)+'/'+Math.round((w.learned||0)*100)+'</td></tr>';
    }).join('')+'</tbody></table>'
    : '<p class="text-[10px] text-slate-600 font-mono">No heads trained yet.</p>';

  const sel = el('n-head-sel');
  if (sel.options.length !== heads.length) {
    sel.innerHTML = heads.map((h,i)=>'<option value="'+i+'">'+h.target.slice(0,4)+' '+HL(h.horizon_s)+'</option>').join('');
  }
  if (nerdHead===null || nerdHead>=heads.length) nerdHead = 0;
  sel.value = String(nerdHead);
  drawTheta();

  const mo = d.monitoring||{}, nv = mo.novelty||{}, dr = mo.drift||{};
  el('n-mon').innerHTML =
    '<div class="flex justify-between text-slate-500">Mahalanobis d&sup2;<span class="text-slate-200 font-bold">'+fmt(nv.d2,2)+'</span></div>'+
    bar((nv.d2||0)/(nv.threshold||12), '#f59e0b')+
    '<div class="flex justify-between text-slate-600">threshold / dims<span>'+fmt(nv.threshold,0)+' / '+(nv.dims||0)+'</span></div>'+
    '<div class="flex justify-between text-slate-500 mt-1">Page-Hinkley m&#8314;<span class="text-slate-200 font-bold">'+fmt(dr.m_pos,3)+'</span></div>'+
    '<div class="flex justify-between text-slate-600">m&#8315; / alarms<span>'+fmt(dr.m_neg,3)+' / '+(dr.alarms||0)+'</span></div>';

  const cl = d.climatology||{};
  el('n-clim').innerHTML =
    '<div class="flex justify-between text-slate-500">history<span class="text-slate-300">'+fmt(cl.history_days,2)+' d</span></div>'+
    '<div class="flex justify-between text-slate-500">harmonics<span class="text-slate-300">'+(cl.diurnal_harmonics||0)+' diurnal, '+
      (cl.annual_terms?(cl.annual_harmonics||0):0)+' annual</span></div>'+
    Object.keys(cl.residual_std||{}).map(k=>
      '<div class="flex justify-between text-slate-600">&sigma; '+k.slice(0,4)+'<span>'+fmt(cl.residual_std[k],3)+'</span></div>').join('');

  const pr = d.precipitation||{};
  const co = (pr.coefficients||[]).slice().sort((a,b)=>Math.abs(b.weight)-Math.abs(a.weight)).slice(0,4);
  el('n-precip').innerHTML =
    '<div class="flex justify-between text-slate-500">labels<span class="text-slate-300">'+(pr.strong_labels||0)+' strong, '+(pr.weak_labels||0)+' weak</span></div>'+
    '<div class="flex justify-between text-slate-500">logloss<span class="text-slate-300">'+fmt(pr.logloss_ewma,4)+'</span></div>'+
    co.map(c=>'<div class="flex justify-between text-slate-600">'+String(c.feature||'?').slice(0,16)+'<span class="'+(c.weight>=0?'text-emerald-400':'text-rose-400')+'">'+
      (c.weight>=0?'+':'')+fmt(c.weight,3)+'</span></div>').join('');
}
function drawTheta() {
  if (!nerdDoc || !nerdDoc.heads || !nerdDoc.heads.length) return;
  const h = nerdDoc.heads[nerdHead], names = nerdDoc.feature_names||[];
  const pairs = (h.theta||[]).map((v,i)=>({n:names[i]||('f'+i), v:v}))
    .sort((a,b)=>Math.abs(b.v)-Math.abs(a.v)).slice(0,11);
  const mx = Math.max.apply(null, pairs.map(p=>Math.abs(p.v)).concat([1e-9]));
  el('n-theta').innerHTML = pairs.map(p=>
    '<div class="flex items-center gap-1.5">'+
    '<span class="text-slate-500 truncate" style="width:46%">'+p.n+'</span>'+
    '<span class="flex-1">'+bar(Math.abs(p.v)/mx, p.v>=0?'#34d399':'#fb7185')+'</span>'+
    '<span class="'+(p.v>=0?'text-emerald-400':'text-rose-400')+'" style="width:22%;text-align:right">'+
    (p.v>=0?'+':'')+fmt(p.v,3)+'</span></div>').join('');
}
loaders.nerd = loadNerd;

/* ---------------- WEATHER ICONS ----------------
   Inline SVG, no icon font and no extra request. The glyph is not chosen from
   the Zambretti class alone: the station also measures cloud index from the
   light sensor and knows the solar elevation, so a "fine" barometer under a
   thick overcast still draws a cloud, and after sunset it draws a moon rather
   than a sun. Snow is picked on temperature, not on the barometer. */
const WX = {
  sun: '<circle cx="32" cy="32" r="13" fill="#fbbf24"/>'+
       '<g stroke="#fbbf24" stroke-width="4" stroke-linecap="round">'+
       '<path d="M32 6v8M32 50v8M6 32h8M50 32h8M13 13l6 6M45 45l6 6M51 13l-6 6M19 45l-6 6"/></g>',
  moon: '<path d="M40 10a22 22 0 1 0 14 40A24 24 0 0 1 40 10z" fill="#e2e8f0"/>'+
        '<circle cx="20" cy="16" r="1.8" fill="#94a3b8"/><circle cx="14" cy="26" r="1.2" fill="#94a3b8"/>',
  cloud: '<path d="M20 46a11 11 0 0 1 1-22 15 15 0 0 1 28 4 9 9 0 0 1-2 18z" fill="#94a3b8"/>',
  partsun: '<circle cx="22" cy="22" r="9" fill="#fbbf24"/>'+
       '<g stroke="#fbbf24" stroke-width="3" stroke-linecap="round">'+
       '<path d="M22 4v6M4 22h6M9 9l4 4M35 9l-4 4"/></g>'+
       '<path d="M26 50a10 10 0 0 1 1-20 13 13 0 0 1 25 4 8 8 0 0 1-2 16z" fill="#cbd5e1"/>',
  partmoon: '<path d="M30 8a15 15 0 1 0 10 27A16 16 0 0 1 30 8z" fill="#e2e8f0"/>'+
       '<path d="M26 52a10 10 0 0 1 1-20 13 13 0 0 1 25 4 8 8 0 0 1-2 16z" fill="#cbd5e1"/>',
  rain: '<path d="M20 40a11 11 0 0 1 1-22 15 15 0 0 1 28 4 9 9 0 0 1-2 18z" fill="#94a3b8"/>'+
        '<g stroke="#38bdf8" stroke-width="3.5" stroke-linecap="round">'+
        '<path d="M22 46l-3 9M33 46l-3 9M44 46l-3 9"/></g>',
  showers: '<path d="M20 40a11 11 0 0 1 1-22 15 15 0 0 1 28 4 9 9 0 0 1-2 18z" fill="#a3adbb"/>'+
        '<g stroke="#38bdf8" stroke-width="3.5" stroke-linecap="round">'+
        '<path d="M26 46l-2 7M39 46l-2 7"/></g>',
  storm: '<path d="M20 38a11 11 0 0 1 1-22 15 15 0 0 1 28 4 9 9 0 0 1-2 18z" fill="#64748b"/>'+
        '<path d="M34 36l-10 15h7l-3 12 12-17h-7l4-10z" fill="#fbbf24"/>',
  snow: '<path d="M20 40a11 11 0 0 1 1-22 15 15 0 0 1 28 4 9 9 0 0 1-2 18z" fill="#cbd5e1"/>'+
        '<g stroke="#e0f2fe" stroke-width="3" stroke-linecap="round">'+
        '<path d="M22 48v8M18 52h8M33 48v8M29 52h8M44 48v8M40 52h8"/></g>',
};
function wxPick(condition, cloud, elevation, tempC, rainProb) {
  const night = (elevation !== undefined && elevation !== null && elevation < -1);
  const c = (cloud === undefined || cloud === null) ? 0.5 : cloud;
  if (condition === 'stormy') return ['storm', 'Thunderstorms'];
  // Snow is a temperature question, not a barometric one.
  if (tempC !== undefined && tempC !== null && tempC <= 1.5 && rainProb > 0.35)
    return ['snow', 'Snow possible'];
  if (condition === 'wet')       return ['rain', 'Wet and windy'];
  if (condition === 'rain')      return ['rain', 'Rain likely'];
  if (condition === 'unsettled') return ['showers', 'Showers around'];
  if (condition === 'changeable')
    return c > 0.7 ? ['cloud','Cloudy'] : [night?'partmoon':'partsun', 'Partly cloudy'];
  // settled / fine / fair: defer to what the light sensor actually sees.
  if (c < 0.25) return [night?'moon':'sun', night ? 'Clear skies' : 'Sunny, open skies'];
  if (c < 0.65) return [night?'partmoon':'partsun', 'Partly cloudy'];
  return ['cloud', 'Cloudy'];
}
function wxSvg(name, size) {
  return '<svg viewBox="0 0 64 64" width="'+size+'" height="'+size+'" aria-hidden="true">'+(WX[name]||WX.cloud)+'</svg>';
}

/* ---------------- METHODS ---------------- */
let methodsDoc = null, methodSel = 'acquire';
const STAGE_COLOUR = { acquire:'#94a3b8', compensate:'#34d399', kalman:'#f59e0b', features:'#06b6d4',
  nowcast:'#818cf8', conformal:'#a78bfa', climatology:'#38bdf8', precip:'#60a5fa',
  monitor:'#fb7185', verify:'#4ade80' };
async function loadMethods() {
  if (!methodsDoc) methodsDoc = await fetch('/api/methods').then(r=>r.json());
  drawDiagram(); drawStage();
}
function drawDiagram() {
  const p = methodsDoc.pipeline;
  el('me-diagram').innerHTML = p.map((s,i)=>{
    const c = STAGE_COLOUR[s.id]||'#94a3b8', on = s.id===methodSel;
    const edge = methodsDoc.flow.filter(f=>f.from===s.id)[0];
    return '<button data-stage="'+s.id+'" class="stagebtn w-full text-left rounded-xl border px-3 py-2 transition" '+
      'style="border-color:'+(on?c+'88':'rgba(255,255,255,.07)')+';background:'+(on?c+'1a':'rgba(15,23,42,.5)')+'">'+
      '<div class="flex items-center gap-2.5"><span class="font-mono text-[10px] w-5 shrink-0" style="color:'+c+'">'+s.stage+'</span>'+
      '<div class="min-w-0 flex-1"><div class="text-xs font-semibold '+(on?'text-white':'text-slate-300')+'">'+s.title+'</div>'+
      '<div class="text-[9px] font-mono text-slate-600 truncate">'+s.technique+'</div></div></div></button>'+
      (i<p.length-1 ? '<div class="flex items-center gap-1.5 pl-[26px] h-4"><div class="w-px h-full" style="background:'+c+'55"></div>'+
        '<span class="text-[8px] font-mono text-slate-700">'+(edge?edge.label:'')+'</span></div>' : '');
  }).join('')+
  '<div class="mt-3 pt-3 border-t border-slate-800"><div class="text-[9px] font-mono text-slate-600 uppercase mb-1.5">feedback edges</div>'+
  methodsDoc.flow.filter(f=>['verify','monitor','climatology'].indexOf(f.from)>=0).map(f=>
    '<div class="text-[9px] font-mono text-slate-600 flex items-center gap-1.5 mb-0.5">'+
    '<span class="text-slate-500">'+f.from+'</span><span class="text-indigo-500">&rarr;</span>'+
    '<span class="text-slate-500">'+f.to+'</span><span class="text-slate-700">'+f.label+'</span></div>').join('')+'</div>';
  document.querySelectorAll('.stagebtn').forEach(b => b.addEventListener('click', () => {
    methodSel = b.dataset.stage; drawDiagram(); drawStage();
  }));
}
function drawStage() {
  const s = methodsDoc.pipeline.filter(x=>x.id===methodSel)[0];
  if (!s) return;
  const c = STAGE_COLOUR[s.id]||'#94a3b8';
  el('me-head').innerHTML =
    '<div class="flex flex-wrap items-baseline gap-2.5"><span class="font-mono text-xs" style="color:'+c+'">stage '+s.stage+'</span>'+
    '<h2 class="text-base font-bold">'+s.title+'</h2>'+
    '<span class="ml-auto font-mono text-[10px] text-slate-600">'+s.module+'</span></div>'+
    '<p class="text-[11px] text-slate-500 font-mono mt-0.5">'+s.technique+'</p>';
  const params = Object.keys(s.params||{}).map(k=>
    '<div class="bg-slate-900/60 rounded-lg border border-slate-800/70 px-2.5 py-1.5">'+
    '<div class="text-[9px] text-slate-600 font-mono uppercase">'+k+'</div>'+
    '<div class="text-[11px] text-slate-200 font-mono font-semibold">'+s.params[k]+'</div></div>').join('');
  el('me-body').innerHTML =
    '<div class="grid grid-cols-2 gap-2 font-mono text-[10px]">'+
    '<div class="bg-slate-900/40 rounded-lg border border-slate-800/60 px-2.5 py-1.5">'+
    '<div class="text-slate-600 uppercase text-[9px]">consumes</div><div class="text-slate-300 mt-0.5">'+s.consumes+'</div></div>'+
    '<div class="bg-slate-900/40 rounded-lg border border-slate-800/60 px-2.5 py-1.5">'+
    '<div class="text-slate-600 uppercase text-[9px]">produces</div><div class="text-slate-300 mt-0.5">'+s.produces+'</div></div></div>'+
    (s.math ? '<div class="bg-slate-950/60 rounded-lg border border-slate-800/70 px-3 py-2.5 overflow-x-auto">'+
      '<div class="text-[9px] text-slate-600 font-mono uppercase mb-1">core relation</div>'+
      (Array.isArray(s.math)?s.math:[s.math]).map(m=>'<div class="tex" data-tex="'+esc(m)+'"></div>').join('')+'</div>' : '')+
    (s.symbols ? '<div><div class="text-[9px] text-slate-600 font-mono uppercase mb-1">symbols</div>'+
      '<div class="grid grid-cols-1 sm:grid-cols-2 gap-x-3 gap-y-1">'+
      Object.keys(s.symbols).map(k=>'<div class="flex gap-2 items-baseline">'+
        '<span class="tex-inline shrink-0" data-tex="'+esc(k)+'"></span>'+
        '<span class="text-[11px] text-slate-500 leading-snug">'+s.symbols[k]+'</span></div>').join('')+
      '</div></div>' : '')+
    '<div><div class="text-[9px] text-slate-600 font-mono uppercase mb-1">why it is done this way</div>'+
    '<p class="text-[12px] text-slate-300 leading-relaxed">'+s.why+'</p></div>'+
    '<div class="border-l-2 pl-3" style="border-color:'+c+'66">'+
    '<div class="text-[9px] font-mono uppercase mb-1" style="color:'+c+'">how it fails</div>'+
    '<p class="text-[12px] text-slate-400 leading-relaxed">'+s.failure+'</p></div>'+
    (params ? '<div><div class="text-[9px] text-slate-600 font-mono uppercase mb-1">live parameters</div>'+
      '<div class="grid grid-cols-2 sm:grid-cols-3 gap-2">'+params+'</div></div>' : '')+
    (methodSel==='verify' ? '<div class="pt-2 border-t border-slate-800">'+
      '<div class="text-[9px] text-slate-600 font-mono uppercase mb-1.5">honest limits of this station</div><ul class="space-y-1.5">'+
      methodsDoc.honest_limits.map(l=>'<li class="text-[11px] text-slate-400 leading-relaxed flex gap-2">'+
        '<span class="text-amber-500 shrink-0">&middot;</span><span>'+l+'</span></li>').join('')+'</ul></div>' : '')+
    (methodSel==='features' ? '<div class="pt-2 border-t border-slate-800">'+
      '<div class="text-[9px] text-slate-600 font-mono uppercase mb-1.5">the '+methodsDoc.features.length+' features</div>'+
      '<div class="flex flex-wrap gap-1">'+methodsDoc.features.map(f=>
        '<span class="px-1.5 py-0.5 rounded bg-slate-900/70 border border-slate-800 text-[9px] font-mono text-slate-500">'+f+'</span>').join('')+
      '</div></div>' : '')+
    (methodSel==='acquire' ? '<div class="pt-2 border-t border-slate-800">'+
      '<div class="text-[9px] text-slate-600 font-mono uppercase mb-1.5">glossary</div>'+
      methodsDoc.glossary.map(g=>'<div class="mb-2"><span class="text-[11px] font-semibold text-slate-300">'+g.term+'</span>'+
        '<p class="text-[11px] text-slate-500 leading-relaxed">'+g.definition+'</p></div>').join('')+'</div>' : '');
  typeset(el('me-body'));
}
loaders.methods = loadMethods;

/* ---------------- BOOT ---------------- */
connectStream();
loadForecast();
loadOutlook();
fetch('/api/status').then(r=>r.json()).then(s => { el('hd-days').innerText = fmt(s.history_days,2); });
setInterval(() => { if (activeTab==='live') { loadForecast(); loadOutlook(); } }, 60000);
el('n-head-sel').addEventListener('change', e => { nerdHead = Number(e.target.value); drawTheta(); });
setInterval(() => { if (activeTab==='models') loadModels(); }, 60000);
setInterval(() => { if (activeTab==='nerd') loadNerd(); }, 30000);
setInterval(() => { if (activeTab==='history') loadHistory(); }, 300000);
</script>
</body>
</html>
"""
