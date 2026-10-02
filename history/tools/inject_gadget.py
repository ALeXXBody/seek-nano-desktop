#!/usr/bin/env python3
"""Step 3 of the spy-APK rebuild: inject the Frida gadget into a merged universal APK.

- writes gadget .so as lib/<abi>/libseekgadget.so
- adds assets/libseekgadget_config.so (gadget config JSON that loads /data/local/tmp/spytrace.js)
- caller is responsible for the smali patch (add loadLibrary("seekgadget") in MainActivity.<clinit>)
"""
import sys
import zipfile

def main(src, dst):
    zin = zipfile.ZipFile(src)
    abis = sorted(set(n.split('/')[1] for n in zin.namelist() if n.startswith('lib/')))
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        for abi in abis:
            gadget = f'tools/gadget-{abi}.so'
            with open(gadget, 'rb') as f:
                zout.writestr(f'lib/{abi}/libseekgadget.so', f.read())
            gadget_file_map = {'armeabi-v7a': 'arm', 'arm64-v8a': 'arm64', 'x86_64': 'x86_64'}
        config = b'{"interaction":{"type":"script","path":"/data/local/tmp/spytrace.js","on_change":"reload"}}'
        zout.writestr('assets/libseekgadget_config.so', config)
    print('gadget injected:', dst)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
