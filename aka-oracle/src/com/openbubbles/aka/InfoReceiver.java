package com.openbubbles.aka;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;
import android.telephony.TelephonyManager;
import android.util.Log;

import java.util.List;

/**
 * Diagnostic broadcast (adb driven) that reports what the framework will tell
 * this app about the SIM -- in particular the line-1 number, which is optional
 * data the carrier may or may not store on the SIM.
 *
 *   adb shell am broadcast -n com.openbubbles.aka/com.openbubbles.aka.InfoReceiver \
 *       -a com.openbubbles.aka.INFO
 *
 * Writes files/info.txt (readable with run-as). Keys: LINE1, SIMOP, SIMOPNAME,
 * NETOP, NETOPNAME, SUBID, SUBS, ICCID(blanked), COUNTRY, ROAMING.
 * Nothing here is a credential; the ICCID is masked to its last 4 digits.
 */
public class InfoReceiver extends BroadcastReceiver {
    private static final String TAG = "AkaOracle";

    @Override
    public void onReceive(final Context context, final Intent intent) {
        final PendingResult pr = goAsync();
        final Context app = context.getApplicationContext();
        new Thread(new Runnable() {
            @Override public void run() {
                StringBuilder out = new StringBuilder();
                try {
                    TelephonyManager tm = (TelephonyManager) app.getSystemService(Context.TELEPHONY_SERVICE);
                    out.append("SDK=").append(Build.VERSION.SDK_INT).append("\n");
                    if (tm != null) {
                        out.append("LINE1=").append(nz(tm.getLine1Number())).append("\n");
                        out.append("SIMOP=").append(nz(tm.getSimOperator())).append("\n");
                        out.append("SIMOPNAME=").append(nz(tm.getSimOperatorName())).append("\n");
                        out.append("NETOP=").append(nz(tm.getNetworkOperator())).append("\n");
                        out.append("NETOPNAME=").append(nz(tm.getNetworkOperatorName())).append("\n");
                        out.append("COUNTRY=").append(nz(tm.getSimCountryIso())).append("\n");
                        out.append("ROAMING=").append(tm.isNetworkRoaming()).append("\n");
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                            out.append("SUBID=").append(tm.getSubscriptionId()).append("\n");
                        }
                        String iccid = nz(tm.getSimSerialNumber());
                        out.append("ICCID=").append(mask(iccid)).append("\n");
                    }
                    SubscriptionManager sm = (SubscriptionManager) app.getSystemService(
                            Context.TELEPHONY_SUBSCRIPTION_SERVICE);
                    if (sm != null) {
                        List<SubscriptionInfo> subs = sm.getActiveSubscriptionInfoList();
                        out.append("SUBS=").append(subs == null ? "null" : String.valueOf(subs.size())).append("\n");
                        if (subs != null) {
                            for (SubscriptionInfo s : subs) {
                                out.append("  sub id=").append(s.getSubscriptionId())
                                   .append(" slot=").append(s.getSimSlotIndex())
                                   .append(" carrier=").append(s.getCarrierName())
                                   .append(" number=").append(nz(s.getNumber()))
                                   .append("\n");
                            }
                        }
                    }
                } catch (Throwable t) {
                    Log.e(TAG, "info failed", t);
                    out.append("ERROR=").append(t).append("\n");
                }
                RegReceiver.Io.write(app, "info.txt", out.toString(), false);
                pr.finish();
            }
        }).start();
    }

    private static String mask(String s) {
        if (s == null || s.length() < 4) return nz(s);
        return "****" + s.substring(s.length() - 4);
    }

    private static String nz(String s) { return s == null ? "null" : s; }
}
