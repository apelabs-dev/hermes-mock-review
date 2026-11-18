#!/usr/bin/env python3
"""Rebuild the committed, offline ESM anchor dependencies (Node/npm required only here).

npm ci uses the exact versions and integrity hashes in scripts/anchors/package-lock.json.
All install/cache state lives in a disposable directory under TMPDIR. No lifecycle scripts.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'mockreview' / 'board' / 'lib'
with tempfile.TemporaryDirectory(prefix='mr-anchor-build-') as temp:
    work = Path(temp)
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(ROOT / 'scripts' / 'anchors' / name, work / name)
    subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--cache', str(work / 'cache')],
                   cwd=work, check=True)
    (work / 'entry.js').write_text("export {finder} from '@medv/finder';\n"
                                  "export * as quote from 'dom-anchor-text-quote';\n"
                                  "export * as position from 'dom-anchor-text-position';\n", encoding='utf-8')
    subprocess.run(['node', str(work / 'node_modules/esbuild/bin/esbuild'), 'entry.js', '--bundle', '--format=esm',
                    '--platform=browser', '--target=es2020', '--legal-comments=inline', '--outfile=anchors.js'],
                   cwd=work, check=True)
    DEST.mkdir(exist_ok=True)
    shutil.copyfile(work / 'anchors.js', DEST / 'anchors.js')
    lock = json.loads((work / 'package-lock.json').read_text(encoding='utf-8'))
    notices = ['# Vendored anchor dependencies', '',
               'Built by `python scripts/vendor_anchors.py`; install requires no build or CDN.',
               'Registry metadata and package licenses verified from the integrity-pinned npm tarballs.', '']
    for path, info in lock['packages'].items():
        if not path or 'esbuild' in path:
            continue
        pkg = work / path
        meta = json.loads((pkg / 'package.json').read_text(encoding='utf-8'))
        licenses = [p for p in pkg.iterdir() if p.name.lower().startswith(('license', 'licence', 'copying'))]
        if not licenses and meta['name'] == 'ancestors':
            # This exact tarball declares MIT in package.json and README, but ships no LICENSE.
            licenses = [pkg / 'README.md']
        assert licenses, path
        notices += [f"## {meta['name']} {meta['version']} ({meta.get('license')})", '',
                    f"Source: {info['resolved']}", f"Integrity: {info['integrity']}", '']
        for license_file in licenses:
            notices += ['```text', license_file.read_text(encoding='utf-8'), '```', '']
    (DEST / 'LICENSES.md').write_text('\n'.join(notices), encoding='utf-8')
print('Rebuilt mockreview/board/lib/anchors.js and LICENSES.md')
