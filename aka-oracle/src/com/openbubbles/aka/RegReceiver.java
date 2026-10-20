package com.openbubbles.aka;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telephony.SmsManager;
import android.telephony.SubscriptionManager;
import android.telephony.TelephonyManager;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Random;

/**
 * Sends the iMessage carrier-gateway registration SMS, triggered from adb:
 *
 *   adb shell am broadcast -n com.openbubbles.aka/com.openbubbles.aka.RegReceiver \
 *       -a com.openbubbles.aka.REGREQ --es token <apns-token-hex> [--es gateway 22223333] [--es sub N]
 *
 * Body sent is exactly:   REG-REQ?v=3;t=<token>;r=<random-int>;
 * (override the whole body with --es body '...' if ever needed).
 *
 * Result lands in files/result.txt. Keys written here are deliberately distinct
 * from the AKA oracle's (SIMREQ=/RESPONSE=) so the two workflows cannot collide:
 *   REGREQ=...       the body that was sent
 *   GATEWAY=...      destination shortcode
 *   SUB=...          subscription id used (-1 = SmsManager default)
 *   SENT=submitted   the framework accepted the message for sending
 *   SENDFAIL=...     an exception was thrown (no permission, radio off, ...)
 * The asynchronous sent/delivered status is appended by SmsStatusReceiver as
 * SENT=ok / SENT=err(rc=N:...) / DELIVERED=ok / DELIVERED=err(...).
 */
public class RegReceiver extends BroadcastReceiver {
    private static final String TAG = "AkaOracle";
    private static final String DEFAULT_GATEWAY = "22223333";

    @Override
    public void onReceive(final Context context, final Intent intent) {
        final PendingResult pr = goAsync();
        final Context app = context.getApplicationContext();
        final Intent in = intent == null ? new Intent() : new Intent(intent);
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    send(app, in);
                } catch (Throwable t) {
                    Log.e(TAG, "regreq failed", t);
                    Io.writeResult(app, "SENDFAIL=" + t + "\n");
                } finally {
                    pr.finish();
                }
            }
        }).start();
    }

    private static void send(Context ctx, Intent intent) {
        String token = intent.getStringExtra("token");
        if (token == null) token = "";
        String gateway = intent.getStringExtra("gateway");
        if (gateway == null || gateway.length() == 0) gateway = DEFAULT_GATEWAY;

        int r = new Random().nextInt() & 0x7fffffff;
        String body = intent.getStringExtra("body");
        if (body == null || body.length() == 0) {
            body = "REG-REQ?v=3;t=" + token + ";r=" + r + ";";
        }

        int sub = intent.getIntExtra("sub", -1);
        if (sub < 0) sub = currentSubscriptionId(ctx);
        SmsManager sm = smsManagerFor(sub);
        String usedSub = String.valueOf(sub);

        Intent sentIntent = new Intent(ctx, SmsStatusReceiver.class);
        sentIntent.setAction(SmsStatusReceiver.ACTION_SENT);
        sentIntent.putExtra("r", r);
        Intent deliveredIntent = new Intent(ctx, SmsStatusReceiver.class);
        deliveredIntent.setAction(SmsStatusReceiver.ACTION_DELIVERED);
        deliveredIntent.putExtra("r", r);

        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) piFlags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent sentPi = PendingIntent.getBroadcast(ctx, r, sentIntent, piFlags);
        PendingIntent deliveredPi = PendingIntent.getBroadcast(ctx, r ^ 0x55aa, deliveredIntent, piFlags);

        StringBuilder head = new StringBuilder();
        head.append("REGREQ=").append(body).append("\n");
        head.append("GATEWAY=").append(gateway).append("\n");
        head.append("SUB=").append(usedSub).append("\n");

        try {
            sm.sendTextMessage(gateway, null, body, sentPi, deliveredPi);
            head.append("SENT=submitted;r=").append(r).append("\n");
            Log.i(TAG, "REGREQ submitted to " + gateway + " via sub " + usedSub);
        } catch (Throwable t) {
            head.append("SENDFAIL=").append(t).append("\n");
            Log.e(TAG, "REGREQ sendTextMessage threw", t);
        }
        Io.writeResult(ctx, head.toString());
    }

    private static int currentSubscriptionId(Context ctx) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                TelephonyManager tm = (TelephonyManager) ctx.getSystemService(Context.TELEPHONY_SERVICE);
                if (tm != null) {
                    int id = tm.getSubscriptionId();
                    if (id >= 0) return id;
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "TelephonyManager.getSubscriptionId: " + t);
        }
        try {
            int id = SubscriptionManager.getDefaultSmsSubscriptionId();
            if (id >= 0) return id;
        } catch (Throwable t) {
            Log.w(TAG, "getDefaultSmsSubscriptionId: " + t);
        }
        return -1;
    }

    private static SmsManager smsManagerFor(int sub) {
        if (sub >= 0) {
            try {
                SmsManager sm = SmsManager.getSmsManagerForSubscriptionId(sub);
                if (sm != null) return sm;
            } catch (Throwable t) {
                Log.w(TAG, "getSmsManagerForSubscriptionId(" + sub + "): " + t);
            }
        }
        return SmsManager.getDefault();
    }

    /** Small shared file helpers (single process, single lock). */
    static final class Io {
        static final Object LOCK = new Object();

        static File file(Context c, String name) {
            return new File(c.getFilesDir(), name);
        }

        static void writeResult(Context c, String s) {
            write(c, "result.txt", s, false);
        }

        static void appendResult(Context c, String s) {
            write(c, "result.txt", s, true);
        }

        static void appendSms(Context c, String s) {
            write(c, "sms.txt", s, true);
        }

        /** Raw PDU archive: a data-SMS reply must survive even if none of the
         *  decoders recognise it, so the bytes are kept as base64 in pdu.txt. */
        static void appendPdu(Context c, String s) {
            write(c, "pdu.txt", s, true);
        }

        static void write(Context c, String name, String s, boolean append) {
            synchronized (LOCK) {
                FileOutputStream fos = null;
                try {
                    fos = new FileOutputStream(file(c, name), append);
                    fos.write(s.getBytes(StandardCharsets.UTF_8));
                    fos.flush();
                } catch (Throwable t) {
                    Log.e(TAG, "write " + name + " failed", t);
                } finally {
                    if (fos != null) try { fos.close(); } catch (Throwable ignored) {}
                }
            }
        }

        static String read(Context c, String name) {
            synchronized (LOCK) {
                java.io.FileInputStream fis = null;
                try {
                    File f = file(c, name);
                    if (!f.exists()) return "";
                    fis = new java.io.FileInputStream(f);
                    byte[] buf = new byte[(int) f.length()];
                    int n = fis.read(buf);
                    return n <= 0 ? "" : new String(buf, 0, n, StandardCharsets.UTF_8);
                } catch (Throwable t) {
                    Log.e(TAG, "read " + name + " failed", t);
                    return "";
                } finally {
                    if (fis != null) try { fis.close(); } catch (Throwable ignored) {}
                }
            }
        }
    }
}
