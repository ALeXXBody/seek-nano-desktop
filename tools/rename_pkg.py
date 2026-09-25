#!/usr/bin/env python3
"""Step 4: rename the package across the APKEditor-decoded tree.

Original: com.thermal.seeknano  ->  Spy: com.thermal.seeknanospy
Covers: manifest package attr, custom permission, component names (qualified by
the rename because they were absolute), provider authorities, arsc package name.
Java class names are intentionally left untouched (the spy keeps the SDK's code).
"""
import sys

OLD = 'com.thermal.seeknano'
NEW = 'com.thermal.seeknanospy'

def patch(path):
    s = open(path).read()
    n = s.count(OLD)
    open(path, 'w').write(s.replace(OLD, NEW))
    print(f'{path}: {n} replacements')

if __name__ == '__main__':
    base = sys.argv[1]  # APKEditor decoded root
    patch(f'{base}/AndroidManifest.xml')
    patch(f'{base}/resources/package_1/package.json')
    patch(f'{base}/resources/package_1/res/values/public.xml')
