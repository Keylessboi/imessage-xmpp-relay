package com.bluebubbles.messaging.services.rustpush.eap_aka;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.telephony.TelephonyManager;
import android.util.Base64;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * EAP-AKA oracle, driven from adb. Two steps, because the ICC call itself needs
 * carrier privileges our app does not hold on Android 12 -- so the SIM is asked
 * via `service call iphonesubinfo 33 ... 129` (which shell *can* do) and this
 * only does the RFC 4187 wrapping.
 *
 *   step 1: am broadcast ... --es challenge <b64>              -> SIMREQ=...
 *   step 2: am broadcast ... --es challenge <b64> --es simresponse <b64>  -> RESPONSE=...
 *
 * Result lands in files/result.txt (readable via run-as: the app is debuggable).
 */
public class AkaReceiver extends BroadcastReceiver {
    private static final String TAG = "AkaOracle";

    @Override
    public void onReceive(final Context context, final Intent intent) {
        final PendingResult pr = goAsync();
        new Thread(new Runnable() {
            @Override public void run() {
                StringBuilder out = new StringBuilder();
                try {
                    TelephonyManager tm = context.getSystemService(TelephonyManager.class);
                    String op = tm.getSimOperator();
                    String imsi = safeSub(tm);
                    out.append("simOperator=").append(nz(op)).append("\n");
                    out.append("subscriberId=").append(imsi).append("\n");

                    String challenge = intent.getStringExtra("challenge");
                    String simResp = intent.getStringExtra("simresponse");
                    String realm = intent.getStringExtra("realm");
                    if (realm == null) realm = "nai.epc";

                    if (challenge != null) {
                        EapAkaChallenge chal = EapAkaChallenge.parseEapAkaChallenge(challenge);
                        out.append("SIMREQ=").append(chal.getSimAuthenticationRequest()).append("\n");

                        if (simResp != null) {
                            EapAkaSecurityContext ctx = EapAkaSecurityContext.from(simResp);
                            String eap = EapAkaResponse.getImsiEap(op, imsi, realm);
                            out.append("NAI=").append(nz(eap)).append("\n");
                            MasterKey mk = MasterKey.create(eap, ctx.getIk(), ctx.getCk());
                            if (mk == null || mk.getAut() == null) {
                                throw new RuntimeException("could not derive K_aut");
                            }
                            byte[] resp = EapAkaResponse.generateEapAkaChallengeResponse(
                                    ctx.getRes(), chal.getIdentifier(), mk.getAut());
                            if (resp == null) {
                                throw new RuntimeException("no challenge response bytes");
                            }
                            out.append("RESPONSE=")
                               .append(Base64.encodeToString(resp, Base64.NO_WRAP).trim())
                               .append("\n");
                        }
                    }
                } catch (Throwable t) {
                    Log.e(TAG, "aka failed", t);
                    out.append("ERROR=").append(t).append("\n");
                }
                write(context, out.toString());
                pr.finish();
            }
        }).start();
    }

    private static String nz(String s) { return s == null ? "null" : s; }

    private static String safeSub(TelephonyManager tm) {
        try { return nz(tm.getSubscriberId()); } catch (Throwable t) { return "ERR:" + t; }
    }

    private static void write(Context c, String s) {
        try {
            File f = new File(c.getFilesDir(), "result.txt");
            FileOutputStream fos = new FileOutputStream(f);
            fos.write(s.getBytes(StandardCharsets.UTF_8));
            fos.close();
        } catch (Throwable t) {
            Log.e(TAG, "write failed", t);
        }
    }
}
