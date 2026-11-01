# ibkr-desktop-fix

[![CI](https://github.com/AotianNeo/ibkr-desktop-fix/actions/workflows/ci.yml/badge.svg)](https://github.com/AotianNeo/ibkr-desktop-fix/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Zero-dependency, zero-disk-modification Java Agent (java.lang.instrument) and ntws.vmoptions tuner for **IBKR Desktop (ntws)** that drops logged-in idle CPU usage from **65%–80% down to under 3%** and eliminates the non-persisted translation disclaimer modal that blocks every launch on non-English locales such as Simplified Chinese (zh_CN).

All bytecode transformations happen strictly in memory at class-load time. Official IBKR JAR files under lib/ remain 100% untouched on disk (preserving code signatures and checksums), and all ntws.vmoptions flags are placed below IBKR's keep-on-update marker so the configuration survives IBKR Desktop client updates automatically.

## Performance

Measured on Apple Silicon macOS with IBKR Desktop 0016.6 (Build 3.6h, bundled Temurin JRE 25.0.2+10), logged into a live account on the Quote / TradingView chart view with zero mouse or keyboard input:

| Metric / Process | Stock IBKR Desktop | Phase 1 (ZGC + CRITICAL logs + QWebChannel loop fix) | Full ibkr-desktop-fix (v1.0.1) |
|--------|--------|--------|--------|
| **JavaApplicationStub** (JVM + Qt Quick + CrBrowserMain) | 53.2% – 65.0% | 37.8% – 53.2% (main-thread 14.4% → 3.3%) | **0.7% – 2.1%** |
| **Active QtWebEngineProcess** (TradingViewChartWebapp) | 10.4% – 12.5% | 10.4% – 12.5% | **0.1% – 0.8%** |
| **Background QtWebEngineProcess** (6–14 hidden webviews) | 3.5% – 12.0% | 0.0% | **0.0%** |
| **Total IBKR Desktop CPU** (all processes combined) | **67% – 89%** | **48% – 65%** | **0.8% – 2.9%** |
| macOS WindowServer compositing load | 37.4% | 37.4% | **25.0%** (desktop baseline) |
| Non-English (zh_CN) startup translation modal | Pops up on every launch | Pops up on every launch | **Eliminated** |

## Quick Start

### Automatic Installation (macOS / Linux / Windows)

Requires only **Python 3.8+**. No external JDK download is needed: when javac is absent, the installer automatically uses its built-in JVMS §4.10.1-verified Python ClassFile v61.0 assembler and validates the transformed bytecode against your local lib/ntws4launch-*.jar before modifying ntws.vmoptions.

```bash
git clone https://github.com/AotianNeo/ibkr-desktop-fix.git
cd ibkr-desktop-fix
./install.sh
```

On Windows (PowerShell or Command Prompt):

```powershell
python install.py
```

On Windows accounts whose user profile path contains spaces (such as C:\Users\First Last), install.py automatically deploys ibkr-webchannel-fix.jar to a whitespace-free directory (C:\Jts\ibkr-desktop-fix or C:\ProgramData\ibkr-desktop-fix) to avoid path-splitting or literal-quote failures across JVM launchers and environment wrappers.

Restart IBKR Desktop after installation to activate the agent.

Installer options:

```bash
./install.sh --dry-run                 # Build and verify against your local IBKR JAR without modifying files
./install.sh --ibkr-dir "/path/to/app" # Specify a custom IBKR Desktop installation directory
./install.sh --uninstall               # Remove managed flags from ntws.vmoptions and remove the agent JAR
```

### Manual Installation (Prebuilt Release JAR)

1. Download **ibkr-webchannel-fix.jar** from the [Releases](https://github.com/AotianNeo/ibkr-desktop-fix/releases) page and place it at a path without spaces (for example ~/ntws/ibkr-webchannel-fix.jar).
2. Open ntws.vmoptions in your IBKR Desktop directory:
   - **macOS**: ~/Applications/IBKR Desktop/ntws.vmoptions (or /Applications/IBKR Desktop/ntws.vmoptions)
   - **Linux**: ~/Jts/ibkr-desktop/ntws.vmoptions
   - **Windows**: %LOCALAPPDATA%\Programs\IBKR Desktop\ntws.vmoptions
3. Ensure the following lines appear below the keep-on-update marker (replace /Users/YOUR_USERNAME with your actual home directory path, without quotes):

```
### keep on update
-XX:-UseParallelGC
-XX:+UseZGC
-Dio.qt.log-messages=CRITICAL
-javaagent:/Users/YOUR_USERNAME/ntws/ibkr-webchannel-fix.jar
```

4. Restart IBKR Desktop. After launch, ~/ntws/launcher.log prints two confirmation lines indicating that LocaleResourcesMgr, LanguageSettings, and QWebAppEngineView were patched in memory.

## How It Works

Profiling a live logged-in IBKR Desktop session (macOS sample profiler, thread-level CPU counters, Chromium SimpleCache inspection, and ClassFile disassembly of ntws4launch-*.jar and ntwslaunch-*.jar) exposed four independent defects that compound at runtime:

- **20 Hz QWebChannel Property Feedback Loop (ibkr/webview/QWebAppEngineView)**: When initializing every embedded QQuickWebEngineView, QWebAppEngineView injects a QWebChannel bootstrap script that registers a signal callback on window.\$app.optionChanged and immediately reassigns window.\$app.option = o inside that callback. In Qt's qwebchannel.js, QObject.unwrapProperties() and handlePropertyUpdate() have already updated object.\_\_propertyCache\_\_ before emitting optionChanged. Reassigning window.\$app.option invokes the ES5 defineProperty setter on window.\$app, which dispatches QWebChannelMessageTypes.setProperty (type 2) back across Chromium Mojo IPC (IPC::ChannelMojo) → Qt6WebChannel C++ → QtJambi JNI into the Java main thread across 7 to 15 concurrent QtWebEngineProcess workers (~20 Hz per view, ~300 cross-process IPC + JNI round-trips per second at idle). WebChannelFixAgent strips the redundant setter reassignment from the QWebAppEngineView constant pool at class-load time while preserving qwebchannel.js's native property cache synchronization, dropping the Java main-thread idle load from 14.4% to 3.3%.

- **60–120 Hz Non-Composited Infinite CSS Animation & Metal Compositing Storm (TradingViewChartWebapp)**: Even after stopping the QWebChannel feedback loop, viewing a quote page with an embedded TradingView chart kept JavaApplicationStub at 37.8%–53.2% CPU (CrBrowserMain at 12.6%, QSGRenderThread at 11.1%, CVDisplayLink at 3.7%) and TradingViewChartWebapp at 10.4%–12.5% CPU with zero user input. Inspecting the live stylesheets in Chromium SimpleCache (global.css?tradesCacheBurst=1 and TradingView bundles) pinpointed two causes:
  1. **Always-on non-composited keyframes in the chart toolbar**: The Auto-Save and Instant-Trading pill switches (.toggle-pill-label::after and #instant-trading-switch-label::after) run toggle-border-shift (3s infinite) animating **background-position**, while the Trading button notification badge runs canvas-settings-notification-pulse (2s infinite) animating **box-shadow**, alongside continuous status blink/pulse badges. Unlike transform or opacity, background-position and box-shadow cannot run on Chromium's GPU compositor fast path: on every 60–120 Hz VSync tick, Blink triggers main-thread paint invalidation, cc::LayerTreeHost rasterizes dirty tiles and sends a Viz CompositorFrame over Mojo IPC to CrBrowserMain, QQuickWebEngineView uploads textures on QSGRenderThread, and CAMetalLayer wakes macOS WindowServer.
  2. **Hidden off-tab WebViews never become document.hidden**: In IBKR Desktop's QML tab stack, WebPanel.onVisibleChanged() calls QWebAppEngineView.sendSuspendResume(boolean, Map), which sends an application-level JSON message over window.\$app without hiding the underlying Chromium RenderWidgetHost. Off-tab WebApps remain in document.visibilityState === "visible" and continue firing 60–120 Hz requestAnimationFrame callbacks and CSS animations in the background.
  3. **In-memory remediation**: WebChannelFixAgent extends QWebAppEngineView.filterWheelEventsScript() (already registered by IBKR at DocumentReady in MainWorld with runsOnSubFrames = true) to inject an idempotent style sheet into every WebApp document and same-origin child iframe (including TradingView's chart iframe), neutralizing persistent decorative infinite animations while keeping their static pill styling and transient loading spinners intact. In addition, it prepends a 28-byte JVMS §4.10.1-compliant bytecode prologue (with exact StackMapTable delta shifting and exception-table relocation) onto QWebAppEngineView.sendSuspendResume(boolean, Map) to pause animations and throttle hidden-tab requestAnimationFrame to 250 ms (4 FPS) using a disjoint timer handle namespace (1073741824 + setTimeout) without freezing V8, keeping QWebChannel (window.\$app) and WebAppWsBridge (window.\$ws) live.

- **Full-Verbosity Cross-JNI Qt Logging & Legacy ParallelGC (ntws.vmoptions)**: Stock IBKR Desktop ships with -Dio.qt.log-messages=ALL and -XX:+UseParallelGC (plus an ignored -XX:ConcGCThreads=6 flag) on its bundled Temurin JRE 25. Routing all Qt DEBUG and INFO messages across JNI into Java logging during market data updates wastes CPU cycles, while ParallelGC introduces stop-the-world pause spikes on a multi-gigabyte GUI heap. Overriding these below the keep-on-update marker with -XX:-UseParallelGC, -XX:+UseZGC, and -Dio.qt.log-messages=CRITICAL switches Java 25 to Generational ZGC and silences non-critical cross-JNI Qt log traffic.

- **Non-Persisted Startup Translation Disclaimer Modal (LocaleResourcesMgr & LanguageSettings)**: On Simplified Chinese (zh_CN) and other non-English locales, IBKR Desktop displays a modal disclaimer dialog (Confirm_IB_translation) on every startup even after clicking OK. Disassembling LanguageSettings.class and LocaleResourcesMgr\$ConfirmTranslationDialog.class inside ntwslaunch-*.jar reveals two upstream bugs: ConfirmTranslationDialog.onOk() closes the window without ever calling LanguageSettings.addConfirmedTranslations(lang), and LanguageSettings.m_confirmedTranslations is an in-memory HashSet with no XML serialization field. WebChannelFixAgent rewrites LocaleResourcesMgr.confirmTranslationLater(), confirmTranslationIfNecessary(...), and confirmTranslation(...) to 1-byte void returns and rewrites LanguageSettings.isTranslationConfirmed(String) to return true.

## Common ntws.vmoptions Pitfalls & Myths

Several circulating tuning guides for IBKR Desktop recommend JVM options that are either dead no-ops, crash startup immediately, or increase CPU load:

- **Setting -Dio.qt.debug=release is a dead no-op (neither lowers CPU nor crashes startup)**: Despite its name, io.qt.debug is not a logging verbosity switch—that role belongs to -Dio.qt.log-messages=CRITICAL. In QtJambi 6.8.8, io.qt.debug is only consulted when probing loose native JAR filenames on disk. Because IBKR Desktop already places all 13 qtjambi-*-native-*.jar archives on the JVM ClassPath and every bundled META-INF/qtjambi-deployment.xml declares configuration="release", io.qt.internal.LibraryUtility.<clinit> sets dontSearchDeploymentSpec = true, skips the io.qt.debug filename branch entirely, and unconditionally overwrites LibraryUtility.configuration with Configuration.Release from the loaded LibraryBundle (which is also why IBKR's own stale -Dio.qt.debug=debug default in Info.plist does not trigger an UnsatisfiedLinkError). Overriding -Dio.qt.debug=release in ntws.vmoptions does nothing at runtime.
- **Putting -DQTWEBENGINE_CHROMIUM_FLAGS=--disable-background-timer-throttling --disable-renderer-backgrounding --num-raster-threads=2 in ntws.vmoptions is a dead no-op (and counterproductive if exported in the OS environment)**: First, install4j's launcher (-[Launcher readOptionsFile:options:classPath:] in JavaApplicationStub) parses ntws.vmoptions strictly line-by-line on CR/LF rather than splitting on spaces, passing the entire line as a single JavaVMOption to JNI_CreateJavaVM—which silently defines an unused Java system property instead of crashing startup. Second, zero classes in IBKR Desktop or QtJambi ever read System.getProperty("QTWEBENGINE_CHROMIUM_FLAGS"); QtWebEngineCore's WebEngineContext reads QTWEBENGINE_CHROMIUM_FLAGS exclusively from the OS process environment via qEnvironmentVariable(). Third, even if exported as a real OS environment variable, --disable-background-timer-throttling and --disable-renderer-backgrounding work in the exact opposite direction by forbidding Blink and RenderProcessHostImpl from throttling hidden background pages and renderer QoS, while --num-raster-threads=2 merely shrinks QtWebEngine's default 4-thread CompositorTileWorker pool without stopping 60–120 Hz VSync paint invalidation.

## Architecture & Verification Suite

```
ibkr-desktop-fix/
├── src/ibkr/fix/WebChannelFixAgent.java  — Zero-dependency Java 17+ ClassFileTransformer agent
├── scripts/build_jar.py                  — Dual-backend builder (javac --release 17 + zero-JDK Python ClassFile v61.0 assembler)
├── tests/verify_agent.py                 — JVMS §4.10.1 ClassFile/StackMapTable verifier + MiniJVM + Node V8 DOM/rAF test
├── tests/JvmSmokeTest.java               — HotSpot (-Xverify:all) native C++ bytecode verification & execution test
├── install.py                            — Cross-platform atomic installer & uninstaller (macOS / Linux / Windows)
├── install.sh                            — POSIX shell wrapper for install.py
└── .github/workflows/ci.yml              — Matrix CI testing both build backends on Temurin JDK 17, 21 & 25
```

Run the build and verification suite locally:

```bash
python3 scripts/build_jar.py
python3 tests/verify_agent.py
```

Verify transformations directly against an installed IBKR Desktop ntws4launch JAR:

```bash
python3 tests/verify_agent.py --ibkr-jar "$HOME/Applications/IBKR Desktop/lib/"ntws4launch-*.jar
```

## License

MIT License. See [LICENSE](LICENSE).
