import ibkr.fix.WebChannelFixAgent;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Map;

/**
 * End-to-end HotSpot JVM (-Xverify:all) smoke test executed in CI.
 * Loads transformed ClassFile v61.0 bytecodes via ClassLoader.defineClass to force
 * HotSpot's native C++ StackMapTable & bytecode verifier to validate and execute
 * every transformed method across all branches.
 */
public final class JvmSmokeTest extends ClassLoader {
    public static void main(String[] args) throws Exception {
        if (args.length != 3 && args.length != 4) {
            throw new AssertionError("Expected 3 or 4 target class paths");
        }
        WebChannelFixAgent agent = new WebChannelFixAgent();
        byte[] wIn = Files.readAllBytes(Path.of(args[0]));
        byte[] locIn = Files.readAllBytes(Path.of(args[1]));
        byte[] lngIn = Files.readAllBytes(Path.of(args[2]));

        byte[] wOut = agent.transform(null, "ibkr/webview/QWebAppEngineView", null, null, wIn);
        byte[] locOut = agent.transform(null, "feature/configure/language/LocaleResourcesMgr", null, null, locIn);
        byte[] lngOut = agent.transform(null, "jsetting/LanguageSettings", null, null, lngIn);

        if (wOut == null || locOut == null || lngOut == null) {
            throw new AssertionError("Expected non-null transformed bytecodes");
        }

        JvmSmokeTest loader = new JvmSmokeTest();
        Class<?> wCls = loader.loadAndVerify("ibkr.webview.QWebAppEngineView", wOut);
        Class<?> locCls = loader.loadAndVerify("feature.configure.language.LocaleResourcesMgr", locOut);
        Class<?> lngCls = loader.loadAndVerify("jsetting.LanguageSettings", lngOut);

        // 1. Verify QWebAppEngineView constant-pool rewrites & sendSuspendResume instrumentation
        Object wObj = wCls.getDeclaredConstructor().newInstance();
        String[] scripts = (String[]) wCls.getMethod("scriptsRef").invoke(wObj);
        if (scripts[0].contains("window.$app.option = o")) {
            throw new AssertionError("QWebChannel 20 Hz loop string was not stripped");
        }
        if (!scripts[1].contains("ibkr-cpu-fix")) {
            throw new AssertionError("CSS/rAF idle fix user script was not injected");
        }

        Method sendSuspend = wCls.getMethod("sendSuspendResume", boolean.class, Map.class);
        wCls.getField("visible").setBoolean(wObj, false);
        sendSuspend.invoke(wObj, true, null);
        String s1 = (String) wCls.getField("lastScript").get(wObj);
        if (s1 == null || !s1.contains("__ibkrHostHidden=true")) {
            throw new AssertionError("Expected hidden=true script when suspend=true && !visible, got: " + s1);
        }

        wCls.getField("visible").setBoolean(wObj, true);
        sendSuspend.invoke(wObj, true, null);
        String s2 = (String) wCls.getField("lastScript").get(wObj);
        if (s2 == null || !s2.contains("__ibkrHostHidden=false")) {
            throw new AssertionError("Expected hidden=false script when visible=true, got: " + s2);
        }

        wCls.getField("visible").setBoolean(wObj, false);
        sendSuspend.invoke(wObj, false, null);
        String s3 = (String) wCls.getField("lastScript").get(wObj);
        if (s3 == null || !s3.contains("__ibkrHostHidden=false")) {
            throw new AssertionError("Expected hidden=false script when suspend=false, got: " + s3);
        }

        // 2. Verify LocaleResourcesMgr popup methods are no-op stubs (unpatched mocks throw AssertionError)
        locCls.getMethod("confirmTranslationLater").invoke(null);
        Method mIfNec = locCls.getDeclaredMethod(
            "confirmTranslationIfNecessary", Locale.class, String.class, String.class, boolean.class);
        mIfNec.setAccessible(true);
        mIfNec.invoke(null, Locale.SIMPLIFIED_CHINESE, "IBKR Desktop", "zh_CN", true);

        Method mConf = locCls.getDeclaredMethod(
            "confirmTranslation", Locale.class, String.class, boolean.class);
        mConf.setAccessible(true);
        mConf.invoke(null, Locale.SIMPLIFIED_CHINESE, "IBKR Desktop", true);

        // 3. Verify LanguageSettings.isTranslationConfirmed returns true
        Object lngObj = lngCls.getDeclaredConstructor().newInstance();
        boolean confirmed = (Boolean) lngCls
            .getMethod("isTranslationConfirmed", String.class)
            .invoke(lngObj, "zh_CN");
        if (!confirmed) {
            throw new AssertionError("Expected isTranslationConfirmed to return true");
        }

        // 4. Optional: verify & execute full_frame (ft=255, d0=0 at PC=0) + non-35-byte sendSuspendResume variant
        if (args.length == 4) {
            byte[] wFullIn = Files.readAllBytes(Path.of(args[3]));
            byte[] wFullOut = agent.transform(null, "ibkr/webview/QWebAppEngineView", null, null, wFullIn);
            if (wFullOut == null) {
                throw new AssertionError("Expected non-null transformed bytecodes for full_frame variant");
            }
            JvmSmokeTest loader2 = new JvmSmokeTest();
            Class<?> wfCls = loader2.loadAndVerify("ibkr.webview.QWebAppEngineView", wFullOut);
            Object wfObj = wfCls.getDeclaredConstructor().newInstance();
            Method wfSuspend = wfCls.getMethod("sendSuspendResume", boolean.class, Map.class);

            wfCls.getField("visible").setBoolean(wfObj, false);
            wfSuspend.invoke(wfObj, true, null);
            String fs1 = (String) wfCls.getField("lastScript").get(wfObj);
            if (fs1 == null || !fs1.contains("__ibkrHostHidden=true")) {
                throw new AssertionError("Expected hidden=true on full_frame variant, got: " + fs1);
            }

            wfSuspend.invoke(wfObj, false, null);
            String fs2 = (String) wfCls.getField("lastScript").get(wfObj);
            if (fs2 == null || !fs2.contains("__ibkrHostHidden=false")) {
                throw new AssertionError("Expected hidden=false on full_frame variant, got: " + fs2);
            }
        }
        System.out.println("[JvmSmokeTest] HotSpot -Xverify:all verified and executed all transformed classes!");
    }

    private Class<?> loadAndVerify(String binaryName, byte[] bytes) {
        Class<?> c = defineClass(binaryName, bytes, 0, bytes.length);
        resolveClass(c);
        return c;
    }
}
