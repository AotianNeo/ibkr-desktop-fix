# ibkr-desktop-fix

[![CI](https://github.com/AotianNeo/ibkr-desktop-fix/actions/workflows/ci.yml/badge.svg)](https://github.com/AotianNeo/ibkr-desktop-fix/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

`ibkr-desktop-fix` is a zero-dependency, zero-disk-modification Java Agent (`java.lang.instrument`) and `ntws.vmoptions` tuner for **IBKR Desktop (`ntws`)** that drops idle logged-in CPU usage from **~65%–80% down to < 3%** and eliminates the non-persisted translation disclaimer modal that blocks every startup on non-English locales (such as `zh_CN`).

All bytecode transformations happen strictly **in memory at class-load time**. Official IBKR JAR files under `lib/` remain 100% untouched on disk (preserving code signatures and checksums), and all `ntws.vmoptions` flags are placed below IBKR's `### keep on update` marker so the fix automatically survives IBKR Desktop client updates.

---

## Measured Benchmarks

Measured on Apple Silicon macOS with **IBKR Desktop `0016.6` (Build `3.6h`, bundled Temurin JRE `25.0.2+10`)**, logged into a live account on the Quote / TradingView chart view with zero mouse or keyboard input:

| Metric / Process | Stock IBKR Desktop | Phase 1 (`ZGC` + `CRITICAL` logs + `QWebChannel` loop fix) | Full `ibkr-desktop-fix` (`v1.0.0`) |
| --- | --- | --- | --- |
| `JavaApplicationStub` (main JVM + Qt Quick + `CrBrowserMain`) | `53.2% – 65.0%` | `37.8% – 53.2%` (`main-thread` `14.4% -> 3.3%`) | **`2.1%`** |
| Active `QtWebEngineProcess` (`TradingViewChartWebapp`) | `10.4% – 12.5%` | `10.4% – 12.5%` | **`0.8%`** |
| Background `QtWebEngineProcess` workers (6–14 hidden webviews) | `3.5% – 12.0%` | `0.0%` | **`0.0%`** |
| **Total IBKR Desktop CPU (all processes combined)** | **`~67% – 89%`** | **`~48% – 65%`** | **`2.9%`** |
| macOS `WindowServer` compositing load | `37.4%` | `37.4%` | **`25.0%`** (desktop baseline) |
| Non-English (`zh_CN`) startup translation disclaimer modal | Pops up on every launch | Pops up on every launch | **Eliminated** |

---

## Usage

### Automatic Installation (macOS / Linux / Windows)

Requires only **Python 3.8+** (no external JDK download required — if `javac` is absent, the installer automatically uses its built-in JVMS §4.10.1-verified Python `ClassFile v61.0` assembler and validates the resulting bytecode against your local `lib/ntws4launch-*.jar` before touching `ntws.vmoptions`):

```bash
git clone https://github.com/AotianNeo/ibkr-desktop-fix.git
cd ibkr-desktop-fix
./install.sh
```

On Windows (PowerShell or Command Prompt):

```powershell
python install.py
```

Then **restart IBKR Desktop** (`Cmd+Q` / `Alt+F4` and relaunch).

#### Useful Installer Flags

```bash
./install.sh --dry-run                 # Build & verify against your local IBKR JAR without modifying files
./install.sh --ibkr-dir "/path/to/app" # Specify a custom IBKR Desktop directory
./install.sh --uninstall               # Remove managed flags from ntws.vmoptions and remove the agent JAR
```

### Manual Installation (Using Prebuilt Release JAR)

1. Download `ibkr-webchannel-fix.jar` from the [Releases](https://github.com/AotianNeo/ibkr-desktop-fix/releases) page and place it at a path without spaces (for example `~/ntws/ibkr-webchannel-fix.jar`).
2. Open your `ntws.vmoptions` file:
   - **macOS**: `~/Applications/IBKR Desktop/ntws.vmoptions` (or `/Applications/IBKR Desktop/ntws.vmoptions`)
   - **Linux**: `~/Jts/ibkr-desktop/ntws.vmoptions`
   - **Windows**: `%LOCALAPPDATA%\Programs\IBKR Desktop\ntws.vmoptions`
3. Ensure the following lines appear **below** `### keep on update` (replace `/Users/YOUR_USERNAME` with your actual home directory path, with **no quotes**):

```text
### keep on update
-XX:-UseParallelGC
-XX:+UseZGC
-Dio.qt.log-messages=CRITICAL
-javaagent:/Users/YOUR_USERNAME/ntws/ibkr-webchannel-fix.jar
```

4. Restart IBKR Desktop.

To verify the agent is active, inspect `~/ntws/launcher.log` after startup:

```text
[ibkr-webchannel-fix] Suppressed zh_CN startup translation confirmation popup
[ibkr-webchannel-fix] Patched QWebAppEngineView (optionLoop + idleCssRafFix + hiddenTabSuspend)
```

---

## How It Works (First-Principles Root-Cause Analysis)

Profiling a live logged-in IBKR Desktop session (`sample`, thread-level CPU counters, Chromium `SimpleCache` inspection, and `ClassFile` disassembly of `ntws4launch-*.jar` and `ntwslaunch-*.jar`) revealed four independent defects that compound at runtime:

### 1. 20 Hz `QWebChannel` Property Feedback Loop (`ibkr/webview/QWebAppEngineView`)

When initializing every embedded `QQuickWebEngineView`, `QWebAppEngineView` injects a `QWebChannel` bootstrap script containing:

```javascript
window.$app.optionChanged.connect(function(o){ if(window.$app) { window.$app.option = o; } } );
```

In Qt's `qwebchannel.js`, before emitting `optionChanged`, `QObject.unwrapProperties()` / `handlePropertyUpdate()` **has already updated** `object.__propertyCache__[propertyIndex]`. Reassigning `window.$app.option = o` inside the signal listener invokes the ES5 `defineProperty` setter on `window.$app`, which immediately dispatches `QWebChannelMessageTypes.setProperty` (`type: 2`) back over Chromium Mojo IPC (`IPC::ChannelMojo`) -> `Qt6WebChannel` C++ -> QtJambi JNI into the Java main thread across **7 to 15 concurrent `QtWebEngineProcess` instances** (~20 Hz per view, ~300 cross-process IPC + JNI round-trips per second while completely idle).

- **Fix**: `WebChannelFixAgent` rewrites the `CONSTANT_Utf8` bootstrap script in `QWebAppEngineView` at class-load time to strip the redundant `window.$app.option = o` setter invocation while keeping `qwebchannel.js`'s native `__propertyCache__` synchronization intact. This drops the Java `main-thread` idle load from `14.4%` to `3.3%` and zeroes out idle background WebEngine processes.

### 2. 60–120 Hz Non-Composited Infinite CSS Animation & Metal Compositing Storm (`TradingViewChartWebapp`)

Even after eliminating the `QWebChannel` feedback loop, opening a quote page with an embedded TradingView chart kept `JavaApplicationStub` at `37.8%–53.2%` CPU (`CrBrowserMain` at `12.6%`, `QSGRenderThread` at `11.1%`, `CVDisplayLink` at `3.7%`) and `TradingViewChartWebapp` (`QtWebEngineProcess`) at `10.4%–12.5%` CPU with zero mouse activity.

Inspecting the live stylesheets inside `~/ntws/webengine_cache/Cache_Data/` (`global.css?tradesCacheBurst=1` and TradingView bundles) pinpointed the exact culprit:

1. **Always-on non-composited `@keyframes` in the chart toolbar**:
   - `.toggle-pill-label::after` and `#instant-trading-switch-label::after` (`#autoSave-switch` and `#instant-trading-switch`) run `animation: toggle-border-shift 3s ease infinite`, which continuously mutates **`background-position`** (`0% 50% -> 100% 50% -> 0% 50%`).
   - `:root.show-trading-button-notice button[data-name="trading"] > :first-child::after` runs `canvas-settings-notification-pulse 2s infinite`, continuously mutating **`box-shadow`**, alongside infinite status badge blink/pulse keyframes (`blinking-*`, `animated-TJ9ObuLF`, `top-level-notify__pulse-dot`, `gen-ai-icon__new`).
   - Unlike `transform` or `opacity`, `background-position` and `box-shadow` **cannot be GPU-composited on the fast path** in Chromium. Every VSync tick (`60–120 Hz` on ProMotion displays), Blink runs main-thread paint invalidation, `cc::LayerTreeHost` rasterizes dirty tiles and submits a new `Viz` `CompositorFrame` over Mojo IPC to `CrBrowserMain` inside `JavaApplicationStub`, `QQuickWebEngineView` uploads textures on `QSGRenderThread`, and `CAMetalLayer` wakes macOS `WindowServer`.
2. **Hidden off-tab WebViews never transition to `document.hidden = true`**:
   - In IBKR Desktop's QML tab stack, `WebPanel.onVisibleChanged()` calls `QWebAppEngineView.sendSuspendResume(boolean, Map)`, which sends an app-level JSON notification over `$app` rather than hiding the underlying Chromium `RenderWidgetHost`. Consequently, off-tab WebApps retain `document.visibilityState === "visible"` and keep firing `60–120 Hz` `requestAnimationFrame` loops and CSS animations in the background.

- **Fix**:
  - `WebChannelFixAgent` extends `QWebAppEngineView.filterWheelEventsScript()` (which IBKR already registers as a `QWebEngineScript` at `DocumentReady` in `MainWorld` with `runsOnSubFrames = true`) to inject an idempotent `<style id="ibkr-cpu-fix">` sheet into every WebApp document and same-origin child `<iframe>` (including TradingView's `L.chart._iFrame`). This disables the persistent decorative infinite animations while preserving their static gradient/pill appearance and leaving transient loading spinners untouched.
  - `WebChannelFixAgent` prepends a 28-byte bytecode prologue (with exact `StackMapTable` frame delta shifting and exception-table relocation) onto `QWebAppEngineView.sendSuspendResume(boolean, Map)`. Whenever a WebPanel is hidden (`suspend && !isVisible()`), it sets `data-ibkr-hidden="1"` across the document and child iframes (pausing all CSS animations/transitions) and throttles `window.requestAnimationFrame` to `250 ms` (4 FPS) using a disjoint timer handle namespace (`1073741824 + setTimeout(...)`) so `cancelAnimationFrame` never collides across visibility transitions—without freezing V8, keeping `QWebChannel` (`window.$app`) and `WebAppWsBridge` (`window.$ws`) live.

### 3. Full-Verbosity Cross-JNI Qt Logging & Legacy ParallelGC (`ntws.vmoptions`)

Stock IBKR Desktop ships with `-Dio.qt.log-messages=ALL` and `-XX:+UseParallelGC` (plus an invalid `-XX:ConcGCThreads=6` flag ignored by ParallelGC) on its bundled Temurin JRE 25. During active market hours, routing all Qt `DEBUG`/`INFO` messages across JNI into Java logging wastes CPU cycles, while `UseParallelGC` introduces stop-the-world pause spikes on a 4 GB GUI heap.

- **Fix**: Appending `-XX:-UseParallelGC`, `-XX:+UseZGC`, and `-Dio.qt.log-messages=CRITICAL` below `### keep on update` switches Java 25 to Generational ZGC (sub-millisecond pauses) and silences non-critical cross-JNI Qt log traffic.

### 4. Non-Persisted Startup Translation Disclaimer Modal (`LocaleResourcesMgr` & `LanguageSettings`)

When running IBKR Desktop in Simplified Chinese (`zh_CN`) or other non-English locales, a modal warning (`Confirm_IB_translation`) blocks startup on every single launch even after clicking **OK**:

- At startup, `LocaleResourcesMgr.confirmTranslationLater()` -> `confirmTranslationIfNecessary(...)` checks `LanguageSettings.isTranslationConfirmed(String)`.
- Disassembling `LanguageSettings.class` and `LocaleResourcesMgr$ConfirmTranslationDialog.class` inside `ntwslaunch-*.jar` shows two upstream bugs:
  1. `ConfirmTranslationDialog.onOk()` only closes the dialog and **never invokes** `LanguageSettings.addConfirmedTranslations(lang)`.
  2. `LanguageSettings.m_confirmedTranslations` is an in-memory `HashSet` with **no XML serialization tag** in `LanguageSettings`—even if populated, it is lost on exit.

- **Fix**: `WebChannelFixAgent` rewrites `LocaleResourcesMgr.confirmTranslationLater()`, `confirmTranslationIfNecessary(...)`, and `confirmTranslation(...)` to 1-byte `return` (`0xB1`), and rewrites `LanguageSettings.isTranslationConfirmed(String)` to `iconst_1; ireturn` (`0x04 0xAC`).

---

## Common `ntws.vmoptions` Pitfalls (Do NOT Add These)

Several circulating LLM-generated tuning guides for IBKR Desktop recommend flags that either **crash IBKR Desktop on launch** or **increase CPU usage**:

1. **NEVER change `-Dio.qt.debug=debug` to `-Dio.qt.debug=release`**:
   In QtJambi's native library loader, `io.qt.debug` is **not** a logging verbosity switch—it selects whether QtJambi loads `debug`-suffixed or `release`-suffixed C++ native libraries from the bundle. Because IBKR Desktop ships `debug`-named QtJambi libraries, setting `-Dio.qt.debug=release` immediately crashes startup with `UnsatisfiedLinkError`.
2. **NEVER add `-DQTWEBENGINE_CHROMIUM_FLAGS=--disable-background-timer-throttling --disable-renderer-backgrounding ...` to `ntws.vmoptions`**:
   - **Wrong layer**: `QTWEBENGINE_CHROMIUM_FLAGS` is an OS environment variable read by Qt C++ via `qgetenv()`, not a Java `-D` system property; Chromium ignores `-DQTWEBENGINE_CHROMIUM_FLAGS`.
   - **Breaks JVM startup**: Unquoted space-separated tokens such as `--disable-renderer-backgrounding` on a `.vmoptions` line are passed as unknown JVM options and abort `JNI_CreateJavaVM`.
   - **Backwards semantics**: Even when exported as an OS environment variable, `--disable-background-timer-throttling` and `--disable-renderer-backgrounding` **forbid** Chromium from throttling hidden background renderers, forcing hidden tabs to burn *more* CPU.

---

## Repository Structure & Verification Suite

```text
ibkr-desktop-fix/
├── src/ibkr/fix/WebChannelFixAgent.java  # Zero-dependency Java 17+ ClassFileTransformer agent
├── scripts/build_jar.py                  # Dual-backend builder (JDK javac --release 17 + zero-JDK Python ClassFile v61.0 assembler)
├── tests/verify_agent.py                 # JVMS §4.10.1 ClassFile/StackMapTable verifier + MiniJVM interpreter + Node V8 DOM/rAF test
├── tests/JvmSmokeTest.java               # HotSpot (-Xverify:all) native C++ bytecode verification & execution smoke test
├── install.py                            # Cross-platform atomic installer & uninstaller (macOS / Linux / Windows)
├── install.sh                            # POSIX shell entrypoint for install.py
└── .github/workflows/ci.yml              # Matrix CI validating both build backends under HotSpot -Xverify:all (JDK 17, 21 & 25)
```

To build and run the full verification suite locally:

```bash
python3 scripts/build_jar.py
python3 tests/verify_agent.py
```

If IBKR Desktop is installed locally, you can also verify the agent's bytecode transformations directly against your installed `ntws4launch-*.jar`:

```bash
python3 tests/verify_agent.py --ibkr-jar "$HOME/Applications/IBKR Desktop/lib/"ntws4launch-*.jar
```

---

## License

Released under the [MIT License](LICENSE).
