package com.openbubbles.aka;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telephony.SmsMessage;
import android.util.Base64;
import android.util.Log;

import java.nio.charset.StandardCharsets;

/**
 * Captures a REG-RESP delivered as a <b>data SMS</b> rather than a text SMS.
 *
 * <p>This is the piece that was missing. The reference implementation —
 * openbubbles-app {@code android/app/src/main/java/com/bluebubbles/messaging/PDUReceiver.kt} —
 * says so in its own comment:
 *
 * <pre>//Runs whenever a data SMS PDU is received. On some carriers (i.e. AT&T), the REG-RESP
 * //  message is sent as a data SMS (PDU) instead of a regular SMS message.</pre>
 *
 * <p>A port-addressed data SMS does <b>not</b> raise
 * {@code android.provider.Telephony.SMS_RECEIVED} and is <b>not</b> written to
 * {@code content://sms/inbox}, so an app listening only for the text-SMS broadcast
 * (as this helper did) will see nothing at all — the reply looks like silence.
 *
 * <p>Decoding mirrors PDUReceiver.kt exactly: try 3GPP, then 3GPP2, then fall back
 * to treating the raw PDU bytes as US-ASCII. The REG-RESP body is an ASCII string
 * sitting inside the PDU, so the fallback is the one that usually works. The
 * unsigned PDU is always archived to {@code files/pdu.txt} as base64, so the bytes
 * survive even when every parse fails.
 *
 * <p>Registered for {@code android.intent.action.DATA_SMS_RECEIVED} with
 * {@code <data android:scheme="sms" />} and no port restriction — the same filter
 * the reference app uses. Restricting to a port would never match, because the
 * port is chosen by the carrier.
 */
public class PduReceiver extends BroadcastReceiver {
    private static final String TAG = "AkaOracle";

    public static final String ACTION_DATA_SMS = "android.intent.action.DATA_SMS_RECEIVED";

    @Override
    public void onReceive(final Context context, final Intent intent) {
        final Context app = context.getApplicationContext();
        final Intent in = intent == null ? new Intent() : new Intent(intent);
        final PendingResult pr = goAsync();
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    handle(app, in);
                } catch (Throwable t) {
                    Log.e(TAG, "data sms receive failed", t);
                    RegReceiver.Io.appendSms(app, "[pdu] src=DATA_SMS error=" + t + "\n");
                } finally {
                    pr.finish();
                }
            }
        }).start();
    }

    private static void handle(Context ctx, Intent intent) {
        if (!ACTION_DATA_SMS.equals(intent.getAction())) {
            Log.w(TAG, "ignoring action " + intent.getAction());
            return;
        }

        // "sms://localhost:5497" — the port is chosen by the carrier, so record it.
        String dest = intent.getDataString();
        int port = -1;
        try { if (intent.getData() != null) port = intent.getData().getPort(); } catch (Throwable ignored) {}

        Object[] pdus = null;
        try {
            Object o = intent.getExtras() == null ? null : intent.getExtras().get("pdus");
            pdus = (Object[]) o;
        } catch (Throwable t) {
            Log.e(TAG, "pdus extra unreadable", t);
        }

        if (pdus == null || pdus.length == 0) {
            RegReceiver.Io.appendSms(ctx, "[pdu] src=DATA_SMS dest=" + dest + " port=" + port
                    + " EMPTY-PDU\n");
            return;
        }

        for (Object o : pdus) {
            if (!(o instanceof byte[])) continue;
            byte[] pdu = (byte[]) o;

            // Keep the bytes unconditionally: a parse failure must not lose the reply.
            RegReceiver.Io.appendPdu(ctx, "[pdu] port=" + port + " dest=" + dest
                    + " len=" + pdu.length + " base64=" + Base64.encodeToString(pdu, Base64.NO_WRAP) + "\n");

            String body = extract(pdu);
            if (body != null) {
                RegReceiver.Io.appendSms(ctx, "[pdu] src=DATA_SMS port=" + port + " body=" + body + "\n");
                RegReceiver.Io.writeResult(ctx, "REGRESP=" + body + "\n");
                Log.i(TAG, "REG-RESP via data SMS: " + body);
            } else {
                // Not a REG-RESP (or not parseable) — still record what we saw, so a
                // missed reply is never indistinguishable from silence.
                RegReceiver.Io.appendSms(ctx, "[pdu] src=DATA_SMS port=" + port
                        + " body-not-found (see files/pdu.txt)\n");
            }
        }
    }

    /** Port of PDUReceiver.kt: 3GPP -> 3GPP2 -> raw ASCII, looking for "REG-RESP". */
    private static String extract(byte[] pdu) {
        String[] formats = new String[]{"3gpp", "3gpp2"};
        for (String fmt : formats) {
            try {
                SmsMessage m;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    m = SmsMessage.createFromPdu(pdu, fmt);
                } else {
                    m = SmsMessage.createFromPdu(pdu);
                }
                if (m != null) {
                    String b = m.getDisplayMessageBody();
                    if (b == null) b = m.getMessageBody();
                    if (b != null && b.contains("REG-RESP")) return from(b);
                }
            } catch (Throwable t) {
                Log.d(TAG, "pdu not " + fmt + ": " + t);
            }
        }
        try {
            String raw = new String(pdu, StandardCharsets.US_ASCII);
            if (raw.contains("REG-RESP")) return from(raw);
        } catch (Throwable t) {
            Log.e(TAG, "raw pdu decode failed", t);
        }
        return null;
    }

    private static String from(String input) {
        return input.substring(input.indexOf("REG-RESP")).trim();
    }
}
