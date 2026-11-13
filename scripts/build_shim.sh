#!/bin/bash
# Build the droidvm boot shim and drop it where crosvm include_bytes!s it.
#
# Adapted from droidvm-meta-repo/2-0_build_shim.sh, which assumes a meta-repo workspace with
# symlinked component dirs; here the AOSP tree is passed in. Its checks are kept because each one
# caught a real failure: a stale binary from another target, SIMD in a pre-MMU shim, and a shim
# that grew into the device tree's 2 MiB offset.
#
#   build_shim.sh <aosp-root>
set -euo pipefail

ROOT=${1:?usage: build_shim.sh <aosp-root>}
SHIM=$ROOT/external/crosvm/droidvm-shim
OUT=$ROOT/out/shim
# Named here AND in the crate's .cargo/config.toml (which is what actually decides the target),
# so the two cannot disagree about where the binary lands.
TARGET=aarch64-unknown-none-softfloat

[ -d "$SHIM" ] || { echo "no shim crate at $SHIM -- is external/crosvm on the right branch?"; exit 1; }

# cd rather than --manifest-path: cargo reads .cargo/config.toml relative to the working
# directory, and that file names the bare-metal target, the linker script and the flags.
( cd "$SHIM" && cargo +stable build --release --target "$TARGET" )

# Anything left from a build for another target is a trap for whoever reads this next.
rm -rf "$SHIM/target/aarch64-unknown-none"
mkdir -p "$OUT"
cp "$SHIM/target/$TARGET/release/droidvm-shim" "$OUT/shim.elf"
aarch64-linux-gnu-objcopy -O binary "$OUT/shim.elf" "$OUT/shim.bin"

# No FP/SIMD: the shim runs before anything sets CPACR_EL1.FPEN, so one such instruction traps in
# a VM that has no handler and no console.
simd=$(aarch64-linux-gnu-objdump -d "$OUT/shim.elf" | grep -cE '\s(q|v)[0-9]+(\.|,|\])' || true)
if [ "$simd" -ne 0 ]; then
  echo "shim uses $simd FP/SIMD operands: it runs before CPACR_EL1.FPEN is set and would trap"
  aarch64-linux-gnu-objdump -d "$OUT/shim.elf" | grep -E '\s(q|v)[0-9]+(\.|,|\])' | head -10
  exit 1
fi

# The copy crosvm compiles in. Kept in the source tree because that is where include_bytes!
# looks, and a stale one is then visible to git rather than invisible.
cp "$OUT/shim.bin" "$ROOT/external/crosvm/hypervisor/src/gunyah/shim.bin"

# The shim shares its lent region with the guest's device tree at AARCH64_SHIM_FDT_OFFSET = 2 MiB.
# `_shim_end` is past the stack and BSS (neither is in the flat binary), so it -- not the file
# size -- is the number that must stay below that.
end=$(python3 - "$OUT/shim.elf" <<'PY'
import subprocess, sys
out = subprocess.run(['aarch64-linux-gnu-nm', sys.argv[1]], capture_output=True, text=True).stdout
for line in out.splitlines():
    parts = line.split()
    if len(parts) == 3 and parts[2] == '_shim_end':
        print(int(parts[0], 16))
        break
PY
)
[ -n "$end" ] || { echo "no _shim_end in the shim: the linker script did not do what it says"; exit 1; }
[ "$end" -le $((0x200000)) ] || {
  printf 'shim ends at %#x, past the device tree at 0x200000 (AARCH64_SHIM_FDT_OFFSET)\n' "$end"
  exit 1
}

size=$(stat -c%s "$OUT/shim.bin")
echo "shim.bin: $size bytes, _shim_end 0x$(printf %x "$end")"
[ "$size" -le $((1024 * 1024)) ] || { echo "shim is over 1 MiB: it does not belong in the boot region"; exit 1; }
