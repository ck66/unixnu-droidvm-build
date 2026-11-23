#!/usr/bin/env python3
"""Build the VirtIO MMIO block transport and bind it to the prelinked IOStorageFamily.

Run from the tree's scripts/ directory by `unixnu build kernel` (the board.toml
[kernel] archives list names it), i.e. cwd is the tree root.

Two separate problems, both solved here.

1. Naming.  components.toml spells the entry `id = "VirtioMmioBlock"` with
   `legacy_name = "NativeVirtioBlock"`, but this tree's build-arm-driver.py keys
   its driver table by the legacy name, so run62's `VirtioMmioBlock` died in
   argparse and run63's catalog read still died with the same invalid choice.
   The choice list is in `--help`, so ask it.

2. Linking.  IOBlockStorageDevice is not part of the kernel; it lives inside the
   prelinked com.apple.iokit.IOStorageFamily kext, whose symbols the kernel link
   cannot see.  run64 therefore failed with "Undefined symbols ... vtable for
   IOBlockStorageDevice".  A kext in the prelinked image keeps the address it was
   linked at, and the driver is compiled against the same class declaration, so
   an absolute alias is a valid binding:

       .globl __ZTV20IOBlockStorageDevice
       .set   __ZTV20IOBlockStorageDevice, <the kext's dispatch table>

   That is the mechanism the 17.7.11 board's build-native-virtio-bindings.py
   uses; here the numbers are read out of the image being built instead of an
   audit record, so they cannot drift from it.  The class dispatch table is found
   structurally -- sixteen zero bytes (offset-to-top, null typeinfo), then the
   two abstract-destructor slots pointing at BRK traps -- and its length must
   equal the slot count of the driver's own vtable, which is what pairs each
   inherited method name with the address in the kext.  Every number is checked;
   anything that cannot be resolved uniquely aborts the step, because a guessed
   address is worse than a failed build.

   The kext layout is read from the kernel this board links before the archive step
   runs, so it is the layout of this run, not a recorded one.  A second link then
   force-loads this archive; if that moved anything, the baked addresses would be
   stale.  `--verify <kernel>` re-resolves against the finished image and compares,
   so drift is reported in the log instead of showing up as a fault at boot.
"""
import json
import re
import shutil
import struct
import subprocess
import sys
import time
import tomllib
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
SCRIPT = TREE / 'scripts' / 'build-arm-driver.py'
CATALOG = TREE / 'components.toml'
SOURCE = 'NativeVirtioBlock.cpp'
BUNDLE = 'com.apple.iokit.IOStorageFamily'
CLASS = '__ZN20IOBlockStorageDevice'
TRAP = b'\x20\x00\x20\xd4'          # brk #0x1, the abstract-destructor slot
TARGETS = [
    TREE / 'build/virtio-mmio-block/libVirtioMmioBlock.a',
    TREE / 'build/native-virtio-block/libNativeVirtioBlock.a',
]


def fail(message):
    print('virtio: ' + message, file=sys.stderr)
    raise SystemExit(1)


def note(message):
    print('virtio: ' + message)


# ---------------------------------------------------------------- 1. the build

def compile_driver():
    """Build the transport archive and return (archive, object with the driver's ABI)."""
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
    note('catalog id %r, legacy %r, outputs %s' % (identifier, legacy, outputs))

    def accepted_names():
        """The component names this tree's build-arm-driver.py will actually take."""
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
    note('build-arm-driver.py takes %r' % name)

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
            note('linked drivers/%s -> %s' % (name, source.relative_to(TREE)))

    result = subprocess.run([sys.executable, str(SCRIPT), name], cwd=str(TREE))
    if result.returncode != 0:
        fail('build-arm-driver.py %s exited %d' % (name, result.returncode))

    produced = [TREE / path for path in outputs if (TREE / path).is_file()]
    if not produced:
        produced = sorted((path for path in (TREE / 'build').glob('**/*irtio*.a') if path.is_file()),
                          key=lambda path: path.stat().st_mtime_ns, reverse=True)[:1]
    if not produced:
        fail('build-arm-driver.py %s produced no archive under build/' % name)
    archive = produced[0]
    object_file = archive.parent / (legacy + '.o')
    if not object_file.is_file():
        candidates = sorted(archive.parent.glob('*.o'))
        if not candidates:
            fail('no object next to %s to read the driver ABI from' % archive)
        object_file = candidates[0]
    note('built %s (%d bytes), object %s' % (archive.relative_to(TREE), archive.stat().st_size,
                                             object_file.name))
    return archive, object_file


# ------------------------------------------------------------- 2. the bindings

def prelink():
    candidates = [TREE / 'kits' / 'NativePrelink']
    candidates += [parent / 'kits' / 'NativePrelink' for parent in list(TREE.parents)[:3]]
    library = next((path for path in candidates if (path / 'native_prelink.py').is_file()), None)
    if library is None:
        fail('no native_prelink.py under %s' % ', '.join(str(path) for path in candidates))
    if str(library) not in sys.path:
        sys.path.insert(0, str(library))
    import native_prelink
    return native_prelink


def load(path):
    """(bytes, parsed Mach-O) for a file."""
    data = path.read_bytes()
    return data, prelink().macho(data)


def at(data, image, va, length):
    return prelink().backed_offset(image['segments'], va, length, len(data))


def read(data, image, va, length):
    start = at(data, image, va, length)
    return data[start:start + length]


def symbols_of(object_path):
    """[(name, value, kind)] from a Mach-O symtab."""
    data = object_path.read_bytes()
    image = prelink().macho(data)
    offset = next(command_at for command, command_at, _ in image['commands'] if command == 2)
    symoff, count, stroff, _ = struct.unpack_from('<4I', data, offset + 8)
    out = []
    for index in range(count):
        strx, kind, _, _, value = struct.unpack_from('<IBBHQ', data, symoff + index * 16)
        end = data.index(b'\0', stroff + strx)
        out.append((data[stroff + strx:end].decode('utf-8', 'replace'), value, kind))
    return out


def derived_vtable(object_path):
    """The driver's own vtable: (name, [symbol name per function slot]), straight from its relocations.

    The slot count is the ABI's own statement of how many virtual slots IOBlockStorageDevice
    has, and that is what selects the right dispatch table in the kext, so it is read as a
    contiguous run from the first slot instead of from the last relocation in the section
    (other classes' vtables live in the same section).
    """
    data = object_path.read_bytes()
    image = prelink().macho(data)
    symbols = symbols_of(object_path)
    symbol = next((item for item in symbols if item[0].startswith('__ZTV') and 'VirtioBlock' in item[0]), None)
    if symbol is None:
        fail('the driver object exports no vtable')
    vtable = symbol[1]
    section = next((item for segment in image['segments'] for item in segment['sections']
                    if item['va'] <= vtable < item['va'] + item['size']), None)
    if section is None:
        fail('cannot find the vtable inside a section')
    reloff, count = struct.unpack_from('<II', data, section['command_offset'] + 56)
    named = {}
    for index in range(count):
        address, word = struct.unpack_from('<II', data, reloff + index * 8)
        if word >> 28 or (word >> 25) & 3 != 3:      # only 8-byte UNSIGNED pointer slots
            continue
        offset = section['va'] + address - (vtable + 16)
        if offset < 0 or offset % 8:
            continue
        named[offset // 8] = symbols[word & 0xffffff][0] if word >> 27 & 1 else None
    run = 0
    while run in named:
        run += 1
    length = max(run, max(named) + 1 if named else 0)
    holes = [slot for slot in range(length) if slot not in named]
    if holes:
        note('driver vtable %s: %d slots, %d without a relocation (first at %d)'
             % (symbol[0], length, len(holes), holes[0]))
    inherited = sum(1 for name in named.values() if name and name.startswith(CLASS))
    note('driver vtable %s: %d slots, %d named, %d of them inherited from IOBlockStorageDevice'
         % (symbol[0], length, sum(1 for name in named.values() if name), inherited))
    return [named.get(index) for index in range(length)]


def kernel_image():
    for name in ('mach.development.bcm2837.unstripped',
                 'mach.development.bcm2837.unstripped.noctf'):
        path = TREE / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837' / name
        if path.is_file():
            note('kernel image %s' % path.name)
            return load(path)
    fail('no linked kernel yet; the bindings need its kext placement')


def storage_kext(data, image):
    """The IOStorageFamily Mach-O and its segments, located through __PRELINK_INFO."""
    module = prelink()
    info = next(segment for segment in image['segments'] if segment['name'] == '__PRELINK_INFO')
    start = at(data, image, info['va'], info['filesize'])
    metadata = module.unserialize_xml(data[start:start + info['filesize']])
    item = next((entry for entry in metadata['_PrelinkInfoDictionary']
                 if entry.get('CFBundleIdentifier') == BUNDLE), None)
    if item is None:
        fail('the kernel image has no %s entry' % BUNDLE)
    kext = module.macho(data, at(data, image, item['_PrelinkExecutableLoadAddr'], 32))
    segments = {segment['name']: segment for segment in kext['segments'] if segment['size']}
    note('%s: %s' % (BUNDLE, ', '.join('%s %#x+%#x' % (key, value['va'], value['size'])
                                       for key, value in sorted(segments.items()))))
    return kext, segments


def dispatch_table(data, image, segments, length):
    """The class dispatch table: 16 zero bytes, then `length` pointers, first two at BRK traps."""
    const = segments['__DATA_CONST']
    blob = read(data, image, const['va'], const['size'])

    def scan(width):
        hits = []
        for offset in range(0, len(blob) - (width + 2) * 8 + 1, 8):
            if blob[offset:offset + 16] != bytes(16):
                continue
            pointers = struct.unpack_from('<%dQ' % width, blob, offset + 16)
            if any(pointer < 0xFFFFFFF000000000 for pointer in pointers):
                continue
            try:
                if not all(read(data, image, pointer, 4) == TRAP for pointer in pointers[:2]):
                    continue
            except ValueError:
                continue
            hits.append((const['va'] + offset, pointers))
        return hits

    found = scan(length)
    if len(found) != 1:
        # Say what the image actually contains, so a mismatch is one log away from a fix.
        shapes = []
        for width in range(length - 24, length + 25):
            hits = scan(width)
            if hits:
                shapes.append('%d slots at %s' % (width, ', '.join('%#x' % hit[0] for hit in hits)))
        note('dispatch tables present with 16 zero bytes and two BRK slots: %s'
             % ('; '.join(shapes) or 'none'))
        fail('found %d %d-slot dispatch tables in %s __DATA_CONST, need exactly one'
             % (len(found), length, BUNDLE))
    table, pointers = found[0]
    note('dispatch table %#x, %d slots' % (table, length))
    return table, pointers


def metaclass(data, image, text, pointers):
    """The class object, through the virtual getMetaClass(): adrp/add/ret returning it."""
    candidates = set()
    for pointer in pointers:
        if not (text['va'] <= pointer < text['va'] + text['size']):
            continue
        try:
            a, add, ret = struct.unpack('<3I', read(data, image, pointer, 12))
        except ValueError:
            continue
        if a & 0x9F00001F != 0x90000000 or add & 0xFFC003FF != 0x91000000 or ret != 0xD65F03C0:
            continue
        immediate = ((a >> 5 & 0x7FFFF) << 2) | (a >> 29 & 3)
        if immediate & 0x100000:
            immediate -= 0x200000
        candidates.add((pointer & ~4095) + immediate * 4096 + (add >> 10 & 4095))
    if len(candidates) != 1:
        fail('found %d metaclass getters, need exactly one: %s'
             % (len(candidates), ', '.join('%#x' % item for item in sorted(candidates))))
    return candidates.pop()


def constructor(data, image, text, table):
    """The protected constructor: stores the dispatch table into the object at [x0]."""
    hits = []
    for pointer in range(text['va'], text['va'] + text['size'] - 32, 4):
        try:
            words = struct.unpack('<8I', read(data, image, pointer, 32))
        except ValueError:
            continue
        if (words[:2] != (0xA9BF7BFD, 0x910003FD) or words[2] & 0xFC000000 != 0x94000000 or
                words[3] & 0x9F00001F != 0x90000008 or words[4] & 0xFFC003FF != 0x91000108 or
                words[5:] != (0xF9000008, 0xA8C17BFD, 0xD65F03C0)):
            continue
        immediate = ((words[3] >> 5 & 0x7FFFF) << 2) | (words[3] >> 29 & 3)
        if immediate & 0x100000:
            immediate -= 0x200000
        stored = ((pointer + 12) & ~4095) + immediate * 4096 + (words[4] >> 10 & 4095)
        if stored == table + 16:
            hits.append(pointer)
    if len(hits) != 1:
        fail('found %d constructors storing %#x, need exactly one' % (len(hits), table + 16))
    return hits[0]


def destructor(data, image, pointers):
    """The base object destructor: the branch right in front of the first abstract trap."""
    target = pointers[0] - 4
    word = struct.unpack('<I', read(data, image, target, 4))[0]
    if word & 0xFC000000 != 0x14000000:
        fail('the word before the abstract destructor is %#x, not a branch' % word)
    return target


def bind(archive, object_path):
    slots = derived_vtable(object_path)
    kernel_data, kernel = kernel_image()
    exports = prelink().exports(kernel_data, kernel)
    by_name = {name: va for va, names in exports.items() for name in names}
    kext, segments = storage_kext(kernel_data, kernel)
    text = segments['__TEXT_EXEC']
    table, pointers = dispatch_table(kernel_data, kernel, segments, len(slots))

    aliases = {}
    for index, symbol in enumerate(slots):
        if symbol is None or not symbol.startswith(CLASS):
            continue
        if symbol in by_name:
            continue  # the kernel already exports it
        aliases[symbol] = pointers[index]

    def exported(symbol):
        """Names come out of an nlist either way, so compare both spellings."""
        return (symbol in by_name or symbol[1:] in by_name or '_' + symbol in by_name)

    imports = [item[0] for item in symbols_of(object_path) if item[2] == 1]
    special = {
        '__ZTV20IOBlockStorageDevice': lambda: table,
        CLASS + 'D2Ev': lambda: destructor(kernel_data, kernel, pointers),
        CLASS + 'C2EPK11OSMetaClass': lambda: constructor(kernel_data, kernel, text, table),
        CLASS + '10gMetaClassE': lambda: metaclass(kernel_data, kernel, text, pointers),
    }
    for symbol, value in special.items():
        if symbol in imports:
            aliases[symbol] = value()

    unknown = sorted({symbol for symbol in imports
                      if symbol not in aliases and not exported(symbol)})
    kernel_side = len(set(imports) - set(aliases) - set(unknown))
    note('%d imports: %d from %s, %d from the kernel, %d unknown'
         % (len(set(imports)), len(aliases), BUNDLE, kernel_side, len(unknown)))
    for symbol in sorted(aliases):
        note('  %-52s %#x' % (symbol, aliases[symbol]))
    for symbol in unknown:
        note('  unknown: ' + symbol)
    if unknown:
        fail('%d imports neither the kext nor the kernel defines; not linking a guess'
             % len(unknown))

    assembly = archive.parent / 'original-bindings.S'
    assembly.write_text('# generated by boards/ios16/build-virtio-archive.py\n'
                        + ''.join('.globl %s\n.set %s, %#x\n' % (item, item, value)
                                  for item, value in sorted(aliases.items())))
    compiled = archive.parent / 'original-bindings.o'
    result = subprocess.run(['xcrun', 'clang', '-target', 'arm64-apple-ios17.0', '-Werror',
                             '-Wno-incompatible-sysroot', '-c', str(assembly), '-o', str(compiled)],
                            capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        fail('assembling the aliases failed')
    result = subprocess.run(['xcrun', 'ar', 'rcs', str(archive), str(compiled)],
                            capture_output=True, text=True)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        fail('appending the aliases to %s failed' % archive)
    note('bound into %s (%d bytes)' % (archive.name, archive.stat().st_size))
    manifest = archive.parent / 'virtio-bindings.json'
    manifest.write_text(json.dumps({
        'table': table, 'length': len(slots), 'kext': BUNDLE,
        'aliases': aliases}, indent=2) + '\n')
    note('wrote %s' % manifest.name)
    return archive


def verify(kernel_path, manifest_path):
    """Re-resolve against the finished kernel and compare with what was linked in.

    The archive step reads the kernel from the run that has not had virtio force-loaded yet;
    if the kext layout moved between that link and the final one, every baked address would
    be stale and the kernel would fault somewhere unrelated.  This says so directly.
    """
    report = json.loads(manifest_path.read_text())
    kernel_data, kernel = load(kernel_path)
    kext, segments = storage_kext(kernel_data, kernel)
    text = segments['__TEXT_EXEC']
    table, pointers = dispatch_table(kernel_data, kernel, segments, report['length'])
    actual = {
        '__ZTV20IOBlockStorageDevice': table,
        CLASS + 'D2Ev': destructor(kernel_data, kernel, pointers),
        CLASS + 'C2EPK11OSMetaClass': constructor(kernel_data, kernel, text, table),
        CLASS + '10gMetaClassE': metaclass(kernel_data, kernel, text, pointers),
    }
    stale = [(name, value, report['aliases'].get(name))
             for name, value in actual.items()
             if report['aliases'].get(name) != value]
    note('%s: dispatch table %#x, %d slots (linked with %#x)'
         % (kernel_path.name, table, report['length'], report['table']))

    def shown(value):
        return ('%#x' % value) if isinstance(value, int) else str(value)

    for name, value in sorted(actual.items()):
        note('  %-52s %#x (linked %s)' % (name, value, shown(report['aliases'].get(name))))
    if stale:
        for name, value, was in stale:
            note('  STALE %s: now %#x, linked %s' % (name, value, shown(was)))
        fail('the kext moved after the archive was built; the linked kernel is not usable')
    note('bindings still current')


def main():
    started = time.time()
    if '--verify' in sys.argv:
        index = sys.argv.index('--verify')
        if index + 1 >= len(sys.argv):
            fail('--verify needs the kernel image to check')
        kernel_path = Path(sys.argv[index + 1])
        if not kernel_path.is_file():
            fail('no %s to verify against' % kernel_path)
        manifest = next((Path(item) for item in sys.argv
                         if item.endswith('virtio-bindings.json')), None)
        if manifest is None:
            found = sorted((TREE / 'build').glob('*/virtio-bindings.json'))
            if not found:
                fail('no virtio-bindings.json under build/ to verify against')
            manifest = found[0]
        note('verifying %s against %s' % (kernel_path, manifest))
        verify(kernel_path, manifest)
        return
    archive, object_file = compile_driver()
    bind(archive, object_file)
    for target in TARGETS:
        if target == archive:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        shutil.copy2(archive, target)
        note('copied to %s' % target.relative_to(TREE))
    note('done in %.1fs' % (time.time() - started))


if __name__ == '__main__':
    main()
