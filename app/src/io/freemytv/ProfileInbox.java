package io.freemytv;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Set;

/** A documented ADB-only transfer of UI preferences. No secrets, permissions, or Home state. */
public final class ProfileInbox extends ContentProvider {
    private static final Set<String> KEYS = new HashSet<>(Arrays.asList(
            "language", "theme", "temperature", "forecast", "city", "latitude", "longitude",
            "sounds", "weather", "quotes", "photos", "saverWatch", "cards", "shelf", "topButtons"));
    private static final Set<String> BOOLS = new HashSet<>(Arrays.asList("sounds", "weather", "quotes", "photos", "saverWatch"));
    private static final String BASELINE = "baseline";

    @Override public boolean onCreate() {
        // Providers can be published before Application.onCreate completes.
        if (Prefs.context() == null) Prefs.init(getContext());
        Place.init(getContext());
        return true;
    }

    private static void checkCaller() {
        int uid = Binder.getCallingUid();
        if (uid != 2000 && uid != android.os.Process.myUid()) throw new SecurityException("ADB shell only");
    }

    private SharedPreferences settings() { return getContext().getSharedPreferences("settings", Context.MODE_PRIVATE); }
    private SharedPreferences transfer() { return getContext().getSharedPreferences("profile-transfer", Context.MODE_PRIVATE); }

    private JSONObject exportProfile() throws Exception {
        Prefs.reload();
        JSONObject s = new JSONObject();
        s.put("language", Prefs.language()); s.put("theme", Prefs.theme());
        s.put("temperature", Prefs.temperature()); s.put("forecast", Prefs.forecast());
        s.put("city", Prefs.city()); s.put("latitude", Prefs.latitude()); s.put("longitude", Prefs.longitude());
        s.put("sounds", Prefs.sounds()); s.put("weather", Prefs.weather()); s.put("quotes", Prefs.quotes());
        s.put("photos", Prefs.photos()); s.put("saverWatch", Prefs.saverWatch()); s.put("cards", Prefs.cards());
        List<String> shelf = Prefs.shelf();
        s.put("shelf", new JSONArray(shelf == null ? HomeApps.shelfPackages(getContext()) : shelf));
        s.put("topButtons", new JSONArray(HomeApps.topButtonKeys()));
        JSONObject p = new JSONObject(); p.put("schema", 1); p.put("kind", "free-my-tv-ui"); p.put("settings", s);
        p.put("max_shelf", HomeApps.lowRam(getContext()) ? 8 : 64);
        return p;
    }

    private static String encoded(JSONObject p) {
        return Base64.encodeToString(p.toString().getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
    }

    /** Validate the whole object before touching preferences, including unexpected top-level fields. */
    private SharedPreferences.Editor validate(JSONObject p) throws Exception {
        if (p.optInt("schema", -1) != 1 || !"free-my-tv-ui".equals(p.optString("kind"))) throw new IllegalArgumentException();
        for (Iterator<String> i = p.keys(); i.hasNext();) {
            String key = i.next();
            if (!Arrays.asList("schema", "kind", "settings", "max_shelf").contains(key)) throw new IllegalArgumentException();
        }
        JSONObject s = p.getJSONObject("settings");
        SharedPreferences.Editor e = settings().edit();
        for (Iterator<String> i = s.keys(); i.hasNext();) {
            String k = i.next(); Object v = s.get(k);
            if (!KEYS.contains(k)) throw new IllegalArgumentException();
            if (BOOLS.contains(k)) {
                if (!(v instanceof Boolean)) throw new IllegalArgumentException();
                e.putBoolean(k, (Boolean) v);
            } else if (k.equals("cards")) {
                if (!(v instanceof Number) || ((Number) v).doubleValue() != ((Number) v).intValue()) throw new IllegalArgumentException();
                int n = ((Number) v).intValue();
                if (n < 3 || n > Recs.MAX_CARDS) throw new IllegalArgumentException();
                e.putInt(k, n);
            } else if (k.equals("shelf") || k.equals("topButtons")) {
                if (!(v instanceof JSONArray)) throw new IllegalArgumentException();
                JSONArray a = (JSONArray) v; List<String> values = new ArrayList<>();
                int limit = k.equals("shelf") ? (HomeApps.lowRam(getContext()) ? 8 : 64) : HomeApps.MAX_TOP;
                if (a.length() > limit) throw new IllegalArgumentException();
                for (int n = 0; n < a.length(); n++) {
                    if (!(a.get(n) instanceof String)) throw new IllegalArgumentException();
                    String value = a.getString(n);
                    if (k.equals("shelf") ? !value.matches("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z0-9_]+)+") : !HomeApps.TOP_DEFAULT.contains(value)) throw new IllegalArgumentException();
                    if (values.contains(value)) throw new IllegalArgumentException();
                    values.add(value);
                }
                e.putString(k, android.text.TextUtils.join(",", values));
            } else {
                if (!(v instanceof String) || ((String) v).length() > 100) throw new IllegalArgumentException();
                String value = (String) v;
                if (k.equals("theme") && !Arrays.asList("auto", "light", "dark").contains(value)) throw new IllegalArgumentException();
                if (k.equals("temperature") && !Arrays.asList("auto", "C", "F").contains(value)) throw new IllegalArgumentException();
                if (k.equals("forecast") && !Arrays.asList("hours", "days").contains(value)) throw new IllegalArgumentException();
                if (k.equals("language") && !value.matches("auto|[a-z]{2}(-[A-Za-z]{2})?")) throw new IllegalArgumentException();
                if ((k.equals("latitude") || k.equals("longitude")) && !value.isEmpty()) {
                    double d = Double.parseDouble(value), max = k.equals("latitude") ? 90 : 180;
                    if (Double.isNaN(d) || Double.isInfinite(d) || Math.abs(d) > max) throw new IllegalArgumentException();
                }
                e.putString(k, value);
            }
        }
        return e;
    }

    private void saveBaseline(JSONObject incoming) throws Exception {
        String saved = transfer().getString(BASELINE, null);
        JSONObject raw = saved == null ? new JSONObject() : new JSONObject(saved);
        for (Iterator<String> i = incoming.keys(); i.hasNext();) {
            String k = i.next();
            if (raw.has(k)) continue;
            JSONObject v = new JSONObject(); v.put("present", settings().contains(k));
            if (settings().contains(k)) v.put("value", settings().getAll().get(k));
            raw.put(k, v);
        }
        if (!transfer().edit().putString(BASELINE, raw.toString()).commit()) throw new IllegalStateException();
    }

    private void restore() throws Exception {
        String saved = transfer().getString(BASELINE, null);
        if (saved == null) return;
        JSONObject raw = new JSONObject(saved); SharedPreferences.Editor e = settings().edit();
        for (Iterator<String> i = raw.keys(); i.hasNext();) {
            String k = i.next();
            if (!KEYS.contains(k)) throw new IllegalArgumentException();
            JSONObject v = raw.getJSONObject(k);
            if (!v.getBoolean("present")) e.remove(k);
            else {
                Object value = v.get("value");
                if (value instanceof Boolean) e.putBoolean(k, (Boolean) value);
                else if (value instanceof Number) e.putInt(k, ((Number) value).intValue());
                else e.putString(k, (String) value);
            }
        }
        if (!e.commit()) throw new IllegalStateException();
        if (!transfer().edit().remove(BASELINE).commit()) throw new IllegalStateException();
        Prefs.reload();
    }

    @Override public Bundle call(String method, String arg, Bundle extras) {
        checkCaller(); Bundle b = new Bundle();
        try {
            if ("export".equals(method)) b.putString("payload", encoded(exportProfile()));
            else if ("apply".equals(method)) {
                if (arg == null || arg.length() > 32768) throw new IllegalArgumentException();
                JSONObject p = new JSONObject(new String(Base64.decode(arg, Base64.DEFAULT), StandardCharsets.UTF_8));
                SharedPreferences.Editor e = validate(p); saveBaseline(p.getJSONObject("settings"));
                if (!e.commit()) throw new IllegalStateException();
                Prefs.reload(); b.putString("payload", encoded(exportProfile()));
            } else if ("restore".equals(method)) { restore(); b.putString("payload", encoded(exportProfile())); }
            else throw new IllegalArgumentException();
            b.putBoolean("ok", true);
        } catch (Exception e) {
            b.putBoolean("ok", false); b.putString("error", "PROFILE_INVALID_OR_IO_ERROR");
        }
        return b;
    }

    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        checkCaller();
        try {
            if (!"/export".equals(uri.getPath())) throw new IllegalArgumentException();
            MatrixCursor c = new MatrixCursor(new String[] {"payload"}); c.addRow(new Object[] {encoded(exportProfile())}); return c;
        } catch (Exception e) { throw new IllegalArgumentException("PROFILE_EXPORT_FAILED"); }
    }
    @Override public String getType(Uri uri) { checkCaller(); return "application/json"; }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String selection, String[] args) { throw new UnsupportedOperationException(); }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { throw new UnsupportedOperationException(); }
}
