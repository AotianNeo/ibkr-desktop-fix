package ibkr.fix;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.nio.charset.StandardCharsets;
import java.security.ProtectionDomain;

/**
 * Zero-disk-modification Java Agent for IBKR Desktop (ntws 0016.6 / Build 3.6h+).
 *
 * Performs three in-memory ClassFile v61.0 transformations at class-load time:
 *
 * 1. QWebChannel 20 Hz IPC feedback loop elimination (ibkr/webview/QWebAppEngineView):
 *    Strips redundant `window.$app.option = o` reassignment inside `optionChanged.connect(...)`
 *    (Qt's qwebchannel.js already updates `object.__propertyCache__` in `handlePropertyUpdate`).
 *
 * 2. WebEngine 60-120 Hz idle CSS/GPU render storm elimination (ibkr/webview/QWebAppEngineView):
 *    - Extends `filterWheelEventsScript()` (MainWorld, DocumentReady, runsOnSubFrames=true)
 *      to inject `<style id="ibkr-cpu-fix">` into every Webapp document and same-origin child
 *      `<iframe>` (including TradingView's `L.chart._iFrame`), disabling persistent decorative
 *      non-composited infinite CSS `@keyframes` (`toggle-border-shift` on `#autoSave-switch`
 *      and `#instant-trading-switch`, `canvas-settings-notification-pulse`, status blinking/pulse
 *      badges) while preserving their static visual styling and keeping transient loading
 *      spinners intact.
 *    - Instruments `QWebAppEngineView.sendSuspendResume(boolean, Map)` (invoked by
 *      `WebPanel.onVisibleChanged()`) to toggle `data-ibkr-hidden="1"` and throttle hidden-tab
 *      `requestAnimationFrame` to 250 ms (using a disjoint timer handle namespace) without
 *      freezing V8, keeping `QWebChannel` (`window.$app`) and `WebAppWsBridge` (`window.$ws`) live.
 *
 * 3. Non-persisted startup translation disclaimer modal suppression:
 *    Short-circuits `LocaleResourcesMgr.confirmTranslationLater()`,
 *    `LocaleResourcesMgr.confirmTranslationIfNecessary(...)`,
 *    `LocaleResourcesMgr.confirmTranslation(...)`, and returns `true` from
 *    `LanguageSettings.isTranslationConfirmed(String)` so `Confirm_IB_translation` does not
 *    re-open on every launch when running non-English locales (e.g. `zh_CN`).
 */
public final class WebChannelFixAgent implements ClassFileTransformer {
    private static final String CLS_WEBAPP = "ibkr/webview/QWebAppEngineView";
    private static final String CLS_LOCALE = "feature/configure/language/LocaleResourcesMgr";
    private static final String CLS_LANG = "jsetting/LanguageSettings";

    private static final String BAD_OPT_JS =
        "window.$app.optionChanged.connect(function(o){ if(window.$app) { window.$app.option = o; } } );";
    private static final String FIX_OPT_JS =
        "/* ibkr-webchannel-fix: qwebchannel.js already updates __propertyCache__ */";

    private static final String WHEEL_PREFIX =
        "document.body.addEventListener( 'wheel'";

    private static final String USER_SCRIPT_JS =
        "(function(){\n"
        + "if(window.__ibkrCpuFixInstalled)return;\n"
        + "window.__ibkrCpuFixInstalled=true;\n"
        + "var root=document.body||document.documentElement;\n"
        + "if(root){root.addEventListener('wheel',function(e){if(e.ctrlKey)e.preventDefault();},{capture:true,passive:false});}\n"
        + "var CSS='.toggle-pill-label::after,.toggle-pill-input:checked+.toggle-pill-label::after,#instant-trading-switch-label::after,"
        + ":root.show-trading-button-notice button[data-name=\"trading\"]>:first-child::after,"
        + "[class*=\"blinking-\"]::after,[class*=\"blinking-\"]:after,[class*=\"animated-TJ9ObuLF\"],"
        + ".gen-ai-icon__new,.top-level-notify__pulse-dot,.player__muted,.intimpact-settings__ackTip span svg"
        + "{animation:none !important;-webkit-animation:none !important;animation-play-state:paused !important;}"
        + "html[data-ibkr-hidden=\"1\"],html[data-ibkr-hidden=\"1\"] *,html[data-ibkr-hidden=\"1\"] *::before,html[data-ibkr-hidden=\"1\"] *::after"
        + "{animation-play-state:paused !important;-webkit-animation-play-state:paused !important;transition:none !important;}';\n"
        + "function ensureStyle(doc){\n"
        + "  try{\n"
        + "    if(!doc||doc.getElementById('ibkr-cpu-fix'))return;\n"
        + "    var s=doc.createElement('style');s.id='ibkr-cpu-fix';s.textContent=CSS;\n"
        + "    var h=doc.head||doc.documentElement;if(h)h.appendChild(s);\n"
        + "  }catch(e){}\n"
        + "}\n"
        + "ensureStyle(document);\n"
        + "var origRAF=window.requestAnimationFrame?window.requestAnimationFrame.bind(window):null;\n"
        + "var origCAF=window.cancelAnimationFrame?window.cancelAnimationFrame.bind(window):null;\n"
        + "var RAF_OFF=1073741824;\n"
        + "function wrappedCAF(id){\n"
        + "  if(id>=RAF_OFF)window.clearTimeout(id-RAF_OFF);\n"
        + "  else if(origCAF)origCAF(id);\n"
        + "}\n"
        + "if(origCAF)window.cancelAnimationFrame=wrappedCAF;\n"
        + "var hostHidden=!!window.__ibkrHostHidden;\n"
        + "try{if(!hostHidden&&window.parent&&window.parent!==window&&window.parent.__ibkrHostHidden)hostHidden=true;}catch(e){}\n"
        + "var hidden=hostHidden||!!document.hidden;\n"
        + "function patchFrame(f){\n"
        + "  try{\n"
        + "    if(!f)return;\n"
        + "    var cw=f.contentWindow;\n"
        + "    var cd=f.contentDocument||(cw&&cw.document);\n"
        + "    if(cd)ensureStyle(cd);\n"
        + "    if(cw&&cw!==window){\n"
        + "      cw.__ibkrHostHidden=!!window.__ibkrHostHidden;\n"
        + "      if(cw.__ibkrSetHidden)cw.__ibkrSetHidden(hidden);\n"
        + "      else if(cd&&cd.documentElement){\n"
        + "        if(hidden)cd.documentElement.setAttribute('data-ibkr-hidden','1');\n"
        + "        else cd.documentElement.removeAttribute('data-ibkr-hidden');\n"
        + "      }\n"
        + "    }\n"
        + "  }catch(e){}\n"
        + "}\n"
        + "function applyHidden(h){\n"
        + "  hidden=!!h;\n"
        + "  ensureStyle(document);\n"
        + "  var de=document.documentElement;\n"
        + "  if(de){if(hidden)de.setAttribute('data-ibkr-hidden','1');else de.removeAttribute('data-ibkr-hidden');}\n"
        + "  if(origRAF){\n"
        + "    if(hidden){\n"
        + "      window.requestAnimationFrame=function(cb){return RAF_OFF+window.setTimeout(function(){cb(performance.now());},250);};\n"
        + "    }else{\n"
        + "      window.requestAnimationFrame=origRAF;\n"
        + "    }\n"
        + "  }\n"
        + "  var fs=document.getElementsByTagName('iframe');\n"
        + "  for(var i=0;i<fs.length;i++)patchFrame(fs[i]);\n"
        + "}\n"
        + "window.__ibkrSetHidden=function(h){window.__ibkrHostHidden=!!h;applyHidden(!!h||!!document.hidden);};\n"
        + "window.addEventListener('load',function(e){\n"
        + "  var t=e&&e.target;\n"
        + "  if(t&&t.tagName==='IFRAME')patchFrame(t);\n"
        + "  else if(t===document)applyHidden(hidden);\n"
        + "},true);\n"
        + "document.addEventListener('visibilitychange',function(){applyHidden(!!window.__ibkrHostHidden||!!document.hidden);});\n"
        + "applyHidden(hidden);\n"
        + "})();\n";

    private static final String JS_HIDE =
        "window.__ibkrHostHidden=true;window.__ibkrSetHidden&&window.__ibkrSetHidden(true);";
    private static final String JS_SHOW =
        "window.__ibkrHostHidden=false;window.__ibkrSetHidden&&window.__ibkrSetHidden(false);";

    public WebChannelFixAgent() {}

    public static void premain(String agentArgs, Instrumentation inst) {
        inst.addTransformer(new WebChannelFixAgent());
    }

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (className == null || classfileBuffer == null) {
            return null;
        }
        try {
            if (CLS_WEBAPP.equals(className)) {
                return patchWebApp(classfileBuffer);
            }
            if (CLS_LOCALE.equals(className)) {
                return patchTranslationMethods(classfileBuffer, false);
            }
            if (CLS_LANG.equals(className)) {
                return patchTranslationMethods(classfileBuffer, true);
            }
        } catch (Throwable t) {
            System.err.println("[ibkr-webchannel-fix] Failed transforming " + className + ": " + t);
        }
        return null;
    }

    private static int u2(byte[] b, int off) {
        return ((b[off] & 0xFF) << 8) | (b[off + 1] & 0xFF);
    }

    private static int i4(byte[] b, int off) {
        return ((b[off] & 0xFF) << 24)
            | ((b[off + 1] & 0xFF) << 16)
            | ((b[off + 2] & 0xFF) << 8)
            | (b[off + 3] & 0xFF);
    }

    private static int cpEntrySize(byte[] b, int off) {
        int tag = b[off] & 0xFF;
        switch (tag) {
            case 1:
                return 3 + u2(b, off + 1);
            case 3:
            case 4:
            case 9:
            case 10:
            case 11:
            case 12:
            case 17:
            case 18:
                return 5;
            case 5:
            case 6:
                return 9;
            case 7:
            case 8:
            case 16:
            case 19:
            case 20:
                return 3;
            case 15:
                return 4;
            default:
                throw new IllegalArgumentException("Unknown CP tag: " + tag);
        }
    }

    private static int skipMembers(byte[] b, int off) {
        int cnt = u2(b, off);
        off += 2;
        for (int i = 0; i < cnt; i++) {
            int ac = u2(b, off + 6);
            off += 8;
            for (int j = 0; j < ac; j++) {
                int al = i4(b, off + 2);
                off += 6 + al;
            }
        }
        return off;
    }

    private static int indexOfBytes(byte[] hay, int start, int len, byte[] needle) {
        int limit = start + len - needle.length;
        for (int i = start; i <= limit; i++) {
            boolean ok = true;
            for (int j = 0; j < needle.length; j++) {
                if (hay[i + j] != needle[j]) {
                    ok = false;
                    break;
                }
            }
            if (ok) {
                return i;
            }
        }
        return -1;
    }

    private static void writeUtf8Entry(DataOutputStream out, byte[] utf8Bytes) throws IOException {
        out.writeByte(1);
        out.writeShort(utf8Bytes.length);
        out.write(utf8Bytes);
    }

    private static void writeStringEntry(DataOutputStream out, int utf8Idx) throws IOException {
        out.writeByte(8);
        out.writeShort(utf8Idx);
    }

    private static int matchTranslationMethod(boolean isLangSettings, String name, String desc) {
        if (isLangSettings) {
            if ("isTranslationConfirmed".equals(name) && "(Ljava/lang/String;)Z".equals(desc)) {
                return 4;
            }
            return 0;
        }
        if ("confirmTranslationLater".equals(name) && "()V".equals(desc)) {
            return 1;
        }
        if ("confirmTranslationIfNecessary".equals(name)
                && "(Ljava/util/Locale;Ljava/lang/String;Ljava/lang/String;Z)V".equals(desc)) {
            return 2;
        }
        if ("confirmTranslation".equals(name)
                && "(Ljava/util/Locale;Ljava/lang/String;Z)V".equals(desc)) {
            return 3;
        }
        return 0;
    }

    private static void writeStubCode(DataOutputStream out, int attrNameIdx, int kind) throws IOException {
        out.writeShort(attrNameIdx);
        if (kind == 4) {
            out.writeInt(14);
            out.writeShort(1);
            out.writeShort(2);
            out.writeInt(2);
            out.writeByte(0x04); // iconst_1
            out.writeByte(0xAC); // ireturn
        } else {
            int locals = (kind == 2) ? 4 : ((kind == 3) ? 3 : 0);
            out.writeInt(13);
            out.writeShort(0);
            out.writeShort(locals);
            out.writeInt(1);
            out.writeByte(0xB1); // return
        }
        out.writeShort(0);
        out.writeShort(0);
    }

    private static byte[] patchTranslationMethods(byte[] buf, boolean isLangSettings) throws IOException {
        if (buf.length < 16 || i4(buf, 0) != 0xCAFEBABE) {
            return null;
        }
        int cpCount = u2(buf, 8);
        String[] utf8 = new String[cpCount];
        int off = 10;
        for (int i = 1; i < cpCount; i++) {
            int tag = buf[off] & 0xFF;
            int sz = cpEntrySize(buf, off);
            if (tag == 1) {
                utf8[i] = new String(buf, off + 3, sz - 3, StandardCharsets.UTF_8);
            }
            off += sz;
            if (tag == 5 || tag == 6) {
                i++;
            }
        }
        int ifCnt = u2(buf, off + 6);
        off = skipMembers(buf, off + 8 + 2 * ifCnt);

        ByteArrayOutputStream baos = new ByteArrayOutputStream(buf.length);
        DataOutputStream out = new DataOutputStream(baos);
        out.write(buf, 0, off);

        int mCnt = u2(buf, off);
        out.writeShort(mCnt);
        off += 2;

        int patched = 0;
        for (int m = 0; m < mCnt; m++) {
            int mn = u2(buf, off + 2);
            int md = u2(buf, off + 4);
            int maCnt = u2(buf, off + 6);
            out.write(buf, off, 8);
            off += 8;

            int kind = matchTranslationMethod(isLangSettings, utf8[mn], utf8[md]);
            for (int a = 0; a < maCnt; a++) {
                int an = u2(buf, off);
                int al = i4(buf, off + 2);
                if (kind != 0 && "Code".equals(utf8[an])) {
                    writeStubCode(out, an, kind);
                    patched++;
                } else {
                    out.write(buf, off, 6 + al);
                }
                off += 6 + al;
            }
        }
        out.write(buf, off, buf.length - off);
        if (patched == 0) {
            return null;
        }
        System.err.println("[ibkr-webchannel-fix] Suppressed zh_CN startup translation confirmation popup");
        return baos.toByteArray();
    }

    private static byte[] shiftStackMapTable(byte[] buf, int smtOff, int smtLen) throws IOException {
        if (smtOff < 0 || smtLen < 2 || u2(buf, smtOff) == 0) {
            return new byte[] { 0, 2, 21, 6 };
        }
        int n = u2(buf, smtOff);
        int ft = buf[smtOff + 2] & 0xFF;
        ByteArrayOutputStream s = new ByteArrayOutputStream(smtLen + 4);
        DataOutputStream ds = new DataOutputStream(s);
        if (ft == 0) {
            ds.writeShort(n + 1);
            ds.writeByte(21);
            ds.writeByte(6);
            ds.write(buf, smtOff + 3, smtLen - 3);
        } else if ((ft >= 1 && ft <= 63) || (ft >= 65 && ft <= 127)) {
            ds.writeShort(n + 2);
            ds.writeByte(21);
            ds.writeByte(6);
            ds.writeByte(ft - 1);
            ds.write(buf, smtOff + 3, smtLen - 3);
        } else {
            int d0 = u2(buf, smtOff + 3);
            if (d0 == 0 && ft == 251) {
                ds.writeShort(n + 1);
                ds.writeByte(21);
                ds.writeByte(6);
                ds.write(buf, smtOff + 5, smtLen - 5);
            } else {
                ds.writeShort(n + 2);
                ds.writeByte(21);
                ds.writeByte(6);
                ds.writeByte(ft);
                ds.writeShort(d0 - 1);
                ds.write(buf, smtOff + 5, smtLen - 5);
            }
        }
        return s.toByteArray();
    }

    private static void writePatchedSuspendCode(
            DataOutputStream out,
            int codeAttrNameIdx,
            byte[] buf,
            int cOff,
            int mrefVis,
            int mrefExec,
            int strHideIdx,
            int strShowIdx,
            int smtNameIdx,
            String[] utf8) throws IOException {
        int origMaxStack = u2(buf, cOff);
        int origMaxLocals = u2(buf, cOff + 2);
        int origCodeLen = i4(buf, cOff + 4);
        int origCodeOff = cOff + 8;
        int p = origCodeOff + origCodeLen;
        int exCnt = u2(buf, p);
        int exOff = p + 2;
        p = exOff + 8 * exCnt;
        int caCnt = u2(buf, p);
        p += 2;

        int smtOff = -1;
        int smtLen = 0;
        for (int k = 0; k < caCnt; k++) {
            int can = u2(buf, p);
            int cal = i4(buf, p + 2);
            if ("StackMapTable".equals(utf8[can])) {
                smtOff = p + 6;
                smtLen = cal;
            }
            p += 6 + cal;
        }

        byte[] newSmt = shiftStackMapTable(buf, smtOff, smtLen);
        byte[] prol = new byte[] {
            0x1b,
            (byte) 0x99, 0x00, 0x14,
            0x2a,
            (byte) 0xb6, (byte) (mrefVis >> 8), (byte) mrefVis,
            (byte) 0x9a, 0x00, 0x0d,
            0x2a,
            0x13, (byte) (strHideIdx >> 8), (byte) strHideIdx,
            (byte) 0xb6, (byte) (mrefExec >> 8), (byte) mrefExec,
            (byte) 0xa7, 0x00, 0x0a,
            0x2a,
            0x13, (byte) (strShowIdx >> 8), (byte) strShowIdx,
            (byte) 0xb6, (byte) (mrefExec >> 8), (byte) mrefExec
        };

        int newCodeLen = 28 + origCodeLen;
        int newAttrLen = 18 + newCodeLen + (8 * exCnt) + newSmt.length;

        out.writeShort(codeAttrNameIdx);
        out.writeInt(newAttrLen);
        out.writeShort(origMaxStack < 2 ? 2 : origMaxStack);
        out.writeShort(origMaxLocals);
        out.writeInt(newCodeLen);
        out.write(prol);
        out.write(buf, origCodeOff, origCodeLen);
        out.writeShort(exCnt);
        for (int e = 0; e < exCnt; e++) {
            int eo = exOff + 8 * e;
            out.writeShort(u2(buf, eo) + 28);
            out.writeShort(u2(buf, eo + 2) + 28);
            out.writeShort(u2(buf, eo + 4) + 28);
            out.writeShort(u2(buf, eo + 6));
        }
        out.writeShort(1);
        out.writeShort(smtNameIdx);
        out.writeInt(newSmt.length);
        out.write(newSmt);
    }

    private static byte[] patchWebApp(byte[] buf) throws IOException {
        if (buf.length < 16 || i4(buf, 0) != 0xCAFEBABE) {
            return null;
        }
        int cpCount = u2(buf, 8);
        int[] cpTag = new int[cpCount];
        int[] cpOff = new int[cpCount];
        int[] cpLen = new int[cpCount];
        String[] utf8 = new String[cpCount];

        int off = 10;
        for (int i = 1; i < cpCount; i++) {
            int tag = buf[off] & 0xFF;
            int sz = cpEntrySize(buf, off);
            cpTag[i] = tag;
            cpOff[i] = off;
            cpLen[i] = sz;
            if (tag == 1) {
                utf8[i] = new String(buf, off + 3, sz - 3, StandardCharsets.UTF_8);
            }
            off += sz;
            if (tag == 5 || tag == 6) {
                i++;
            }
        }
        int cpEnd = off;
        int thisClass = u2(buf, cpEnd + 2);

        int mrefIsVisible = 0;
        int mrefExecScript = 0;
        int smtUtf8Idx = 0;
        for (int i = 1; i < cpCount; i++) {
            if (cpTag[i] == 1 && "StackMapTable".equals(utf8[i])) {
                smtUtf8Idx = i;
            } else if (cpTag[i] == 10 && u2(buf, cpOff[i] + 1) == thisClass) {
                int nt = u2(buf, cpOff[i] + 3);
                if (nt > 0 && nt < cpCount && cpTag[nt] == 12) {
                    String mn = utf8[u2(buf, cpOff[nt] + 1)];
                    String md = utf8[u2(buf, cpOff[nt] + 3)];
                    if ("isVisible".equals(mn) && "()Z".equals(md)) {
                        mrefIsVisible = i;
                    } else if ("executeScript".equals(mn) && "(Ljava/lang/String;)V".equals(md)) {
                        mrefExecScript = i;
                    }
                }
            }
        }

        boolean canPatchSuspend = (mrefIsVisible != 0 && mrefExecScript != 0 && smtUtf8Idx != 0);
        int newCpCount = canPatchSuspend ? (cpCount + 4) : cpCount;

        ByteArrayOutputStream baos = new ByteArrayOutputStream(buf.length + 4096);
        DataOutputStream out = new DataOutputStream(baos);
        out.write(buf, 0, 8);
        out.writeShort(newCpCount);

        byte[] badOptBytes = BAD_OPT_JS.getBytes(StandardCharsets.UTF_8);
        byte[] fixOptBytes = FIX_OPT_JS.getBytes(StandardCharsets.UTF_8);
        byte[] wheelPrefixBytes = WHEEL_PREFIX.getBytes(StandardCharsets.UTF_8);
        byte[] userScriptBytes = USER_SCRIPT_JS.getBytes(StandardCharsets.UTF_8);

        int patchedCount = 0;
        for (int i = 1; i < cpCount; i++) {
            int tag = cpTag[i];
            int o = cpOff[i];
            int l = cpLen[i];
            if (tag == 1) {
                int payOff = o + 3;
                int payLen = l - 3;
                int hit = indexOfBytes(buf, payOff, payLen, badOptBytes);
                if (hit >= 0) {
                    int newLen = payLen - badOptBytes.length + fixOptBytes.length;
                    out.writeByte(1);
                    out.writeShort(newLen);
                    out.write(buf, payOff, hit - payOff);
                    out.write(fixOptBytes);
                    int after = hit + badOptBytes.length;
                    out.write(buf, after, (payOff + payLen) - after);
                    patchedCount++;
                } else if (payLen >= wheelPrefixBytes.length
                        && indexOfBytes(buf, payOff, wheelPrefixBytes.length, wheelPrefixBytes) == payOff) {
                    writeUtf8Entry(out, userScriptBytes);
                    patchedCount++;
                } else {
                    out.write(buf, o, l);
                }
            } else {
                out.write(buf, o, l);
            }
            if (tag == 5 || tag == 6) {
                i++;
            }
        }

        if (canPatchSuspend) {
            writeUtf8Entry(out, JS_HIDE.getBytes(StandardCharsets.UTF_8));
            writeStringEntry(out, cpCount);
            writeUtf8Entry(out, JS_SHOW.getBytes(StandardCharsets.UTF_8));
            writeStringEntry(out, cpCount + 2);
        }

        int ifCnt = u2(buf, cpEnd + 6);
        int fieldsEnd = skipMembers(buf, cpEnd + 8 + 2 * ifCnt);
        out.write(buf, cpEnd, fieldsEnd - cpEnd);
        off = fieldsEnd;

        int mCnt = u2(buf, off);
        out.writeShort(mCnt);
        off += 2;

        for (int m = 0; m < mCnt; m++) {
            int mn = u2(buf, off + 2);
            int md = u2(buf, off + 4);
            int maCnt = u2(buf, off + 6);
            out.write(buf, off, 8);
            off += 8;

            boolean isTarget = canPatchSuspend
                && "sendSuspendResume".equals(utf8[mn])
                && "(ZLjava/util/Map;)V".equals(utf8[md]);
            for (int a = 0; a < maCnt; a++) {
                int an = u2(buf, off);
                int al = i4(buf, off + 2);
                if (isTarget && "Code".equals(utf8[an])) {
                    writePatchedSuspendCode(
                        out, an, buf, off + 6,
                        mrefIsVisible, mrefExecScript,
                        cpCount + 1, cpCount + 3, smtUtf8Idx, utf8);
                    patchedCount++;
                } else {
                    out.write(buf, off, 6 + al);
                }
                off += 6 + al;
            }
        }

        out.write(buf, off, buf.length - off);
        if (patchedCount == 0) {
            return null;
        }
        System.err.println(
            "[ibkr-webchannel-fix] Patched QWebAppEngineView (optionLoop + idleCssRafFix + hiddenTabSuspend)");
        return baos.toByteArray();
    }
}
