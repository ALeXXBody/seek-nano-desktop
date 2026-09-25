# Rebuild / redeploy recipes

Everything below runs on the TrueNAS box (Debian, root). Environment pieces:
`java` (21), `apksigner`, `keytool`, `adb`, `python3`, pip-installed `androguard`,
jars in `tools/` (`APKEditor-1.4.9.jar`, optional `apktool_3.0.3.jar`),
frida gadget `.so` files for arm/arm64/x86_64 (download matching your frida version).

## 0. Fetch the source app (one-time)

```bash
# Version found in Play Store: com.thermal.seeknano 1.5.0 (27)
curl -L "https://d.apkpure.com/b/XAPK/com.thermal.seeknano?version=latest" -o seeknano.xapk
```

## 1. Merge splits → universal APK

```bash
python3 -m zipfile -e seeknano.xapk xapk/
java -jar tools/APKEditor.jar m -i xapk -o merged.apk
```

## 2. Inject gadget

```bash
python3 tools/inject_gadget.py merged.apk patched.apk
```

## 3. Decode, rename, rebuild

```bash
java -jar tools/APKEditor.jar d -i patched.apk -o decoded
python3 tools/rename_pkg.py decoded
# (one-time) smali hook — see docs: loadLibrary("seekgadget") before loadLibrary("seekcamera")
#   file: decoded/smali/classes/com/thermal/seeknano/MainActivity.smali
java -jar tools/APKEditor.jar b -i decoded -o patched_name.apk
```

## 4. Sign

```bash
keytool -genkeypair -keystore debug.keystore -alias spy -keypass spytrace \
        -storepass spytrace -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=SeekSpy,O=Local"
apksigner sign --ks debug.keystore --ks-key-alias spy --ks-pass pass:spytrace \
        --key-pass pass:spytrace --out seekspy.apk patched_name.apk
apksigner verify --print-certs seekspy.apk
```

## 5. Deploy WITHOUT a cable (phone already has Wireless debugging on)

On the phone: Developer options → Wireless debugging → Pair device with pairing code.
Note the 6-digit code + IP:PORT shown, then from the Linux box:

```bash
adb pair <IP>:<PAIRPORT>          # enter the 6-digit code when asked
adb devices                        # should list <IP>:<DEBUGPORT> device
adb install -r --bypass-low-target-sdk-block artifacts/seekspy.apk
adb push frida/spytrace.js /data/local/tmp/spytrace.js
adb shell am start -n com.thermal.seeknanospy/com.thermal.seeknanospy.MainActivity
adb shell "run-as com.thermal.seeknanospy ls files"  # optional checks
adb logcat -s spysdk SeekwareAAR frida
```

## 6. Capture a session

1. Phone: open **Seek Nano (spy)**, plug in the Nano camera, let it connect & stream ~1 min.
2. TS log is mirrored to `/sdcard/Download/spytrace.log`.
3. `adb pull /sdcard/Download/spytrace.log analysis/trace-<date>.log`

## 7. Extract gadget `.so` files (one-time, from frida release)

```bash
for a in arm arm64 x86_64; do
  curl -LO "https://github.com/frida/frida/releases/download/17.19.0/frida-gadget-17.19.0-android-$a.so.xz"
  xz -dk "frida-gadget-17.19.0-android-$a.so.xz" -c > "tools/gadget-$a.so"
done
```
