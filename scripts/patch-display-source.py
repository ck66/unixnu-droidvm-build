#!/usr/bin/env python3
"""Patch the display sources in CI and report what it changed.

Run from the repository root of unixnu-droidvm-build after the overlay has been
applied and after the iPadOS display sources have been taken, i.e.
``python3 self/scripts/patch-display-source.py``.

Two jobs:

1. ``DroidVMVirtioGpu3D.cpp``: silence the array-in-boolean expressions that
   upstream never compiled with -Werror.  Pure rewrites, semantics unchanged.

2. ``NativeLegacyDisplay.cpp``: install the swap pump plus its probe globals.

   MEASURED 2026-11-17 (run 44077683331, k22 on the phone): the pump's timer ran
   exactly once -- ``_droidvm_probe_tick_count = 1`` after userland was fully up.
   ``IOTimerEventSource::setTimeoutMS`` is one-shot, and the first version of this
   patch did not re-arm it inside the action, so every "the pump had nothing to
   do" reading from k19/k21 was taken from a pump that had ticked once at boot.
   The action now re-arms itself, so the pump really is periodic.

   The probe answers the only question left about the swap path:

     _droidvm_probe_tick_count             pump ticks (proves it is periodic)
     _droidvm_probe_base_swap_ticks        ticks that saw a pending base swap
     _droidvm_probe_base_swap_validated    ... and that swap was accepted by the
                                           validator (kickOnce's gate)
     _droidvm_probe_base_swap_ptr          last such pointer

   > 0 base_swap_ticks  => the client's swap reaches the inherited queue, so the
                           fix is to route it into kickOnce.
   == 0 base_swap_ticks => the client call never reaches this class, so the entry
                           point is another virtual/selector.
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.')
PUMP_MARK = 'droidvmPump'

VIRTIO_3D_REWRITES = (
    # gSurfaceUses is an array: `!array` is always false, and with -Werror the
    # build stops.  Dropping the always-false half keeps the meaning.
    ('if (!gSurfaceUses || gSurfaceUseCount == MaxSurfaceUses)',
     'if (gSurfaceUseCount == MaxSurfaceUses)'),
    ('if (gSurfaceUses)',
     'if (reinterpret_cast<uintptr_t>(gSurfaceUses) != 0)'),
)

PROBE_HEAD = '''#include <stdint.h>
// DroidVM probe: `volatile uint64_t x;` is already a definition in C++, so the
// forward declarations must say extern or the definitions below are redefinitions
// (run 44073411882 died exactly there).
extern "C" {
extern volatile uint64_t droidvm_probe_base_swap_ticks;
extern volatile uint64_t droidvm_probe_base_swap_validated;
extern volatile uint64_t droidvm_probe_base_swap_ptr;
extern volatile uint64_t droidvm_probe_tick_count;
}
volatile uint64_t droidvm_probe_base_swap_ticks=0;
volatile uint64_t droidvm_probe_base_swap_validated=0;
volatile uint64_t droidvm_probe_base_swap_ptr=0;
volatile uint64_t droidvm_probe_tick_count=0;
'''

# pendingSwapRequest() is the accessor kickOnce() itself uses; kLegacyCurrentSwap
# is a file-scope constant declared later in the file than this insertion point.
PROBE_MEMBERS = '''    IOTimerEventSource *droidvmPump=nullptr;
    static void droidvmPumpFired(OSObject *owner,IOTimerEventSource *) {
        auto *self=static_cast<NativeLegacyDisplay *>(owner);
        droidvm_probe_tick_count=droidvm_probe_tick_count+1;
        const OriginalLegacySwapRequest *cur=self->pendingSwapRequest();
        if(cur){
            droidvm_probe_base_swap_ticks=droidvm_probe_base_swap_ticks+1;
            droidvm_probe_base_swap_ptr=(uint64_t)(uintptr_t)cur;
            if(self->isValidated(cur))droidvm_probe_base_swap_validated=droidvm_probe_base_swap_validated+1;
            self->kickSwapQueue();
        }
        // MEASURED 2026-11-17: setTimeoutMS is one-shot. Without this line the
        // pump fired once at start() and never again (tick_count stayed 1).
        if(self->droidvmPump)self->droidvmPump->setTimeoutMS(16);
    }
    void startDroidvmPump() {
        if(droidvmPump)return;
        droidvmPump=IOTimerEventSource::timerEventSource(this,droidvmPumpFired);
        if(!droidvmPump)return;
        IOWorkLoop *loop=getWorkLoop();
        if(!loop || loop->addEventSource(droidvmPump)!=kIOReturnSuccess){OSSafeReleaseNULL(droidvmPump);return;}
        droidvmPump->setTimeoutMS(16);
    }
'''


def fail(message):
    print('::error::' + message)
    raise SystemExit(1)


def patch_virtio_3d(root):
    path = root / 'UniXNU-droidvm/package/DroidVMVirtioGpu3D.cpp'
    if not path.exists():
        print('DroidVMVirtioGpu3D.cpp not present; skipped')
        return
    text = path.read_text()
    changed = 0
    for old, new in VIRTIO_3D_REWRITES:
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            changed += count
    if changed:
        path.write_text(text)
    print('virtio-3D warning rewrites:', changed)


def patch_display(root):
    path = root / 'UniXNU/drivers/GopFramebuffer/NativeLegacyDisplay.cpp'
    if not path.exists():
        fail('NativeLegacyDisplay.cpp not found at %s' % path)
    text = path.read_text()
    if PUMP_MARK in text:
        print('pump already present; nothing to do')
        return

    anchor = '    void kickSwapQueue() override {'
    if anchor not in text:
        fail('kickSwapQueue anchor missing: the pinned display source changed')
    text = text.replace(anchor, PROBE_MEMBERS + anchor, 1)

    call = '        startVsync();startDirect();return true;'
    if call not in text:
        fail('start() anchor missing: cannot attach the pump')
    text = text.replace(call, '        startVsync();startDirect();startDroidvmPump();return true;', 1)

    path.write_text(PROBE_HEAD + text)

    body = (PROBE_HEAD + text)
    print('pump installed')
    print('  startDroidvmPump calls:', body.count('startDroidvmPump()'))
    print('  setTimeoutMS(16) count:', body.count('setTimeoutMS(16)'))
    print('  re-arm line present:  ', 'self->droidvmPump->setTimeoutMS(16)' in body)


def main():
    patch_virtio_3d(ROOT)
    patch_display(ROOT)


if __name__ == '__main__':
    main()
