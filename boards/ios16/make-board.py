#!/usr/bin/env python3
"""Turn the working 17.7.11 droidvm board into the iOS 16 one, in place.

    boards/ios16/make-board.py UniXNU-droidvm

Why derive instead of shipping a board.toml: every field this board keeps is a field
that is already known to boot, so the diff against a working board stays visible and
small.  Everything specific to 20H392 is listed below and nothing else is touched.

Values and where they come from (see hy4/ios16/PROGRESS.md):
  [ipsw]                  20H392 / iPhone10,6 / d221ap (S1, six pins),
                          fetch = KernelCache + DeviceTree + StaticTrustCache only,
                          because a kernel-only build fetches no volume
  [ipsw.sha256]           the four members measured 2026-11-18 against 122-77006
  [kernel] defconfig      ios16_defconfig (ported from whyred_defconfig)
  [kernel] archives       build-droidvm-all.py's REFERENCE_ARCHIVES: no storage,
                          no display, no owner root -- this run only wants a kernel
  [kernel.prelink]        the decompressed 20H392 slice (2ec297ba…), the AMFI
                          initializer inside that slice's own __TEXT_EXEC
                          (0xfffffff005703c54), and S7's 30 roots (no codeless
                          IOHIDEventDriver, which the 17.7.11 board does list)
  [payload]/[adt]         identity only; the volume UUID is a seed output, never
                          copied from another board
"""
import sys
import tomllib
from pathlib import Path

IPSW_OLD = '''url = "https://updates.cdn-apple.com/2025SpringFCS/fullrestores/122-77166/DCA6CFE8-90E9-4B92-880D-3DE171798E40/iPad_Pro_HFR_17.7.11_21H461_Restore.ipsw"
product = "iPad7,1"
version = "17.7.11"
build = "21H461"
device_class = "j120ap"
chip_id = 0x8011
board_id = 12
fetch = ["KernelCache", "DeviceTree", "StaticTrustCache", "Cryptex1,SystemTrustCache", "Cryptex1,AppOS", "Cryptex1,AppTrustCache"]'''

IPSW_NEW = '''url = "https://updates.cdn-apple.com/2025FallFCS/fullrestores/122-77006/1158E502-D76D-4226-AA06-ED5E30CD1111/iPhone10,3,iPhone10,6_16.7.16_20H392_Restore.ipsw"
product = "iPhone10,6"
version = "16.7.16"
build = "20H392"
device_class = "d221ap"
chip_id = 0x8015
board_id = 14
# A kernel-only build fetches no volume: the OS/SystemOS dmgs are 7.7 GB and bvxn-wrapped.
fetch = ["KernelCache", "DeviceTree", "StaticTrustCache"]'''

PIN_OLD = '''"044-76040-038.dmg" = "3f99b859c9728379110b66657c7b15285bcb1925ad8b5726542e2cdcf8b1028d"
"044-76034-038.dmg" = "b4908cde41d23e6bc05e6af51a2c9eeb00cf0f863a6a18b1600c96a3fd70414f"'''

PIN_NEW = '''# Measured 2026-11-18 against 122-77006 with tools/ipswfetch.py over HTTP Range; the whole
# archive hashes to ece1194f7e233e9109c5790a8d23a4564a10a254e15ec5b09978a343265568fb.
"kernelcache.release.iphone10b" = "2d6a13d040aaf87cec09bb6906d43e9bd8cea951e43d1aa1cd62707dfdc5aaec"
"Firmware/all_flash/DeviceTree.d221ap.im4p" = "889e838319619c10f5f7f6ef25f9ff2a3a652a3c078803038ba678efa1201b79"
"Firmware/087-86698-044.dmg.trustcache" = "63e01c03b31e1532a5581a24865490673f0b5c297b0199fe0095637c6100ef4b"
"Firmware/087-86593-047.dmg.trustcache" = "9cc478fe3525e2683b5b9ea1eea0851eaebebdab21d206556233400e59d2e361"'''

ARCHIVES_NEW = '''archives = [
  # build-droidvm-all.py's REFERENCE_ARCHIVES: the set the 17.7.11 reference candidate
  # uses, so no storage/display audit and no OS volume. This run only wants a kernel.
  "build-corecrypto-14.py",
  "build-corecrypto-14.py --namespace",
  # ArmDTPlatform and ArmCpu are absent on purpose: the archive builders read the compile
  # records the kernel build wrote for each kext, and the BCM2837 machine config does not
  # build those two (they belong to the phone-shaped machine). They come back once this board
  # gets its own machine config; the kernel itself does not need them to compile or link.
  "build-arm-driver.py NativeAES",
  "build-native-aes-bindings.py",
  "build-libpthread-kernel.py",
]'''

BOARD_NAME_OLD = 'name = "droidvm"\nsoc = "crosvm-gunyah"\ndescription = "DroidVM guest (crosvm on Gunyah)"'
BOARD_NAME_NEW = 'name = "ios16"\nsoc = "crosvm-gunyah"\ndescription = "DroidVM guest (crosvm on Gunyah), iOS 16.7.16 profile"'


def main():
    device = Path(sys.argv[1] if len(sys.argv) > 1 else 'UniXNU-droidvm')
    path = device / 'board.toml'
    text = path.read_text()

    def sub(old, new):
        nonlocal text
        if old not in text:
            raise SystemExit('board.toml: anchor missing: %r' % old[:70])
        text = text.replace(old, new, 1)

    sub(BOARD_NAME_OLD, BOARD_NAME_NEW)
    sub(IPSW_OLD, IPSW_NEW)
    sub(PIN_OLD, PIN_NEW)
    sub('defconfig = "droidvm_defconfig"', 'defconfig = "ios16_defconfig"')

    # The patch series is the part of a board that cannot be derived: UniXNU's arch profile
    # ships one list written against xnu-10063, and applying that plus whyred's device list
    # stacks two generations of patches on a 8796 tree (that is what kept failing: pmap.c and
    # the firehose headers are only consistent in one of them). N1709's series *is* the arm64
    # list adapted to 8796 -- it replaces the entries that moved and names the four it omits --
    # so this board takes that list alone.
    sub('defconfig = "ios16_defconfig"',
        'defconfig = "ios16_defconfig"\npatch_series = ["device:patches/xnu/series"]')

    start = text.index('archives = [')
    end = text.index('\n]', start)
    text = text[:start] + ARCHIVES_NEW + text[end + 2:]

    sub('kernelcache_sha256 = "f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7"',
        'kernelcache_sha256 = "2ec297bac95bcba01046fd0148e97e7e5885c05a6c18827f39f534ba375c1221"')
    sub('early_security_init = "com.apple.driver.AppleMobileFileIntegrity=0xfffffff005ab4ae0"',
        'early_security_init = "com.apple.driver.AppleMobileFileIntegrity=0xfffffff005703c54"')
    # 17.7.11 ships 31 roots; S7's real iOS 16 profile ships 30 (no codeless IOHIDEventDriver).
    sub('  "com.apple.iokit.IOHIDEventDriver",\n', '')

    sub('root_match_volume_uuid = "04769D26-2CA0-4825-B443-E2E5F88300F2"   # the original OS volume',
        'root_match_volume_uuid = "TODO"   # seed output; never copy another board value')
    sub('ipad_identity = true', 'ipad_identity = false')
    sub('chip_id = "8011"                     # iPad7,1 (A10X), as board [ipsw] chip_id',
        'chip_id = "8015"                     # iPhone10,6 (A11), as board [ipsw] chip_id')

    path.write_text(text)

    board = tomllib.loads(text)
    assert board['kernel']['defconfig'] == 'ios16_defconfig'
    assert len(board['kernel']['prelink']['roots']) == 30, len(board['kernel']['prelink']['roots'])
    assert board['kernel']['prelink']['kernelcache_sha256'].startswith('2ec297ba')
    assert board['kernel']['prelink']['early_security_init'].endswith('0xfffffff005703c54')
    assert board['ipsw']['build'] == '20H392' and board['ipsw']['device_class'] == 'd221ap'
    print('board %s: %s %s, roots %d, archives %d, fetch %s' % (
        board['board']['name'], board['ipsw']['product'], board['ipsw']['build'],
        len(board['kernel']['prelink']['roots']), len(board['kernel']['archives']),
        board['ipsw']['fetch']))


if __name__ == '__main__':
    main()
