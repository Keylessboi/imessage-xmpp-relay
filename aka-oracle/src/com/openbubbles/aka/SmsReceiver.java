package com.openbubbles.aka;

import android.content.BroadcastReceiver;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.Telephony;
import android.telephony.SmsMessage;
import android.util.Log;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Appends every inbound SMS to files/sms.txt, verbatim, one record per line:
 *
 *   [<iso-timestamp>] src=SMS_RECEIVED from=<address> time=<millis> body=<body>
 *
 * Primary path: the manifest-registered android.provider.Telephony.SMS_RECEIVED
 * broadcast, parsed with Telephony.Sms.Intents.getMessagesFromIntent() (which
 * handles both 3GPP and 3GPP2 PDUs). If that throws or yields nothing, the raw
 * "pdus" extra is parsed by hand with SmsMessage.createFromPdu(). Both are the
 * Telephony.Sms.Intents path; there is no other supported receive path in a
 * non-default SMS app.
 *
 * Extra adb-drivable actions (for testing the file plumbing without a real SMS):
 *   adb shell am broadcast -n com.openbubbles.aka/com.openbubbles.aka.SmsReceiver \
 *       -a com.openbubbles.aka.SMS_TEST --es sender +15550001111 --es body hello
 *   adb shell am broadcast -n com.openbubbles.aka/com.openbubbles.aka.SmsReceiver \
 *       -a com.openbubbles.aka.SMS_INBOX_DUMP
 * The dump action reads content://sms/inbox (needs READ_SMS) and appends only
 * messages newer than the last dump, recorded in files/inbox_last.txt.
 */
public class SmsReceiver extends BroadcastReceiver {
    private static final String TAG = "AkaOracle";

    public static final String ACTION_SMS_RECEIVED = "android.provider.Telephony.SMS_RECEIVED";
    public static final String ACTION_TEST = "com.openbubbles.aka.SMS_TEST";
    public static final String ACTION_DUMP = "com.openbubbles.aka.SMS_INBOX_DUMP";

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
                    Log.e(TAG, "sms receive failed", t);
                    RegReceiver.Io.appendSms(app, stamp() + " src=ERROR error=" + t + "\n");
                } finally {
                    pr.finish();
                }
            }
        }).start();
    }

    private static void handle(Context ctx, Intent intent) {
        String action = intent.getAction();

        if (ACTION_TEST.equals(action)) {
            String sender = intent.getStringExtra("sender");
            String body = intent.getStringExtra("body");
            append(ctx, "SMS_TEST", sender == null ? "?" : sender, System.currentTimeMillis(),
                    body == null ? "" : body);
            return;
        }
        if (ACTION_DUMP.equals(action)) {
            dumpInbox(ctx);
            return;
        }
        if (!ACTION_SMS_RECEIVED.equals(action)) {
            Log.w(TAG, "ignoring action " + action);
            return;
        }

        SmsMessage[] msgs = null;
        try {
            msgs = Telephony.Sms.Intents.getMessagesFromIntent(intent);
        } catch (Throwable t) {
            Log.w(TAG, "getMessagesFromIntent failed, falling back to raw pdus", t);
        }
        if (msgs == null || msgs.length == 0) {
            msgs = parsePdus(intent);
        }
        if (msgs == null || msgs.length == 0) {
            RegReceiver.Io.appendSms(ctx, stamp() + " src=SMS_RECEIVED EMPTY-PDU\n");
            return;
        }
        for (SmsMessage m : msgs) {
            if (m == null) continue;
            String from = m.getDisplayOriginatingAddress();
            String body = m.getDisplayMessageBody();
            if (body == null) body = m.getMessageBody();
            append(ctx, "SMS_RECEIVED", from == null ? "?" : from, m.getTimestampMillis(),
                    body == null ? "" : body);
        }
    }

    private static SmsMessage[] parsePdus(Intent intent) {
        try {
            Object[] pdus = (Object[]) intent.getExtras().get("pdus");
            if (pdus == null) return null;
            String format = intent.getStringExtra("format");
            if (format == null) format = "3gpp";
            SmsMessage[] out = new SmsMessage[pdus.length];
            for (int i = 0; i < pdus.length; i++) {
                byte[] pdu = (byte[]) pdus[i];
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    out[i] = SmsMessage.createFromPdu(pdu, format);
                } else {
                    out[i] = SmsMessage.createFromPdu(pdu);
                }
            }
            return out;
        } catch (Throwable t) {
            Log.e(TAG, "raw pdu parse failed", t);
            return null;
        }
    }

    @SuppressWarnings("deprecation")
    private static void dumpInbox(Context ctx) {
        long since = 0L;
        try { since = Long.parseLong(RegReceiver.Io.read(ctx, "inbox_last.txt").trim()); }
        catch (Throwable ignored) {}
        long maxDate = since;
        int count = 0;
        ContentResolver cr = ctx.getContentResolver();
        Cursor c = null;
        try {
            c = cr.query(Uri.parse("content://sms/inbox"),
                    new String[]{"address", "body", "date"}, null, null, "date ASC");
            if (c == null) {
                RegReceiver.Io.appendSms(ctx, stamp() + " src=INBOX-DUMP error=null-cursor\n");
                return;
            }
            int ia = c.getColumnIndex("address");
            int ib = c.getColumnIndex("body");
            int id = c.getColumnIndex("date");
            while (c.moveToNext()) {
                long date = id >= 0 ? c.getLong(id) : 0L;
                if (date <= since) continue;
                String addr = ia >= 0 ? c.getString(ia) : "?";
                String body = ib >= 0 ? c.getString(ib) : "";
                append(ctx, "INBOX", addr == null ? "?" : addr, date, body == null ? "" : body);
                if (date > maxDate) maxDate = date;
                count++;
            }
        } catch (Throwable t) {
            Log.e(TAG, "inbox dump failed", t);
            RegReceiver.Io.appendSms(ctx, stamp() + " src=INBOX-DUMP error=" + t + "\n");
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) {}
        }
        if (count > 0 || maxDate > since) {
            RegReceiver.Io.writeResult(ctx, "INBOXDUMP=" + count + "\n");
        }
        RegReceiver.Io.write(ctx, "inbox_last.txt", String.valueOf(maxDate), false);
    }

    private static void append(Context ctx, String src, String from, long when, String body) {
        String line = stamp() + " src=" + src + " from=" + from + " time=" + when + " body=" + body + "\n";
        RegReceiver.Io.appendSms(ctx, line);
        Log.i(TAG, line.trim());
    }

    private static String stamp() {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US);
        return "[" + f.format(new Date()) + "]";
    }
}
