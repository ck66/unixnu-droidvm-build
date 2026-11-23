#!/usr/bin/env python3
"""Build the VirtIO MMIO block transport, whatever this tree calls it.

Run from the tree's scripts/ directory by `unixnu build kernel` (the board.toml
[kernel] archives list names it), i.e. cwd is the tree root.

Why a wrapper instead of one more "build-arm-driver.py <name>" line: the tree's
components.toml and the tree's build-xnu-4k-baseline.sh do not come from the same
revision on this board.  One snapshot names the component

    id = "VirtioMmioBlock"   legacy_name = "NativeVirtioBlock"
    outputs = ["build/virtio-mmio-block/libVirtioMmioBlock.a"]

and an older one keeps the legacy identity:

    id = "NativeVirtioBlock"
    outputs = ["build/native-virtio-block/libNativeVirtioBlock.a"]

run62 passed VirtioMmioBlock and died in argparse (`invalid choice`), which left
build-xnu-4k-baseline.sh:787 `test -f $VIRTIO_ARCHIVE` failing.  So: read the id out
of the tree's own catalog, build through the tree's own build-arm-driver.py, and
then make BOTH output paths exist -- the link only needs the one the script picked.

The source directory is a second hazard: build-arm-driver.py derives it from the
identifier (drivers/<id>/), while the submodule is always checked out at
drivers/VirtioMmioBlock.  A symlink bridges that when the names differ.
"""
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
SCRIPT = TREE / 'scripts' / 'build-arm-driver.py'
CATALOG = TREE / 'components.toml'
SOURCE = 'NativeVirtioBlock.cpp'
TARGETS = [
    TREE / 'build/virtio-mmio-block/libVirtioMmioBlock.a',
    TREE / 'build/native-virtio-block/libNativeVirtioBlock.a',
]


def fail(message):
    print('virtio: ' + message, file=sys.stderr)
    raise SystemExit(1)


if not SCRIPT.is_file():
    fail('no %s' % SCRIPT)
if not CATALOG.is_file():
    fail('no %s' % CATALOG)

entries = tomllib.loads(CATALOG.read_text()).get('component', [])
entry = next((item for item in entries if SOURCE in (item.get('source_files') or [])), None)
if entry is None:
    fail('components.toml has no driver with source_files containing %r' % SOURCE)

identifier = entry['id']
outputs = entry.get('outputs') or []
print('virtio: catalog id %r, outputs %s' % (identifier, outputs))

# Where the sources actually are: prefer the catalog path, else the submodule name,
# else anything under drivers/ that carries the file.
declared = entry.get('path') or ''
places = [TREE / declared] if declared else []
places += [TREE / 'drivers' / identifier, TREE / 'drivers' / 'VirtioMmioBlock']
places += sorted(path.parent for path in (TREE / 'drivers').glob('*/' + SOURCE))
source = next((path for path in places if (path / SOURCE).is_file()), None)
if source is None:
    fail('no directory under drivers/ contains %s (looked at %s)'
         % (SOURCE, ', '.join(str(path.relative_to(TREE)) for path in places)))

holder = TREE / 'drivers' / identifier
if holder != source:
    # build-arm-driver.py looks in drivers/<identifier>/ and nowhere else.
    if holder.is_symlink():
        holder.unlink()
    if not holder.exists():
        holder.symlink_to(source)
        print('virtio: linked drivers/%s -> %s' % (identifier, source.relative_to(TREE)))

before = {path: path.stat().st_mtime_ns for path in TARGETS if path.is_file()}
started = time.time()
result = subprocess.run([sys.executable, str(SCRIPT), identifier], cwd=str(TREE))
if result.returncode != 0:
    fail('build-arm-driver.py %s exited %d' % (identifier, result.returncode))

# What it just wrote: the catalog's own output path first, else the newest archive
# under build/ that mentions virtio.
produced = [TREE / path for path in outputs if (TREE / path).is_file()]
if not produced:
    produced = sorted(
        (path for path in (TREE / 'build').glob('**/*irtio*.a') if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    produced = produced[:1]
if not produced:
    fail('build-arm-driver.py %s produced no archive under build/' % identifier)

archive = produced[0]
if archive.stat().st_mtime_ns <= before.get(archive, 0) and archive in before:
    print('virtio: warning, %s was not rewritten by this run' % archive.relative_to(TREE))
print('virtio: built %s (%d bytes)' % (archive.relative_to(TREE), archive.stat().st_size))

for target in TARGETS:
    if target.is_file():
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, target)
    print('virtio: copied to %s so the link step finds it' % target.relative_to(TREE))

print('virtio: done in %.1fs' % (time.time() - started))
