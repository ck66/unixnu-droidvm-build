#!/bin/bash
# Collect the built crosvm plus the .so files it directly links into one directory.
#
# Adapted from droidvm-meta-repo/2-1_collect_crosvm.sh. Its notes still apply: crosvm's RUNPATH
# is $ORIGIN, so pushing this directory and running ./crosvm is enough; and no `pipefail` on
# purpose -- findso()/grep legitimately produce no match (a lib not in the tree is assumed to be
# on the device), and with pipefail that exit 1 would abort the script through set -e.
#
#   collect_crosvm.sh <dest-dir>      (run from the AOSP root)
set -eu

DEST=${1:-out/crosvm_pkg}
ARCH="${ARCH:-android_arm64}"

RE=$(ls prebuilts/clang/host/linux-x86/*/bin/llvm-readelf 2>/dev/null | head -1)
BIN=$(ls out/soong/.intermediates/external/crosvm/crosvm_device_only/crosvm_device_only_bin/*/crosvm 2>/dev/null | grep -v unstripped | head -1)
[ -n "$BIN" ] || BIN=$(find out/soong/.intermediates -path '*crosvm_device_only*' -name crosvm -type f 2>/dev/null | grep -v unstripped | head -1)
[ -n "$RE" ]  || { echo "llvm-readelf not found under prebuilts/clang"; exit 1; }
[ -n "$BIN" ] || { echo "crosvm not built yet (no crosvm_device_only_bin)"; exit 1; }

echo "binary  $BIN"
echo "readelf $RE"

# bionic, always on the device
SKIP="libc.so libdl.so libm.so libstdc++.so"
mkdir -p "$DEST"
cp -f "$BIN" "$DEST/crosvm"

needed() { "$RE" -d "$1" 2>/dev/null | awk -F'[][]' '/NEEDED/{print $2}'; }
findso() { find out/soong/.intermediates -path "*${ARCH}*_shared*" -name "$1" 2>/dev/null | grep -v unstripped | head -1; }

declare -A seen
for so in $(needed "$BIN"); do
  case " $SKIP " in *" $so "*) continue;; esac
  [ -n "${seen[$so]:-}" ] && continue
  seen[$so]=1
  src=$(findso "$so")
  if [ -n "$src" ]; then
    cp -f "$src" "$DEST/"
    printf '  + %-28s %s\n' "$so" "$src"
  else
    printf '  ? %-28s (not in tree; assume present on device)\n' "$so"
  fi
done

echo "==> $DEST"
ls -l "$DEST"
