"""Teach UniXNU's patch resolver to retry a patch with less context.

The resolver (scripts/unixnu/patches.py, prepare()) applies the selected series one file at a
time and aborts the build on the first refusal. The series was written against xnu-10063; on
8796 most of the refusals are only "the surrounding lines moved", which `git apply -C1` or
`-C0` accepts. The same edit prints how many patches were selected and which ones needed the
shortened context, so a CI log answers "what did this board actually get".

Usage: python3 patch-resolver.py <checkout-root>       (default: UniXNU)
"""
from pathlib import Path
import sys

OLD = """            try:
                git(work, 'apply', str(path))
            except Error as error:
                shutil.rmtree(work)
                raise Error(f'XNU patch {path}: {error}') from error"""

NEW = """            applied_with = None
            tried = []
            for fuzz in ([], ['-C1'], ['-C0']):
                try:
                    git(work, 'apply', *fuzz, str(path))
                    applied_with = fuzz
                    break
                except Error as error:
                    first = str(error).strip().splitlines()
                    tried.append(first[0] if first else 'error')
            if applied_with is None:
                shutil.rmtree(work)
                raise Error('XNU patch %s: %s' % (path, ' | '.join(tried))) from None
            if applied_with:
                print('kernel: patch %s needed %s' % (path.name, applied_with[0]))"""


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else 'UniXNU')
    target = root / 'scripts' / 'unixnu' / 'patches.py'
    if not target.is_file():
        raise SystemExit('no %s' % target)
    text = target.read_text()
    if 'applied_with = None' in text:
        print('resolver already patched')
        return 0
    if OLD not in text:
        raise SystemExit('resolver anchor not found in %s' % target)
    text = text.replace(OLD, NEW, 1)
    text = text.replace("        for path in selected:\n",
                        "        print('kernel: %d patches selected' % len(selected))\n"
                        "        for path in selected:\n", 1)
    target.write_text(text)
    print('resolver patched:', target)
    return 0


if __name__ == '__main__':
    sys.exit(main())
