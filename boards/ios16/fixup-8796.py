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


SHIM = """/* Synthesised for this board: xnu-8796's pmap.c includes the closed per-SoC PPL/IOMMU
 * headers (arm64/ppl/sart.h, arm64/ppl/uat.h), which Apple has never published. Everything it
 * takes from them is an IOMMU lock-down hook, and this board has no IOMMU tables to lock --
 * so they collapse to nothing. Macros, not prototypes: the call sites pass different argument
 * counts under XNU_MONITOR and there is nothing to implement.
 */
#pragma once

#define sart_bootstrap()               do { } while (0)
#define uat_bootstrap()                do { } while (0)
#define ptep_get_iommu(...)            ((void *)0)
#define pmap_ppl_lockdown_page(...)    do { } while (0)
#define pmap_ppl_lockdown_pages(...)   do { } while (0)
#define pmap_ppl_lockdown_page_with_prot(...) do { } while (0)
#define pmap_ppl_unlockdown_page(...)  do { } while (0)
#define pmap_ppl_unlockdown_pages(...) do { } while (0)
#define pmap_ppl_unlockdown_page_locked(...)  do { } while (0)

/* pmap.c calls this at line ~6315 and defines it `static` at ~11940, so the declaration it
 * used to get from the closed header has to be here or the call becomes an implicit
 * declaration (and then the static definition is a conflicting redefinition). */
static void pmap_phys_write_disable(vm_address_t va);
"""


def write_ppl_shims():
    """Only when the PPL/SART headers are genuinely absent (they always are)."""
    work = Path('.cache/xnu-work')
    if not work.is_dir():
        print('fixup: no xnu work tree, skip PPL shims')
        return False
    target_dir = work / 'osfmk/arm64/ppl'
    if (target_dir / 'sart.h').is_file():
        print('fixup: ppl/sart.h present, series guard applied')
        return False
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ('sart.h', 'uat.h'):
        (target_dir / name).write_text(SHIM)
    print('fixup: wrote osfmk/arm64/ppl/{sart.h,uat.h} (no-op IOMMU hooks)')
    return True


SHIM_C = """/* Synthesised for this board: two symbols that 8796's sources call but that live in Apple's
 * closed per-SoC / PPL sources. Neither does anything useful here.
 *
 *  - CleanPoC_DcacheRegion_Force_nopreempt_nohid: the "no preemption, no HID" variant of a PoC
 *    dcache clean. This board has coherent DMA and no PPL, so the plain flush is the right
 *    behaviour; the callers only need the lines written back.
 *  - ml_enable_monitor: enables the PPL/monitor mode. There is no monitor on a non-Apple SoC.
 *
 * Do NOT add bsd/ headers to this file: mount.h pulls bsd/sys/types.h and mount_internal.h
 * needs uthread_t, which only exist when a bsd/ translation unit includes them. That is why
 * the vfs peer below lives in vfs_subr.c instead of here (run52 failed on exactly that).
 */
#include <arm/caches_internal.h>
#include <mach/vm_types.h>
#include <stddef.h>

void
CleanPoC_DcacheRegion_Force_nopreempt_nohid(vm_offset_t va, size_t length)
{
	(void)va;
	(void)length;
	FlushPoC_Dcache();
}

void
ml_enable_monitor(void)
{
}

/* Boot-stage word for this board (see add_stage_probes): arm_init writes one value after each
 * bring-up step, in order, so a kernel that stopped says exactly how far it got. It lives in
 * this translation unit because that is the one every configuration links, and __startup_func
 * code may reference it. */
volatile unsigned int droidvm_ios16_stage[8];
"""


def disable_mockfs():
    """mockfs is the ramdisk root the phone-shaped boards use; this board mounts the real APFS
    volumes from the payload. vfs_conf.c guards its mockfs entry with `#if MOCKFS`, so the option
    has to go -- and it may be declared in config/MASTER *or* in the machine config, so clear it
    everywhere. (Compiling mockfs instead is not an option: 8796's public sources need a private
    header.)"""
    import re
    touched = []
    for path in sorted(Path('.cache/xnu-work/config').glob('MASTER*')):
        text = path.read_text()
        kept = [l for l in text.splitlines(True)
                if not re.match(r'^\s*options\s+MOCKFS\b', l)]
        if len(kept) != len(text.splitlines(True)):
            path.write_text(''.join(kept))
            touched.append(path.name)
    # The option is not the only place the macro can come from (turning MASTER off was not
    # enough: the link still pulled mockfs in), so switch the two guarded blocks in vfs_conf.c
    # off directly. They are `#if MOCKFS ... #endif` around the vfs table entry and the
    # vnodeop desc; `#if 0` keeps the file's shape intact instead of editing the entries.
    conf = Path('.cache/xnu-work/bsd/vfs/vfs_conf.c')
    hit = 0
    if conf.is_file():
        text = conf.read_text()
        hit = text.count('#if MOCKFS')
        if hit:
            conf.write_text(text.replace('#if MOCKFS', '#if 0 /* unixnu: this board mounts the payload volumes */'))
    if touched:
        print('fixup: dropped options MOCKFS from %s' % ', '.join(touched))
    if hit:
        print('fixup: disabled %d "#if MOCKFS" block(s) in vfs_conf.c' % hit)
    if not touched and not hit:
        print('fixup: mockfs was already off')
    return bool(touched or hit)


def add_compile_records():
    """xnu-10063 (iOS 17) emits a per-object JSON compile record -- `-MJ $(@:o=o.json)` in
    makedefs/MakeInc.rule -- and the whole UniXNU archive stage is written against it
    (build-arm-driver.py and friends read IOPlatformExpert.cpo.json to replay the kernel's own
    compile command). 8796 only writes `-MF`, so on iOS 16 nothing ever produced those records:
    that, and not a missing tool, is why the archives could not build. Adding the flag is the
    whole fix.
    """
    rule = Path('.cache/xnu-work/makedefs/MakeInc.rule')
    if not rule.is_file():
        print('fixup: no makedefs/MakeInc.rule, skip compile records')
        return False
    text = rule.read_text()
    if '-MJ $(@:o=o.json)' in text:
        print('fixup: compile records already enabled')
        return False
    added = 0
    for old in ('S_RULE_1A=$(_v)${S_KCC} -c ${SFLAGS} -MD -MF $(@:o=d) -MP',
                'C_RULE_1A=$(_v)${KCC} -c ${filter-out ${$@_CFLAGS_RM}, ${CFLAGS} ${CWARNFLAGS}} -MD -MF $(@:o=d) -MP',
                'P_RULE_1A=$(_v)${KC++} -o $@ -c ${CXXFLAGS} ${filter-out ${$@_CFLAGS_RM}, ${CFLAGS} ${CXXWARNFLAGS}} -MD -MF $(@:o=d) -MP'):
        if old in text:
            head, tail = old.split('-MF $(@:o=d) -MP')
            text = text.replace(old, '%s-MF $(@:o=d) -MJ $(@:o=o.json) -MP%s' % (head, tail), 1)
            added += 1
    if not added:
        print('fixup: compile-record rule anchors missing')
        return False
    rule.write_text(text)
    print('fixup: added -MJ $(@:o=o.json) to %d compile rules' % added)
    return True


LIBPTHREAD_PATCH = (
    'diff --git a/kern/kern_support.c b/kern/kern_support.c' "\n"
    'index bdb04ab..c59708d 100644' "\n"
    '--- a/kern/kern_support.c' "\n"
    '+++ b/kern/kern_support.c' "\n"
    '@@ -666,7 +666,9 @@ _bsdthread_register(struct proc *p,' "\n"
    ' \t/* return the supported feature set as the return value. */' "\n"
    ' \t*retval = PTHREAD_FEATURE_SUPPORTED;' "\n"
    ' ' "\n"
    '-#if _PTHREAD_CONFIG_JIT_WRITE_PROTECT' "\n"
    '+/* xnu-8796 (iOS 16) has no proc_get_jit_entitled / proc_get_pthread_jit_allowlist2 in' "\n"
    '+ * struct pthread_kern; this Darwin 23 feature block cannot compile against it. */' "\n"
    '+#if 0' "\n"
    ' \tif (pthread_kern->proc_get_jit_entitled(p)) {' "\n"
    ' \t\t*retval |= PTHREAD_FEATURE_JIT_ENTITLED;' "\n"
    ' \t}' "\n"
)


SOURCE_PTHREAD_BOOTSTRAP = (
    '/* SPDX-License-Identifier: BSD-2-Clause-Patent */' "\n"
    '#include <kern/startup.h>' "\n"
    '#include <kern/debug.h>' "\n"
    '#include <libkern/section_keywords.h>' "\n"
    '#include <mach/kmod.h>' "\n"
    '' "\n"
    'extern kern_return_t pthread_start(kmod_info_t *, void *);' "\n"
    'SECURITY_READ_ONLY_LATE(kern_return_t) source_pthread_status = KERN_FAILURE;' "\n"
    '' "\n"
    'static void' "\n"
    'source_pthread_bootstrap(void)' "\n"
    '{' "\n"
    '\tstatic kmod_info_t info = {' "\n"
    '\t\t.info_version = KMOD_INFO_VERSION,' "\n"
    '\t\t.name = "source-libpthread",' "\n"
    '\t\t.version = "519.120.4",' "\n"
    '\t};' "\n"
    '\tsource_pthread_status = pthread_start(&info, NULL);' "\n"
    '\tif (source_pthread_status != KERN_SUCCESS) {' "\n"
    '\t\tpanic("source libpthread registration failed: %d", source_pthread_status);' "\n"
    '\t}' "\n"
    '}' "\n"
    'STARTUP(EARLY_BOOT, STARTUP_RANK_SECOND, source_pthread_bootstrap);' "\n"
)


def write_source_pthread_bootstrap():
    """build-libpthread-kernel.py compiles bsd/kern/source_pthread_bootstrap.c as its
    bootstrap object. UniXNU's arch series creates that file, but this board takes whyred's
    (8796-adapted) series alone and the arch: entry is not selected, so the archive fails with
    `clang: error: no such file or directory`. It is a 23-line registration shim; the patch
    that adds it changes nothing else, so supplying it directly is equivalent.
    """
    target = Path('.cache/xnu-work/bsd/kern/source_pthread_bootstrap.c')
    if not Path('.cache/xnu-work/bsd/kern').is_dir():
        print('fixup: no xnu work tree, skip pthread bootstrap')
        return False
    if target.is_file():
        print('fixup: source_pthread_bootstrap.c already present')
        return False
    target.write_bytes(SOURCE_PTHREAD_BOOTSTRAP.encode())
    print('fixup: wrote %s' % target)
    return True


def write_libpthread_patch():
    """build-libpthread-kernel.py clones Darwin 23's libpthread and replays the kernel's own
    compile command against it. 8796's `struct pthread_kern` has no proc_get_jit_entitled /
    proc_get_pthread_jit_allowlist2, so kern_support.c:670 fails with
    `no member named 'proc_get_jit_entitled'`. The archive script applies every
    tree/patches/libpthread/*.patch to its work clone -- that is the supported extension point,
    so drop a patch there (sorted last, its hunk is at line 666, clear of the shipped patch's
    559/902+ hunks).
    """
    target = Path('patches/libpthread/zzzz-8796-no-jit-entitled.patch')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == LIBPTHREAD_PATCH.encode():
        print('fixup: libpthread patch already in place')
        return False
    target.write_bytes(LIBPTHREAD_PATCH.encode())
    print('fixup: wrote %s' % target)
    return True


def write_link_shims():
    work = Path('.cache/xnu-work')
    src = work / 'osfmk/arm64/unixnu_shims.c'
    if not work.is_dir():
        print('fixup: no xnu work tree, skip link shims')
        return False
    files = work / 'osfmk/conf/files.arm64'
    if not files.is_file():
        print('fixup: no osfmk/conf/files.arm64, skip link shims')
        return False
    if 'unixnu_shims.c' in files.read_text():
        print('fixup: link shims already registered')
        return False
    src.write_text(SHIM_C)
    text = files.read_text().rstrip('\n') + '\nosfmk/arm64/unixnu_shims.c\tstandard\n'
    files.write_text(text)
    print('fixup: registered osfmk/arm64/unixnu_shims.c')
    return True


VFS_PEER = (
    '\n'
    '/*\n'
    ' * Added for this board. prelink stopped on the 20H392 kernelcache with\n'
    ' *   "Kernel target 0xfffffff00734c520 has 0 native address candidates:\n'
    ' *    [\'_vfs_getvfs_with_vfsops\']"\n'
    ' * A kext inside that cache imports the symbol, so Apple\'s own 16.7.16 build exports it,\n'
    ' * while the public xnu-8796.141.3 drop only has vfs_getvfs(). xnu-10063 is where the\n'
    ' * definition is public -- same lookup, but the mount is dropped unless it belongs to the\n'
    ' * vfsops the caller expected, which keeps a caller from racing a filesystem swap under it.\n'
    ' * In 8796 vfs_getvfs() is already "mount_list_lookupby_fsid(fsid, 0, 0)", so this is a\n'
    ' * faithful port rather than a stub.\n'
    ' */\n'
    'struct mount *\n'
    'vfs_getvfs_with_vfsops(fsid_t *fsid, const struct vfsops * const ops)\n'
    '{\n'
    '\tstruct mount *mp = vfs_getvfs(fsid);\n'
    '\n'
    '\tif (mp != NULL && mp->mnt_op != ops) {\n'
    '\t\tmp = NULL;\n'
    '\t}\n'
    '\treturn mp;\n'
    '}\n'
)

VFS_ANCHOR = (
    'struct mount *\n'
    'vfs_getvfs(fsid_t *fsid)\n'
    '{\n'
    '\treturn mount_list_lookupby_fsid(fsid, 0, 0);\n'
    '}\n'
)


def add_vfs_peer():
    """prelink rebinds the prelinked kexts onto this kernel by name, so every kernel symbol a
    kext imports has to exist here. 20H392 imports vfs_getvfs_with_vfsops, which the public
    8796 sources never defined; it goes into vfs_subr.c because that is the only place where
    mount_list_lookupby_fsid, struct mount and mnt_op are all in scope (see SHIM_C's note)."""
    path = Path('.cache/xnu-work/bsd/vfs/vfs_subr.c')
    if not path.is_file():
        print('fixup: no vfs_subr.c, skip the vfs peer')
        return False
    text = path.read_text()
    if 'vfs_getvfs_with_vfsops' in text:
        print('fixup: vfs_getvfs_with_vfsops already present')
        return False
    if VFS_ANCHOR not in text:
        print('fixup: vfs_getvfs anchor missing, skip the vfs peer')
        return False
    path.write_text(text.replace(VFS_ANCHOR, VFS_ANCHOR + VFS_PEER, 1))
    print('fixup: added vfs_getvfs_with_vfsops to bsd/vfs/vfs_subr.c')
    return True


PRE_INVENTORY_HEAD_OLD = (
    "    extents = [s for module in modules.values() for s in module['segments'] "
    "if s['size'] and s['name'] != '__LINKEDIT']\n"
    "    literal_ranges, instruction_count = [], 0\n"
)

PRE_INVENTORY_HEAD_NEW = (
    "    extents = [s for module in modules.values() for s in module['segments'] "
    "if s['size'] and s['name'] != '__LINKEDIT']\n"
    "    literal_ranges, instruction_count = [], 0\n"
    "    # Words that decode as PC-relative but point into no segment at all: inline data the\n"
    "    # digest table above does not know (see the external-target branch below).\n"
    "    unrecognised = []\n"
)

PRE_EXTERNAL_OLD = (
    "                if not internal:\n"
    "                    raise ValueError(f'External PC-relative encoding at "
    "{segment[\"va\"] + offset:#x}; no instruction rewrite supported')\n"
)

PRE_EXTERNAL_NEW = (
    "                if not internal:\n"
    "                    # Not a reference at all. Everything a prelinked kernel can point at is\n"
    "                    # inside one of these segments, so a decoded PC-relative target that\n"
    "                    # lands in none of them means the word is data. 16.7.16's kexts carry\n"
    "                    # float constant pools the digest table above does not know: the pool at\n"
    "                    # 0xfffffff005b8c1cc is `-32768, -0.538853, 0, 0.0313307` repeated, whose\n"
    "                    # words decode as an ADRP into nowhere (0x...c1cc) and as an ADR into a\n"
    "                    # gap inside the image (0x...c29c). Record the word and keep scanning\n"
    "                    # instead of aborting the whole prelink on data.\n"
    "                    unrecognised.append({'segment': segment['name'],\n"
    "                                         'va': segment['va'] + offset, 'word': word})\n"
    "                    continue\n"
)

PRE_RETURN_OLD = (
    "    return {'pc_relative_encodings_preserved': instruction_count, "
    "'known_inline_data': literal_ranges,\n"
    "            'register_dataflow_qualified': False}\n"
)

PRE_RETURN_NEW = (
    "    return {'pc_relative_encodings_preserved': instruction_count, "
    "'known_inline_data': literal_ranges,\n"
    "            'unrecognised_inline_data': unrecognised,\n"
    "            'register_dataflow_qualified': False}\n"
)


def patch_prelink_tool():
    """The kit aborts the whole prelink when one word inside a kext's __TEXT_EXEC decodes as a
    PC-relative instruction whose target is outside every segment. On 20H392 that is a float
    constant pool, not code, so the abort is a false positive: target outside the image's whole
    span means "data", target merely between extents still means "unsupported reference"."""
    path = Path('kits/NativePrelink/emit_prelink.py')
    if not path.is_file():
        print('fixup: no kits/NativePrelink/emit_prelink.py, skip the prelink tolerance patch')
        return False
    text = path.read_text()
    if 'unrecognised_inline_data' in text:
        print('fixup: prelink tolerance patch already applied')
        return False
    for old, new, what in ((PRE_INVENTORY_HEAD_OLD, PRE_INVENTORY_HEAD_NEW, 'inventory head'),
                           (PRE_EXTERNAL_OLD, PRE_EXTERNAL_NEW, 'external-target abort'),
                           (PRE_RETURN_OLD, PRE_RETURN_NEW, 'inventory return')):
        if old not in text:
            print('fixup: prelink anchor missing (%s), skip the tolerance patch' % what)
            return False
        text = text.replace(old, new, 1)
    path.write_text(text)
    print('fixup: patched the prelink instruction inventory (outside-image targets are data)')
    return True


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
    # The bootstrap pass links without -L$(fakeroot)/usr/local/lib/kernel (that flag is only
    # added for the non-bootstrap pass in build-xnu-4k-baseline.sh), so ld searches the SDK's
    # own usr/local/lib/kernel -- which does not exist. Put the archive where it looks.
    lib = Path('.cache/fakeroot/usr/local/lib/kernel/libfirehose_kernel.a')
    if lib.is_file():
        import subprocess
        sdk_path = subprocess.run(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'],
                                  capture_output=True, text=True).stdout.strip()
        if sdk_path:
            dest = Path(sdk_path) / 'usr/local/lib/kernel'
            try:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / lib.name).write_bytes(lib.read_bytes())
                print('late: %s' % (dest / lib.name))
            except OSError:
                subprocess.run(['sudo', '-n', 'mkdir', '-p', str(dest)], check=False)
                subprocess.run(['sudo', '-n', 'cp', str(lib), str(dest / lib.name)], check=False)
                print('late: sudo copy -> %s' % (dest / lib.name))
        else:
            print('late: no iphoneos sdk path')
    else:
        print('late: no %s' % lib)
    print('late: %d EXPORT_HDRS copies' % placed)
    return placed > 0


def add_stage_probes():
    """arm_init writes one stage word after each bring-up step.

    The prelinked image loads at slide 0 (DarwinBoot's `va=` is __PRELINK_TEXT's vmaddr) and the
    image verifies byte for byte in guest memory, yet _PE_state stays all zero while one vCPU
    burns ~54% -- so the kernel starts executing and stops before PE_init finishes. Guessing
    which step is hopeless here: the whole boot happens inside arm_init, and every one of its
    steps is a different subsystem (rebase, cpu_data, platform expert, VM, printf, timers).
    One word per step turns that into a single read.
    """
    path = Path('.cache/xnu-work/osfmk/arm/arm_init.c')
    if not path.exists():
        fail('osfmk/arm/arm_init.c missing: cannot place the stage probes')
    text = path.read_text()
    if 'droidvm_ios16_stage' in text:
        print('ios16 stage probes already present')
        return
    # The declaration goes next to arm_init's forward declaration, never inside the definition:
    # inserting it before `arm_init(` splits the `void` return type from the name, which clang
    # reports as "array has incomplete element type 'volatile void'" (run57 died exactly there).
    decl = 'void arm_init(boot_args * args);\n'
    if text.count(decl) != 1:
        fail('arm_init forward declaration anchor not unique')
    text = text.replace(
        decl, decl + '\nextern volatile unsigned int droidvm_ios16_stage[8];\n', 1)
    anchor = 'arm_init(\n\tboot_args       *args)\n{\n'
    if anchor not in text:
        fail('arm_init signature anchor missing')
    # (source line, stage index): the value written after that call returns is index+1, so 0
    # means "the first step never finished" and 8 means "arm_init ran to machine_startup".
    probes = [
        ('\tcpu_data_init(&BootCpuData);\n', 0),
        ('\tPE_init_platform(FALSE, args); /* Get platform expert set up */\n', 1),
        ('\tarm_vm_init(xmaxmem, args);\n', 2),
        ('\tkernel_startup_initialize_upto(STARTUP_SUB_KPRINTF);\n', 3),
        ('\tPE_init_printf(FALSE);\n', 4),
        ('\tPE_init_platform(TRUE, &BootCpuData);\n', 5),
        # __stack_chk_guard is outside every #if, so this one always compiles (pal_hib_init
        # sits under #if HIBERNATION).
        ('\t__stack_chk_guard = (unsigned long)early_random();\n', 6),
        ('\tmachine_startup(args);\n', 7),
    ]
    for line, index in probes:
        if text.count(line) != 1:
            fail('stage probe anchor not unique: %r' % line.strip())
        text = text.replace(line, line + '\tdroidvm_ios16_stage[%d]=%d;\n' % (index, index + 1), 1)
    path.write_text(text)
    print('ios16 stage probes placed: 8 steps in arm_init')


def main():
    if '--late' in sys.argv:
        place_late()
        return 0
    fetch_headers()
    dedupe_pmap_field()
    write_ppl_shims()
    disable_mockfs()
    add_compile_records()
    write_libpthread_patch()
    write_source_pthread_bootstrap()
    write_link_shims()
    add_vfs_peer()
    add_stage_probes()
    patch_prelink_tool()
    print('fixup: done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
