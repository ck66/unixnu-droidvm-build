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
build-xnu-4k-baseline.sh:787 `test -f $VIRTIO_ARCHIVE` failing.  run63 then read
the id out of components.toml, got "VirtioMmioBlock" -- and died in the same
argparse line, because that tree's build-arm-driver.py keys its driver table by
the legacy name (`NativeVirtioBlock`) while the catalog spells the entry
`id = "VirtioMmioBlock"`.  So: ask build-arm-driver.py --help which names it
takes, build through it, and then make BOTH output paths exist -- the link only
needs the one the kernel script picked.

The source directory is a second hazard: build-arm-driver.py derives it from the
identifier (drivers/<id>/), while the submodule is always checked out at
drivers/VirtioMmioBlock.  A symlink bridges that when the names differ.
"""
import re
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
legacy = entry.get('legacy_name') or identifier
outputs = entry.get('outputs') or []
print('virtio: catalog id %r, legacy %r, outputs %s' % (identifier, legacy, outputs))


def accepted_names():
    """The component names this tree's build-arm-driver.py will actually take.

    It is not enough to read components.toml: run63 did exactly that and got
    `invalid choice: 'VirtioMmioBlock'` from the very same catalog, because that
    snapshot's build-arm-driver.py keys its table by the legacy name while the
    catalog file spells the entry `id = "VirtioMmioBlock"`.  --help prints the
    real choice list, so ask it.
    """
    result = subprocess.run([sys.executable, str(SCRIPT), '--help'],
                            capture_output=True, text=True)
    names = set()
    for block in re.findall(r'\{([^{}]*)\}', result.stdout + result.stderr):
        names.update(part.strip() for part in block.split(',') if part.strip())
    return names


def pick_name():
    names = accepted_names()
    for candidate in (identifier, legacy, 'NativeVirtioBlock', 'VirtioMmioBlock'):
        if candidate in names:
            return candidate
    fail('build-arm-driver.py accepts none of %r (it lists: %s)'
         % ([identifier, legacy], ', '.join(sorted(names)) or 'nothing'))


name = pick_name()
print('virtio: build-arm-driver.py takes %r' % name)

# Where the sources actually are: prefer the catalog path, else the name we are
# about to pass (build-arm-driver.py derives the directory from it), else anything
# under drivers/ that carries the file.
declared = entry.get('path') or ''
places = [TREE / declared] if declared else []
places += [TREE / 'drivers' / name, TREE / 'drivers' / 'VirtioMmioBlock']
places += sorted(path.parent for path in (TREE / 'drivers').glob('*/' + SOURCE))
source = next((path for path in places if (path / SOURCE).is_file()), None)
if source is None:
    fail('no directory under drivers/ contains %s (looked at %s)'
         % (SOURCE, ', '.join(str(path.relative_to(TREE)) for path in places)))

holder = TREE / 'drivers' / name
if holder != source:
    if holder.is_symlink():
        holder.unlink()
    if not holder.exists():
        holder.symlink_to(source)
        print('virtio: linked drivers/%s -> %s' % (name, source.relative_to(TREE)))

before = {path: path.stat().st_mtime_ns for path in TARGETS if path.is_file()}
started = time.time()
result = subprocess.run([sys.executable, str(SCRIPT), name], cwd=str(TREE))
if result.returncode != 0:
    fail('build-arm-driver.py %s exited %d' % (name, result.returncode))

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
