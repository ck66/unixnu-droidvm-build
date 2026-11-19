"""Last-resort source fixups for xnu-8796 that no patch in the series covers.

Run from the tree root (out/<board>/tree), after the series is applied and before `make`.
Every step prints what it did and does nothing on a second run, so this is safe to repeat.

Three things 8796 needs and the public tarball / the 10063-era series do not give it:

1. Private firehose headers. Apple's 8796 tarball ships only `libkern/firehose/
   firehose_types_private.h` and `libkern/os/firehose.h`; `os/log.c` and `bsd/kern/subr_log.c`
   also include `firehose/{tracepoint,chunk,ioctl,}private.h` and `os/firehose_buffer_private.h`.
   10063 ships the first four, and `os/firehose_buffer_private.h` lives in libdispatch (its
   `libfirehose_kernel` target is what installs it for a normal xnu build). They are copied in
   here from pinned tags so the tree is self-contained.
2. `bool reserved10;` twice in `osfmk/arm/pmap/pmap.h` (Rosetta branch and PAC branch). With
   both features off the struct has a duplicate member and nothing compiles.
3. `osfmk/arm/pmap/pmap.c` includes and calls the PPL/SART bootstrap unconditionally. Those
   headers are not published at all; the series' `non-apple-iommu-bootstrap.patch` adds the
   `XNU_MONITOR_*` guards, but its context no longer matches 8796. Without the IOMMU tables
   this board has nothing to bootstrap, so the calls become no-ops.

Usage: python3 fixup-8796.py            (from out/<board>/tree)
"""
from pathlib import Path
import os
import sys
import urllib.request

# Pinned sources. 10063 for the firehose private headers (same wire format, still shipped),
# libdispatch for the buffer header (that is where a normal xnu build gets it from).
HEADERS = {
    'libkern/firehose/tracepoint_private.h':
        'apple-oss-distributions/xnu/xnu-10063.141.1/libkern/firehose/tracepoint_private.h',
    'libkern/firehose/chunk_private.h':
        'apple-oss-distributions/xnu/xnu-10063.141.1/libkern/firehose/chunk_private.h',
    'libkern/firehose/ioctl_private.h':
        'apple-oss-distributions/xnu/xnu-10063.141.1/libkern/firehose/ioctl_private.h',
    'libkern/firehose/private.h':
        'apple-oss-distributions/xnu/xnu-10063.141.1/libkern/firehose/private.h',
    'libkern/os/firehose_buffer_private.h':
        'apple-oss-distributions/libdispatch/libdispatch-1477.100.9/os/firehose_buffer_private.h',
}

TOKEN = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')


def fetch_headers():
    work = Path('.cache/xnu-work')
    if not work.is_dir():
        print('fixup: no xnu work tree, skip headers')
        return 0
    added = 0
    sdk_include = Path('.cache/fakeroot/usr/local/include')
    for relative, repo_path in HEADERS.items():
        from_libdispatch = relative == 'libkern/os/firehose_buffer_private.h'
        target = (sdk_include / relative[len('libkern/'):]) if from_libdispatch else (work / relative)
        if target.exists():
            continue
        url = 'https://raw.githubusercontent.com/' + repo_path
        request = urllib.request.Request(url, headers={'User-Agent': 'unixnu-ios16-fixup'})
        if TOKEN:
            request.add_header('Authorization', 'Bearer ' + TOKEN)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
        except Exception as error:                     # noqa: BLE001 - report, keep going
            print('fixup: %s FAILED (%s)' % (relative, error))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        added += 1
        print('fixup: added %s (%d bytes)' % (relative, len(data)))
        if from_libdispatch:
            print('fixup: placed %s in the kernel SDK include path' % target)
    if not added:
        print('fixup: private firehose headers already present')
    return added


def dedupe_pmap_field():
    p = Path('.cache/xnu-work/osfmk/arm/pmap/pmap.h')
    if not p.is_file():
        print('fixup: no pmap.h, skip')
        return False
    t = p.read_text()
    n = t.count('bool reserved10;')
    if n < 2:
        print('fixup: pmap.h reserved10 x%d, nothing to do' % n)
        return False
    i = t.rfind('bool reserved10;')
    t = t[:i] + 'bool reserved13;' + t[i + len('bool reserved10;'):]
    p.write_text(t)
    print('fixup: pmap.h reserved10 %d -> %d' % (n, t.count('bool reserved10;')))
    return True


def stub_ppl_iommu():
    """Only when the PPL/SART headers are genuinely absent from the tree."""
    work = Path('.cache/xnu-work')
    if (work / 'osfmk/arm64/ppl/sart.h').is_file():
        print('fixup: sart.h present, series guard applied')
        return False
    p = work / 'osfmk/arm/pmap/pmap.c'
    if not p.is_file():
        print('fixup: no pmap.c, skip')
        return False
    t = p.read_text()
    if '/* unixnu: PPL/SART bootstrap stubbed */' in t:
        print('fixup: PPL/SART already stubbed')
        return False
    changed = False
    for header, call in (('<arm64/ppl/sart.h>', 'sart_bootstrap'), ('<arm64/ppl/uat.h>', 'uat_bootstrap')):
        line = '#include %s\n' % header
        if line in t:
            t = t.replace(line, '/* unixnu: PPL/SART bootstrap stubbed */\n', 1)
            changed = True
        if '%s();' % call in t:
            t = t.replace('%s();' % call, '/* %s(); -- no IOMMU tables on this board */' % call, 1)
            changed = True
    if changed:
        p.write_text(t)
        print('fixup: PPL/SART includes and calls stubbed out in pmap.c')
    return changed


def place_late():
    """After installhdrs the kernel compiles against EXPORT_HDRS (and installhdrs re-creates
    the SDK fakeroot), so the libdispatch header has to be re-placed right before the build."""
    source = Path('.cache/fakeroot/usr/local/include/os/firehose_buffer_private.h')
    if not source.is_file():
        source = Path('.cache/xnu-work/libkern/os/firehose_buffer_private.h')
    if not source.is_file():
        print('late: no firehose_buffer_private.h to place')
        return False
    data = source.read_bytes()
    placed = 0
    for export in Path('build').rglob('EXPORT_HDRS'):
        target = export / 'libkern/os/firehose_buffer_private.h'
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        placed += 1
        print('late: %s' % target)
    sdk = Path('.cache/fakeroot/usr/local/include/os/firehose_buffer_private.h')
    if not sdk.is_file():
        sdk.parent.mkdir(parents=True, exist_ok=True)
        sdk.write_bytes(data)
        print('late: %s' % sdk)
    print('late: %d EXPORT_HDRS copies' % placed)
    return placed > 0


def main():
    if '--late' in sys.argv:
        place_late()
        return 0
    fetch_headers()
    dedupe_pmap_field()
    stub_ppl_iommu()
    print('fixup: done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
