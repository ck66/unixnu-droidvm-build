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
extern volatile uint64_t droidvm_probe_ready_ticks;
extern volatile uint64_t droidvm_probe_kick_calls;
extern volatile uint64_t droidvm_probe_w[10];
extern volatile uint64_t droidvm_probe_w2[8];
}
volatile uint64_t droidvm_probe_base_swap_ticks=0;
volatile uint64_t droidvm_probe_base_swap_validated=0;
volatile uint64_t droidvm_probe_base_swap_ptr=0;
volatile uint64_t droidvm_probe_tick_count=0;
volatile uint64_t droidvm_probe_ready_ticks=0;
volatile uint64_t droidvm_probe_kick_calls=0;
volatile uint64_t droidvm_probe_w[10]={0};
volatile uint64_t droidvm_probe_w2[8]={0};
'''

PROBE_MEMBERS = '''    IOTimerEventSource *droidvmPump=nullptr;
    static void droidvmPumpFired(OSObject *owner,IOTimerEventSource *) {
        auto *self=static_cast<NativeLegacyDisplay *>(owner);
        droidvm_probe_tick_count=droidvm_probe_tick_count+1;
        // The swap queue of the legacy family, at the offsets the original
        // submit path uses (measured from the original binary: current +4872,
        // ready head +4880, ready tail +4888, pending +4896). Original submit
        // moves pending -> ready before calling virtual slot 276, which is the
        // kickSwapQueue we override, so a swap that arrived and was not taken
        // stays visible here.
        const unsigned char *base=reinterpret_cast<const unsigned char *>(self);
        // Offsets taken from this build's own generated ABI
        // (scripts/build-native-legacy-display.py): current +5472, queue head
        // +5480 (that is what pendingSwapRequest() reads), queue tail +5488,
        // boot dimensions +5808/+5812, instance extent 0x18a0. The 4872/4880
        // numbers belong to the iOS 16.5 class (extent 0x1660) and read as
        // zeros here -- run 44112922634 measured that mistake.
        memcpy((void *)droidvm_probe_w,base+5464,sizeof(uint64_t)*10);
        memcpy((void *)droidvm_probe_w2,base+5792,sizeof(uint64_t)*4);
        if(droidvm_probe_w[2] || droidvm_probe_w[1])droidvm_probe_ready_ticks=droidvm_probe_ready_ticks+1;
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

    # Count entries into our override. The pump only calls it when a swap is
    # pending, and the pending probe never fires, so any non-zero count here is
    # the original submit path calling virtual slot 276 directly.
    kick_body = '    void kickSwapQueue() override {\n        armVsync();'
    if kick_body not in text:
        fail('armVsync() anchor missing: cannot count kickSwapQueue entries')
    text = text.replace(
        kick_body,
        '    void kickSwapQueue() override {\n'
        '        droidvm_probe_kick_calls=droidvm_probe_kick_calls+1;\n'
        '        armVsync();', 1)

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



PROBE_SHAPE_HEAD = """// Plain types on purpose: this block is prepended before any #include, so
// uint32_t / uint64_t are not declared yet at this point.
extern "C" volatile unsigned int native_legacy_sel_shape[96][6];
volatile unsigned int native_legacy_sel_in[96][16];
volatile unsigned long long native_legacy_sel_scalar[96][4];
// First 64 bytes of each selector's structure input and its scalars: enough to
// see what the compositor asks the display to accept (run 44198165323 showed
// selector 17 writing the same 3084-byte structure selector 27 reads).
extern "C" volatile unsigned int native_legacy_sel_in[96][16];
extern "C" volatile unsigned long long native_legacy_sel_scalar[96][4];
extern "C" volatile unsigned long long native_legacy_lie_mask;
extern "C" volatile unsigned long long native_legacy_lie_mask_hi;
static unsigned long long droidvm_lie_loaded;
volatile unsigned int native_legacy_sel_shape[96][6];
volatile unsigned long long native_legacy_lie_mask;
volatile unsigned long long native_legacy_lie_mask_hi;
"""

PROBE_SHAPE_OLD = """        if(selector<kSelectors){++stat->selector_calls[selector];if(status)++stat->selector_fails[selector];stat->selector_status[selector]=(uint32_t)status;}
    }
    return status;
}"""

PROBE_SHAPE_NEW = """        if(selector<kSelectors){++stat->selector_calls[selector];if(status)++stat->selector_fails[selector];stat->selector_status[selector]=(uint32_t)status;}
    }
    if(selector<96) {
        native_legacy_sel_shape[selector][0]=native_legacy_sel_shape[selector][0]+1;
        if(args) {
            native_legacy_sel_shape[selector][1]=(uint32_t)args->scalarInputCount;
            native_legacy_sel_shape[selector][2]=(uint32_t)args->scalarOutputCount;
            native_legacy_sel_shape[selector][3]=(uint32_t)args->structureInputSize;
            native_legacy_sel_shape[selector][4]=(uint32_t)args->structureOutputSize;
        }
        native_legacy_sel_shape[selector][5]=(uint32_t)status;
        if(args) {
            if(args->structureInput && args->structureInputSize)
                memcpy((void *)native_legacy_sel_in[selector],args->structureInput,args->structureInputSize<64?args->structureInputSize:64);
            if(args->scalarInput)
                memcpy((void *)native_legacy_sel_scalar[selector],args->scalarInput,(args->scalarInputCount<4?args->scalarInputCount:4)*8);
        }
    }
    if(status==kIOReturnUnsupported) {
        if(selector<64 && ((native_legacy_lie_mask>>selector)&1ULL))status=kIOReturnSuccess;
        else if(selector>=64 && selector<96 && ((native_legacy_lie_mask_hi>>(selector-64))&1ULL))status=kIOReturnSuccess;
    }
    return status;
}"""


def patch_probe(root):
    path = root / 'UniXNU-droidvm/package/LegacyDisplayProbe.cpp'
    if not path.exists():
        fail('LegacyDisplayProbe.cpp not found at %s' % path)
    text = path.read_text()
    if 'native_legacy_sel_shape' in text:
        print('selector-shape probe already present')
        return
    if PROBE_SHAPE_OLD not in text:
        fail('wrappedExternal tail changed: cannot add the selector-shape probe')
    text = text.replace(PROBE_SHAPE_OLD, PROBE_SHAPE_NEW, 1)
    text = PROBE_SHAPE_HEAD + text
    # lie mask, read once from the boot argument
    prologue = '    ClientStat *stat=statFor(client);\n'
    lie_setup = (
        '    if(!droidvm_lie_loaded){droidvm_lie_loaded=1;unsigned long long m=0,mhi=0;\n'
        '        PE_parse_boot_argn("legacy-lie",&m,sizeof m);\n'
        '        PE_parse_boot_argn("legacy-lie-hi",&mhi,sizeof mhi);\n'
        '        native_legacy_lie_mask=m;native_legacy_lie_mask_hi=mhi;}\n'
    )
    text = text.replace(prologue, prologue + lie_setup, 1)
    path.write_text(text)
    print('selector-shape probe installed; lie mask from legacy-lie=<hex>')


def main():
    patch_virtio_3d(ROOT)
    patch_display(ROOT)
    patch_probe(ROOT)


if __name__ == '__main__':
    main()
