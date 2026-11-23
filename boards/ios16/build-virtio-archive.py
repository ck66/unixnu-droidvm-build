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
   uses with an audit record of the numbers.  The class dispatch table is found
   structurally -- sixteen zero bytes (offset-to-top, null typeinfo), then the
   two abstract-destructor slots pointing at BRK traps -- and its length must
   equal the slot count of the driver's own vtable, which is what pairs each
   inherited method name with the address in the kext.  Every number is checked;
   anything that cannot be resolved uniquely aborts the step, because a guessed
   address is worse than a failed build.

   The order is what forces two runs.  This step runs *before* the kernel link --
   the kernel link consumes this archive -- so at binding time there is no image
   that says where the kext will land.  Without a record the aliases therefore
   point at a stub in this object: in range for every branch and page reference,
   so the link succeeds and produces the image; `--measure <kernel>` then reads
   the placement out of it and writes the record (boards/ios16/virtio-record.json).
   With a record present the aliases get the real addresses.  `--verify <kernel>`
   re-resolves against the finished image and compares, so a kernel whose kext
   moved is reported instead of producing a fault somewhere unrelated.
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
# Staged next to this script by the workflow from boards/ios16/virtio-record.json: the
# addresses a previous run measured, because this step runs before the kernel is linked.
RECORD = TREE / 'scripts' / 'virtio-record.json'


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


def storage_kext(data, image):
    """The IOStorageFamily Mach-O and its segments, located through __PRELINK_INFO."""
    module = prelink()
    info = next((segment for segment in image['segments'] if segment['name'] == '__PRELINK_INFO'), None)
    if info is None:
        fail('%s has no __PRELINK_INFO: it is the raw link output, not the prelinked image'
             % 'the kernel')
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


def needed_symbols(object_path, slots):
    """Which undefined symbols have to come from the kext, and where each one lives in it.

    A slot index is the driver's own numbering, so it is also how `--measure` and `--verify`
    find the address again in a kernel image.
    """
    imports = {item[0] for item in symbols_of(object_path) if item[2] == 1}
    where = {}
    for index, symbol in enumerate(slots):
        if symbol and symbol.startswith(CLASS):
            where[symbol] = ['slot', index]
    for symbol, kind in {
        '__ZTV20IOBlockStorageDevice': 'table',
        CLASS + 'D2Ev': 'destructor',
        CLASS + 'C2EPK11OSMetaClass': 'constructor',
        CLASS + '10gMetaClassE': 'metaclass',
    }.items():
        where[symbol] = ['special', kind]
    selected = {name: where[name] for name in where if name in imports}
    missed = sorted(name for name in imports if name.startswith(CLASS) and name not in selected)
    note('%d imports, %d bound to %s (%d slots, %d special)'
         % (len(imports), len(selected), BUNDLE,
            sum(1 for kind, _ in selected.values() if kind == 'slot'),
            sum(1 for kind, _ in selected.values() if kind == 'special')))
    for name in missed:
        note('  not in the class table and not exported by the kernel: ' + name)
    if missed:
        fail('%d IOBlockStorageDevice symbols are neither virtual nor derivable; a non-virtual '
             'member cannot be located from the class table' % len(missed))
    return selected


def resolve(kernel_path, plan):
    """Address of every needed symbol, read out of a prelinked kernel image."""
    data, image = load(kernel_path)
    kext, segments = storage_kext(data, image)
    text = segments['__TEXT_EXEC']
    table, pointers = dispatch_table(data, image, segments, plan['length'])
    special = {
        'table': lambda: table,
        'destructor': lambda: destructor(data, image, pointers),
        'constructor': lambda: constructor(data, image, text, table),
        'metaclass': lambda: metaclass(data, image, text, pointers),
    }
    done = {}
    values = {}
    for name, (kind, where) in sorted(plan['needed'].items()):
        if kind == 'slot':
            if where >= len(pointers):
                fail('%s is slot %d of %d' % (name, where, len(pointers)))
            values[name] = pointers[where]
        else:
            if where not in done:
                done[where] = special[where]()
            values[name] = done[where]
    note('resolved %d symbols against %s (table %#x, %d slots)'
         % (len(values), kernel_path.name, table, len(pointers)))
    return values


def aliases(plan, values):
    """The assembly that defines the driver's imports: absolute addresses, or placeholders."""
    if values is None:
        # No kernel exists yet at archive time -- the kernel link consumes this archive, so a
        # record can only come from a previous run.  Point every import at a stub in this same
        # object instead: in range for every branch and page reference, so the link succeeds and
        # produces the image `--measure` needs.
        lines = ['.section __TEXT,__text,regular,pure_instructions',
                 '.p2align 2', 'virtio_placeholder:', '\tret',
                 '.section __TEXT,__const', '.p2align 3',
                 'virtio_placeholder_data:', '\t.space 16']
        for name, (kind, where) in sorted(plan['needed'].items()):
            data = kind == 'special' and where in ('table', 'metaclass')
            lines += ['.globl %s' % name,
                      '.set %s, %s' % (name, 'virtio_placeholder_data' if data
                                       else 'virtio_placeholder')]
        return '\n'.join(lines) + '\n'
    missing = sorted(set(plan['needed']) - set(values))
    if missing:
        fail('the record has no address for %s' % ', '.join(missing))
    return ''.join('.globl %s\n.set %s, %#x\n' % (name, name, values[name])
                   for name in sorted(values))


def emit(archive, plan, values):
    assembly = archive.parent / 'original-bindings.S'
    assembly.write_text('# generated by boards/ios16/build-virtio-archive.py\n'
                        + aliases(plan, values))
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
    note('%s into %s (%d bytes)'
         % ('bound' if values else 'placeholder-bound', archive.name, archive.stat().st_size))


def bind(archive, object_path):
    slots = derived_vtable(object_path)
    plan = {'length': len(slots), 'needed': needed_symbols(object_path, slots)}
    if RECORD.is_file():
        record = json.loads(RECORD.read_text())
        if record.get('length') != plan['length']:
            fail('the record was made for a %s-slot table, the driver has %d'
                 % (record.get('length'), plan['length']))
        note('using the recorded addresses in %s' % RECORD.name)
        emit(archive, plan, record['aliases'])
        return False
    note('no %s yet, so this run only links: run --measure on the image it produces and '
         'commit the result' % RECORD.name)
    emit(archive, plan, None)
    pending = archive.parent / 'virtio-needs.json'
    pending.write_text(json.dumps(plan, indent=2) + '\n')
    note('wrote %s' % pending.name)
    return True


def measure(kernel_path, pending_path, output_path):
    """Write the record that the next run's archive will be bound with."""
    plan = json.loads(pending_path.read_text())
    values = resolve(kernel_path, plan)
    record = {'length': plan['length'], 'kext': BUNDLE, 'needed': plan['needed'],
              'aliases': values, 'source': kernel_path.name}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    for name in sorted(values):
        note('  %-52s %#x' % (name, values[name]))
    note('wrote %s' % output_path)


def verify(kernel_path, record_path):
    """Re-resolve against the finished kernel and compare with what the archive was bound to."""
    record = json.loads(record_path.read_text())
    actual = resolve(kernel_path, record)
    stale = [(name, value, record['aliases'].get(name))
             for name, value in actual.items() if record['aliases'].get(name) != value]
    for name in sorted(actual):
        note('  %-52s %#x (bound %s)' % (name, actual[name],
                                         '%#x' % record['aliases'][name]
                                         if name in record['aliases'] else 'nothing'))
    if stale:
        for name, value, was in stale:
            note('  STALE %s: now %#x, bound %s' % (name, value, '%#x' % was if was else 'nothing'))
        fail('%d bindings moved after the archive was built; the linked kernel is not usable'
             % len(stale))
    note('all %d bindings still current' % len(actual))


def main():
    started = time.time()
    argv = sys.argv[1:]
    output = None
    while '--out' in argv:
        at = argv.index('--out')
        if at + 1 >= len(argv):
            fail('--out needs a path')
        output = Path(argv[at + 1])
        del argv[at:at + 2]
    if '--measure' in argv or '--verify' in argv:
        mode = '--measure' if '--measure' in argv else '--verify'
        rest = [item for item in argv if item != mode]
        if not rest:
            fail('%s needs the kernel image' % mode)
        kernel_path = Path(rest[0])
        if not kernel_path.is_file():
            fail('no %s' % kernel_path)
        given = [Path(item) for item in rest[1:]]
        if mode == '--measure':
            pending = given[0] if given else next(
                iter(sorted((TREE / 'build').glob('*/virtio-needs.json'))), None)
            if pending is None or not pending.is_file():
                fail('no virtio-needs.json under build/ to measure for')
            measure(kernel_path, pending, output or Path('virtio-record.json'))
        else:
            record = given[0] if given else RECORD
            if not record.is_file():
                fail('no record at %s; this archive was bound with placeholders' % record)
            verify(kernel_path, record)
        return
    archive, object_file = compile_driver()
    placeholders = bind(archive, object_file)
    for target in TARGETS:
        if target == archive:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        shutil.copy2(archive, target)
        note('copied to %s' % target.relative_to(TREE))
    note('done in %.1fs%s' % (time.time() - started,
                              ', placeholders only' if placeholders else ''))


if __name__ == '__main__':
    main()
