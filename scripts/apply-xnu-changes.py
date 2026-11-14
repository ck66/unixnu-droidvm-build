#!/usr/bin/env python3
"""Apply this repository's XNU changes to a prepared source tree.

Why not a hand-written patch file: a unified diff has to agree with its own hunk counts and with the
exact context of the file it lands on.  When it does not, one tool says "corrupt patch" while another
silently skips it -- and a build that skips it still succeeds, which is how 43600590879 produced a
126 MB kernel with `strings | grep 'DroidVM owner-cache'` = 0.

The edits anchor on text that occurs once and assert their own result: the anchor must be found and
the marker must be present afterwards.  The script is idempotent; the workflow runs it, lets
`git diff` write the patch (so hunk counts are git's problem), and reverts the tree so the build
applies that patch with all the others.

  0001  osfmk/vm/vm_map_store_rb.c
        A hole list that has drifted from the RB tree is a lost hint, not a reason to panic a guest
        that is otherwise fully up.  Replaces the panic statement with `return;`.  (Verified on the
        phone as a 4-byte edit: read_bytes 205 MB -> 778 MB, four vCPUs busy, exec launchd reached.)

  0002  bsd/kern/kern_exec.c -- native_owner_cache_prepare()
        Reports the owner-cache refusal through a panic when booted with owner-cache-diag=1.
        43606725375 showed it never fired, i.e. that chain reports success.

  0003  bsd/kern/kern_exec.c -- after native_cryptex_os_view()
        Reports the cryptex OS view stages unconditionally.  The owner chain succeeding while dyld
        still says "no dyld cache" puts the failure downstream: the root's /System/Cryptexes/OS only
        reaches the SystemOS volume through this view, and none of its stages are fatal upstream.

usage: apply-xnu-changes.py <xnu source tree>
"""
import re
import sys
from pathlib import Path

FAILED = []


def read(tree, relative):
    path = tree / relative
    if not path.is_file():
        sys.exit(f'{relative}: not in {tree} -- is this the prepared xnu-work tree?')
    return path, path.read_text()


def check(name, condition, detail):
    if condition:
        print(f'  ok    {name}')
    else:
        print(f'  FAIL  {name}: {detail}')
        FAILED.append(name)


def edit_001(text):
    """Return the text with the hole-list panic replaced by a plain return."""
    anchor = 'panic("Illegal action: h1:'
    first = text.find(anchor)
    if first < 0:
        return text, False
    end = text.find(');', first)
    if end < 0:
        raise SystemExit('0001: found the panic anchor but not its closing ");"')
    # Keep the leading indentation of the statement so the surrounding braces stay readable.
    line_start = text.rfind('\n', 0, first) + 1
    indent = text[line_start:first]
    replacement = (indent + '/* DroidVM: the hole list is a hint over the RB tree, not the authority.  A missed\n'
                   + indent + ' * hole leaves first_free on an address that may be taken, and the next search\n'
                   + indent + ' * re-checks the tree and corrects it; nothing is mapped or unmapped here, so\n'
                   + indent + ' * skipping the update cannot lose an entry.  On this board the drift happens on\n'
                   + indent + ' * the first exec, with the guest otherwise fully up, so the panic cost a boot. */\n'
                   + indent + 'return;')
    # end points at the ");" that closes the panic call, so the replacement has to take both
    return text[:line_start] + replacement + text[end + 2:], True


def edit_002(text):
    """Return the text with the owner-cache diagnostic inserted after the authorization line."""
    pattern = re.compile(r'^([ \t]*)native_owner_cache_authorized = cache_authorized;[ \t]*\n', re.M)
    match = pattern.search(text)
    if not match:
        return text, False
    ind = match.group(1)          # the indentation of the statement in the function
    o = ind + '\t'                # one level deeper, inside the new if
    block = (
        f'{ind}/*\n'
        f'{ind} * DroidVM: the owner chain returns an errno and the caller goes on to exec init against\n'
        f'{ind} * whatever root it has, so the guest only fails later with dyld\'s "no dyld cache".\n'
        f'{ind} * Report the refusal here instead -- behind owner-cache-diag=1, so an ordinary build\n'
        f'{ind} * of this is inert.  stage: 0 root not authorized, 1 volume never published,\n'
        f'{ind} * 3 mount() failed (mount_status holds its errno), 4 mounted but not authorized.\n'
        f'{ind} */\n'
        f'{ind}if (error) {{\n'
        f'{o}extern volatile int native_owner_root_apple_autherr;\n'
        f'{o}extern volatile int native_owner_root_decision;\n'
        f'{o}extern int PE_parse_boot_argn(const char *, void *, int);\n'
        f'{o}int diag = 0;\n'
        f'\n'
        f'{o}if (PE_parse_boot_argn("owner-cache-diag", &diag, sizeof(diag)) && diag) {{\n'
        f'{o}\tpanic("DroidVM owner-cache: stage=%d status=%d mount=%d root_auth=%d "\n'
        f'{o}\t    "autherr=%d decision=%d dev=%d/%d path=%s",\n'
        f'{o}\t    native_owner_cache_stage, error, native_owner_cache_mount_status,\n'
        f'{o}\t    root_authorized, native_owner_root_apple_autherr, native_owner_root_decision,\n'
        f'{o}\t    (int)root_device, (int)selected, native_owner_cache_device_path);\n'
        f'{o}}}\n'
        f'{ind}}}\n'
    )
    return text[:match.end()] + block + text[match.end():], True


def edit_003(text):
    """Return the text with a diagnostic panic after the cryptex OS view is built."""
    anchor = 'native_cryptex_os_view(p, scratch);'
    first = text.find(anchor)
    if first < 0:
        return text, False
    line_start = text.rfind('\n', 0, first) + 1
    ind = text[line_start:first]
    o = ind + '\t'
    block = (
        f'{ind}{anchor}\n'
        f'{ind}/*\n'
        f'{ind} * DroidVM: the owner chain reported success yet dyld still says "no dyld cache", so the\n'
        f'{ind} * failure is downstream of it.  This is where the cryptex OS view is built (the root\'s\n'
        f'{ind} * /System/Cryptexes/OS only reaches the SystemOS volume through it), and none of the\n'
        f'{ind} * stages here are fatal upstream.  Report them all.\n'
        f'{ind} */\n'
        f'{ind}{{\n'
        f'{o}extern volatile int native_cryptex_os_stage, native_cryptex_os_status;\n'
        f'{o}extern volatile int native_gpu_addon_stage, native_gpu_addon_status;\n'
        f'{o}extern volatile int native_gpu_addon2_stage, native_gpu_addon2_status;\n'
        f'{o}extern volatile int native_gpu_addon3_stage, native_gpu_addon3_status;\n'
        f'{o}extern volatile int native_owner_cache_stage, native_owner_cache_status;\n'
        f'{o}extern volatile int native_owner_cache_mount_status;\n'
        f'{o}extern volatile int native_owner_data_stage, native_owner_data_status;\n'
        f'{o}extern volatile int native_staged_apps_stage, native_staged_view_stage, native_staged_view_status;\n'
        f'{o}extern volatile int native_owner_boot_manifest_status;\n'
        f'{o}extern char native_owner_cache_device_path[64];\n'
        f'{o}/* Unconditional on purpose: a gated panic left "did the owner chain succeed, or did the\n'
        f'{o} * boot argument not parse?" open (43606725375), and this build exists only to answer it. */\n'
        f'{o}panic("DroidVM cryptex: stage=%d status=%d addon=%d/%d addon2=%d/%d addon3=%d/%d "\n'
        f'{o}    "owner=%d/%d/%d data=%d/%d staged=%d/%d/%d manifest=%d path=%s",\n'
        f'{o}    native_cryptex_os_stage, native_cryptex_os_status,\n'
        f'{o}    native_gpu_addon_stage, native_gpu_addon_status,\n'
        f'{o}    native_gpu_addon2_stage, native_gpu_addon2_status,\n'
        f'{o}    native_gpu_addon3_stage, native_gpu_addon3_status,\n'
        f'{o}    native_owner_cache_stage, native_owner_cache_status, native_owner_cache_mount_status,\n'
        f'{o}    native_owner_data_stage, native_owner_data_status,\n'
        f'{o}    native_staged_apps_stage, native_staged_view_stage, native_staged_view_status,\n'
        f'{o}    native_owner_boot_manifest_status, native_owner_cache_device_path);\n'
        f'{ind}}}\n'
    )
    return text[:line_start] + block + text[first + len(anchor):], True


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    tree = Path(sys.argv[1])
    if not (tree / 'bsd').is_dir() or not (tree / 'osfmk').is_dir():
        sys.exit(f'{tree} does not look like an XNU source tree')

    print('0001 osfmk/vm/vm_map_store_rb.c')
    path, text = read(tree, 'osfmk/vm/vm_map_store_rb.c')
    if 'Illegal action: h1:' not in text:
        check('panic replaced', 'DroidVM: the hole list is a hint' in text,
              'the anchor is gone and so is the comment: already applied or a different source?')
    else:
        new, applied = edit_001(text)
        check('anchor found', applied, 'no "Illegal action: h1:" in the file')
        check('comment present', 'DroidVM: the hole list is a hint' in new, 'replacement did not land')
        check('panic gone', 'Illegal action: h1:' not in new, 'the panic is still there')
        path.write_text(new)

    print('0002 bsd/kern/kern_exec.c (owner cache)')
    path, text = read(tree, 'bsd/kern/kern_exec.c')
    if 'DroidVM owner-cache' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_002(text)
        check('anchor found', applied, 'no "native_owner_cache_authorized = cache_authorized;"')
        check('panic present', 'DroidVM owner-cache' in new, 'replacement did not land')
        if applied:
            path.write_text(new)

    print('0003 bsd/kern/kern_exec.c (cryptex view)')
    path, text = read(tree, 'bsd/kern/kern_exec.c')
    if 'DroidVM cryptex:' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_003(text)
        check('cryptex anchor found', applied, 'no "native_cryptex_os_view(p, scratch);"')
        check('cryptex panic present', 'DroidVM cryptex:' in new, 'replacement did not land')
        if applied:
            path.write_text(new)

    if FAILED:
        sys.exit('apply-xnu-changes: ' + ', '.join(FAILED))
    print('all changes are in the source the compiler will read')


if __name__ == '__main__':
    main()
