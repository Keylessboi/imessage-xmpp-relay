#!/usr/bin/env bash
#
# Reproducible, Gradle-free build of the AKA Oracle APK.
#
#   ./build.sh
#
# Toolchain: aapt2 -> javac -> d8 -> zipalign -> apksigner, exactly as the
# original APK was built. Nothing is downloaded here; point the script at an
# existing Android SDK with ANDROID_SDK_ROOT (or ANDROID_HOME), otherwise it
# searches ~/android-sdk, ~/Android/Sdk and /opt/android-sdk.
#
# The SDK must contain:
#   build-tools/<ver>/{aapt2,d8,zipalign,apksigner}
#   platforms/android-<api>/android.jar
# On this machine they were installed by hand (command-line tooling is not on
# PATH); see README.md "Rebuilding".
#
# Signing: reuses build/oracle.keystore (alias "oracle", store/key password
# "android") so "adb install -r" keeps working over the previous install.
# Override with KS / KS_PASS / KS_ALIAS. If the keystore is missing it is
# generated. Nothing here prints keystore contents.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/build"

# --- locate an Android SDK -------------------------------------------------
SDK="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
for cand in "$SDK" "$HOME/android-sdk" "$HOME/Android/Sdk" /opt/android-sdk; do
    if [ -n "$cand" ] && [ -d "$cand/build-tools" ] && [ -d "$cand/platforms" ]; then
        SDK="$cand"
        break
    fi
done
if [ -z "$SDK" ] || [ ! -d "$SDK/build-tools" ]; then
    echo "ERROR: no Android SDK with build-tools found." >&2
    echo "       export ANDROID_SDK_ROOT=/path/to/sdk  (needs build-tools/* and platforms/android-*/android.jar)" >&2
    exit 1
fi

BT="$(find "$SDK/build-tools" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -1)"
ANDROID_JAR="$(find "$SDK/platforms" -mindepth 2 -maxdepth 2 -name android.jar | sort -V | tail -1)"
if [ -z "$BT" ] || [ -z "$ANDROID_JAR" ]; then
    echo "ERROR: SDK at $SDK lacks build-tools or platforms/android-*/android.jar" >&2
    exit 1
fi
AAPT2="$BT/aapt2"; D8="$BT/d8"; ZIPALIGN="$BT/zipalign"; APKSIGNER="$BT/apksigner"
for t in "$AAPT2" "$D8" "$ZIPALIGN" "$APKSIGNER"; do
    [ -x "$t" ] || { echo "ERROR: missing $t" >&2; exit 1; }
done
echo "SDK         : $SDK"
echo "build-tools : $BT"
echo "android.jar : $ANDROID_JAR"

MIN_SDK=24
TARGET_SDK=32

# --- clean ----------------------------------------------------------------
rm -rf "$OUT/classes" "$OUT/dex" "$OUT/flat"
rm -f "$OUT/base.apk" "$OUT/unsigned.apk" "$OUT/aligned.apk" "$OUT/aka-oracle.apk" "$OUT/aka-oracle.apk.idsig"
mkdir -p "$OUT/classes" "$OUT/dex" "$OUT/flat"

# --- 1. resources (there are none today; kept for when res/ appears) ------
LINK_RES=()
if [ -d "$HERE/res" ]; then
    "$AAPT2" compile --dir "$HERE/res" -o "$OUT/flat/res.zip"
    LINK_RES=(-R "$OUT/flat/res.zip")
fi

# --- 2. link the manifest into an unsigned resource APK -------------------
"$AAPT2" link \
    -o "$OUT/base.apk" \
    -I "$ANDROID_JAR" \
    --manifest "$HERE/AndroidManifest.xml" \
    "${LINK_RES[@]}" \
    --min-sdk-version "$MIN_SDK" \
    --target-sdk-version "$TARGET_SDK" \
    --auto-add-overlay

# --- 3. compile Java ------------------------------------------------------
find "$HERE/src" -name '*.java' > "$OUT/sources.txt"
# -parameters is REQUIRED, not cosmetic: javac emits a MethodParameters
# attribute whose names are null for the synthetic constructor parameters of
# anonymous inner classes, and this d8 (R8 8.2.2 / build-tools 34.0.0) then dies
# with an NPE ("Cannot invoke String.length() because <parameter1> is null") on
# every *$1.class. -parameters fills the names in and d8 succeeds.
javac -source 8 -target 8 -Xlint:-options -parameters -encoding UTF-8 \
    -classpath "$ANDROID_JAR" \
    -d "$OUT/classes" \
    @"$OUT/sources.txt"

# --- 4. dex (d8 desugars; --lib supplies the platform classes) ------------
find "$OUT/classes" -name '*.class' -print0 | \
    xargs -0 "$D8" --release --min-api "$MIN_SDK" --lib "$ANDROID_JAR" --output "$OUT/dex"

# --- 5. package classes.dex into the APK ----------------------------------
cp "$OUT/base.apk" "$OUT/unsigned.apk"
( cd "$OUT/dex" && zip -q -j "$OUT/unsigned.apk" classes.dex )

# --- 6. zipalign BEFORE signing (apksigner preserves alignment) -----------
"$ZIPALIGN" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

# --- 7. sign --------------------------------------------------------------
KS="${KS:-$OUT/oracle.keystore}"
KS_PASS="${KS_PASS:-android}"
KS_ALIAS="${KS_ALIAS:-oracle}"
if [ ! -f "$KS" ]; then
    echo "generating $KS"
    keytool -genkeypair -keystore "$KS" -storetype PKCS12 \
        -storepass "$KS_PASS" -keypass "$KS_PASS" -alias "$KS_ALIAS" \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=AKA Oracle, O=openbubbles, C=US"
fi
"$APKSIGNER" sign \
    --ks "$KS" --ks-key-alias "$KS_ALIAS" \
    --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" \
    --out "$OUT/aka-oracle.apk" "$OUT/aligned.apk"

"$APKSIGNER" verify --print-certs "$OUT/aka-oracle.apk" | head -4
echo
echo "built $OUT/aka-oracle.apk"
echo "install: adb install -r $OUT/aka-oracle.apk"
