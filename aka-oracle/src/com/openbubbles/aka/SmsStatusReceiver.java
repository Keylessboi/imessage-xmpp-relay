package com.openbubbles.aka;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.telephony.SmsManager;
import android.util.Log;

/**
 * Receives the asynchronous SmsManager sent/delivered PendingIntents for a
 * REG-REQ and appends the outcome to files/result.txt.
 *
 *   SENT=ok;r=<id>              framework reports the SMS was sent
 *   SENT=err(rc=N:...);r=<id>   send failed; N is an SmsManager.RESULT_ERROR_*
 *   DELIVERED=ok;r=<id>         the gateway acknowledged delivery (if requested)
 *   DELIVERED=err(rc=N:...);r=<id>
 */
public class SmsStatusReceiver extends BroadcastReceiver {
    private static final String TAG = "AkaOracle";

    public static final String ACTION_SENT = "com.openbubbles.aka.SMS_SENT";
    public static final String ACTION_DELIVERED = "com.openbubbles.aka.SMS_DELIVERED";

    @Override
    public void onReceive(Context context, Intent intent) {
        final Context app = context.getApplicationContext();
        final String action = intent == null ? null : intent.getAction();
        final int rc = getResultCode();
        final int r = intent == null ? -1 : intent.getIntExtra("r", -1);

        String line;
        if (ACTION_SENT.equals(action)) {
            line = rc == Activity.RESULT_OK
                    ? "SENT=ok;r=" + r + "\n"
                    : "SENT=err(rc=" + rc + ":" + describe(rc) + ");r=" + r + "\n";
        } else if (ACTION_DELIVERED.equals(action)) {
            line = rc == Activity.RESULT_OK
                    ? "DELIVERED=ok;r=" + r + "\n"
                    : "DELIVERED=err(rc=" + rc + ":" + describe(rc) + ");r=" + r + "\n";
        } else {
            line = null;
        }
        if (line != null) {
            Log.i(TAG, line.trim());
            RegReceiver.Io.appendResult(app, line);
        }
    }

    private static String describe(int rc) {
        switch (rc) {
            case SmsManager.RESULT_ERROR_GENERIC_FAILURE: return "generic-failure";
            case SmsManager.RESULT_ERROR_NO_SERVICE: return "no-service";
            case SmsManager.RESULT_ERROR_NULL_PDU: return "null-pdu";
            case SmsManager.RESULT_ERROR_RADIO_OFF: return "radio-off";
            case SmsManager.RESULT_ERROR_SHORT_CODE_NOT_ALLOWED: return "short-code-not-allowed";
            case SmsManager.RESULT_ERROR_SHORT_CODE_NEVER_ALLOWED: return "short-code-never-allowed";
            default: return "unknown";
        }
    }
}
