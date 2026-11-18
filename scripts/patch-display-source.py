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
// uint32_t / uint64_t are not declared yet at this point. Definitions live
// inside the same extern "C" block as the uses, otherwise C++ gives them a
// different language linkage than the declaration (run 44221920492).
extern "C" {
volatile unsigned int native_legacy_sel_shape[96][6];
volatile unsigned int native_legacy_sel_in[96][16];
volatile unsigned long long native_legacy_sel_scalar[96][4];
volatile unsigned long long native_legacy_sel_out[96][4];
volatile unsigned long long native_legacy_lie_mask;
volatile unsigned long long native_legacy_lie_mask_hi;
}
static unsigned long long droidvm_lie_loaded;
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
            // What the client actually receives: without this the size and
            // refresh the compositor reads back are unknown (selector 8 asked
            // four times in run 44225411284 and nothing was composited).
            if(args->scalarOutput && args->scalarOutputCount)
                memcpy((void *)native_legacy_sel_out[selector],args->scalarOutput,(args->scalarOutputCount<4?args->scalarOutputCount:4)*8);
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



IOS_IDENTITY_OLD = """                valid=native_legacy_gop_mapping_observed(this,bootMapping,width,height);"""

IOS_IDENTITY_NEW = """                valid=native_legacy_gop_mapping_observed(this,bootMapping,width,height);
                // iOS: a display has to identify itself before its clients trust
                // it (macOS's WindowServer reads IOMFBUUID and the EDID; QuartzCore
                // on iOS asks the family for the same things). The macOS branch
                // publishes these through native_legacy_ioav_* helpers that this
                // build does not contain at all, so the same identity is assembled
                // here directly: a valid EDID 1.3 with one detailed timing for the
                // mode startHardware scans out, plus a stable UUID string.
                if(valid && bootMapping) {
                    uint8_t edid[128];
                    bzero(edid,sizeof edid);
                    edid[0]=0x00;edid[1]=0xff;edid[2]=0xff;edid[3]=0xff;edid[4]=0xff;edid[5]=0xff;edid[6]=0xff;edid[7]=0x00;
                    edid[8]=0x30;edid[9]=0x0a;                 // manufacturer id "UNX"
                    edid[10]=0x02;edid[11]=0x00;               // product code
                    edid[12]=0x01;edid[13]=0x00;edid[14]=0x00;edid[15]=0x00;
                    edid[16]=0x01;edid[17]=0x03;               // EDID 1.3
                    edid[18]=0x80;                             // digital input
                    edid[19]=0x46;edid[20]=0x27;               // sRGB, preferred timing, 8-bit
                    edid[21]=0x8d;edid[22]=0x0e;edid[23]=0x00;
                    edid[24]=0x0a;                             // no chroma coordinates
                    edid[38]=1;edid[39]=1;                     // one established timing
                    edid[54]=0;edid[55]=0;                     // no standard timings
                    {
                        // Detailed timing descriptor, CVT reduced blanking:
                        // h: 48 front / 32 sync / 80 back, v: 3 front / 6 sync / 14 back.
                        const uint32_t hActive=width, vActive=height;
                        const uint32_t hFront=48, hSync=32, hBack=80;
                        const uint32_t vFront=3, vSync=6, vBack=14;
                        const uint32_t hBlank=hFront+hSync+hBack, vBlank=vFront+vSync+vBack;
                        const uint32_t clock10k=(uint32_t)(((uint64_t)(hActive+hBlank)*(vActive+vBlank)*60+5000)/10000);
                        uint8_t *d=edid+54;
                        d[0]=(uint8_t)(clock10k&0xff);d[1]=(uint8_t)((clock10k>>8)&0xff);
                        d[2]=(uint8_t)(hActive&0xff);d[3]=(uint8_t)(hBlank&0xff);
                        d[4]=(uint8_t)((((hActive>>8)&0x0f)<<4)|((hBlank>>8)&0x0f));
                        d[5]=(uint8_t)(vActive&0xff);d[6]=(uint8_t)(vBlank&0xff);
                        d[7]=(uint8_t)((((vActive>>8)&0x0f)<<4)|((vBlank>>8)&0x0f));
                        d[8]=(uint8_t)(hFront&0xff);d[9]=(uint8_t)(hSync&0xff);
                        d[10]=(uint8_t)(((vFront&0x0f)<<4)|(vSync&0x0f));
                        d[11]=(uint8_t)((((hFront>>8)&0x03)<<6)|(((hSync>>8)&0x03)<<4)|(((vFront>>4)&0x03)<<2)|((vSync>>4)&0x03));
                        d[12]=0;d[13]=0;d[14]=0;d[15]=0;d[16]=0;
                        d[17]=0x1a;   // digital separate sync, +h +v
                    }
                    {   // monitor name descriptor
                        uint8_t at=54+18;
                        edid[at]=0x00;edid[at+1]=0x00;edid[at+2]=0x00;edid[at+3]=0xfc;edid[at+4]=0x00;
                        const char *name="UniXNU Display";
                        for(unsigned i=0;i<13;++i)edid[at+5+i]=(uint8_t)(name[i]?name[i]:0x20);
                        edid[at+5+13]=0x0a;
                    }
                    {   // range limits descriptor
                        uint8_t at=54+36;
                        edid[at]=0x00;edid[at+1]=0x00;edid[at+2]=0x00;edid[at+3]=0xfd;edid[at+4]=0x00;
                        edid[at+5]=50;edid[at+6]=75;edid[at+7]=30;edid[at+8]=90;edid[at+9]=0x00;
                        edid[at+10]=0x0a;edid[at+11]=0x20;edid[at+12]=0x20;edid[at+13]=0x20;edid[at+14]=0x20;
                    }
                    edid[126]=0;                                // no extension block
                    {unsigned sum=0;for(unsigned i=0;i<127;++i)sum+=edid[i];edid[127]=(uint8_t)(0x100-(sum&0xff));}
                    OSData *data=OSData::withBytes(edid,sizeof edid);
                    if(data){setProperty("NativeDisplayEDID",data);setProperty("EDID",data);data->release();}
                    setProperty("IOMFBUUID","554E5800-0000-4000-8000-000000000001");
                    setProperty("DisplayWidth",width,32);
                    setProperty("DisplayHeight",height,32);
                    IOLog("NativeLegacyDisplay: identity published %ux%u clock %u ok",width,height,(unsigned)(((uint64_t)(width+160)*(height+23)*60+5000)/10000));
                }"""


def patch_ios_identity(root):
    path = root / 'UniXNU/drivers/GopFramebuffer/NativeLegacyDisplay.cpp'
    text = path.read_text()
    if 'identity published' in text:
        print('iOS identity publisher already present')
        return
    if IOS_IDENTITY_OLD not in text:
        fail('gop mapping anchor missing: cannot publish the iOS display identity')
    text = text.replace(IOS_IDENTITY_OLD, IOS_IDENTITY_NEW, 1)
    path.write_text(text)
    print('iOS identity publisher installed')


def main():
    patch_virtio_3d(ROOT)
    patch_display(ROOT)
    patch_probe(ROOT)
    patch_ios_identity(ROOT)


if __name__ == '__main__':
    main()
