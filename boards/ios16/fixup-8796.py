"""Last-resort source fixups for xnu-8796 that no patch covers.

Run from the tree root (out/<board>/tree), after the patches are applied and before
`make`. Idempotent: every step prints what it changed and does nothing on a second run.
"""
from pathlib import Path
import sys


def dedupe_pmap_field():
    """8796 declares `bool reserved10;` twice: once behind CONFIG_ROSETTA and once behind
    HAS_APPLE_PAC. This board turns both off, so the struct has a duplicate member and
    nothing compiles ("error: duplicate member 'reserved10'"). UniXNU's
    non-apple-pmap-field.patch fixes the 10063 numbering, where the second field had a
    different name; on 8796 it is reserved10 as well, so rename the later one here."""
    p = Path('.cache/xnu-work/osfmk/arm/pmap/pmap.h')
    if not p.is_file():
        print('fixup: no %s (tree not prepared yet?)' % p)
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


def main():
    ok = dedupe_pmap_field()
    print('fixup: done, changed=%s' % ok)
    return 0


if __name__ == '__main__':
    sys.exit(main())
