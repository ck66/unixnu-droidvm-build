#!/usr/bin/env python3
"""Apply this repository's two XNU changes to a prepared source tree.

Why not a patch file: a hand-written unified diff has to agree with its own hunk counts and with the
exact context of the file it lands on.  When it does not, one tool says "corrupt patch" and another
silently skips it -- and a build that skips it still succeeds, which is how 43600590879 produced a
126 MB kernel with `strings | grep 'DroidVM owner-cache'` = 0.

Both edits therefore anchor on text that occurs exactly once in the file, and each one asserts its
own result: the anchor must be found, and the expected marker must be present afterwards.

  0001  osfmk/vm/vm_map_store_rb.c
        A hole list that has drifted from the RB tree is a lost hint, not a reason to panic a guest
        that is otherwise fully up.  Replaces the panic statement with `return;`.  (Verified on the
        phone as a 4-byte edit: read_bytes 205 MB -> 778 MB, four vCPUs busy, exec launchd reached.)

  0002  bsd/kern/kern_exec.c
        The owner chain returns an errno and the caller goes on to exec init against whatever root
        it has, so the guest only fails later with dyld's "no dyld cache".  Behind the boot argument
        owner-cache-diag=1, turn that errno into a panic carrying stage/errno/device.

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

    print('0002 bsd/kern/kern_exec.c')
    path, text = read(tree, 'bsd/kern/kern_exec.c')
    if 'DroidVM owner-cache' in text:
        print('  ok    already applied')
    else:
        new, applied = edit_002(text)
        check('anchor found', applied, 'no "native_owner_cache_authorized = cache_authorized;"')
        check('panic present', 'DroidVM owner-cache' in new, 'replacement did not land')
        if applied:
            path.write_text(new)

    if FAILED:
        sys.exit('apply-xnu-changes: ' + ', '.join(FAILED))
    print('both changes are in the source the compiler will read')


if __name__ == '__main__':
    main()
