# SeekNano RE artifacts

Track here only files we generated ourselves. The original `com.thermal.seeknano`
APK/XAPK (Seek Thermal property) is **not** committed, per the legal note in the
README — pull it e.g. from APKPure (`com.thermal.seeknano` v1.5.0, versionCode 27)
and repro with the tools in `docs/rebuild.md` (moved: the kit lives in `history/tools/`).

| File | Origin | Note |
|---|---|---|
| `seekspy-installer.apk.sha256` | local build | checksum of repacked `seekspy.apk` (51 MB) — actual APK attaches to GitHub releases |
| `sha256-original-apk.txt` | local calc | integrity reference of the original XAPK used for analysis |

Rebuild pipeline (order):

1. `python3 -m zipfile -e seeknano.xapk xapk/`
2. `java -jar APKEditor.jar m -i xapk -o merged.apk`
3. `python3 inject_gadget.py merged.apk patched.apk`  (adds libseekgadget.so + config asset + smali hook; script in this repo)
4. `java -jar APKEditor.jar d/b` renamed package + edit via `rename.py`
5. `keytool`/`apksigner` sign → `seekspy.apk`
