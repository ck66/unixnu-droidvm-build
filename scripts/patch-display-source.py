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
extern volatile unsigned long long droidvm_probe_primary;
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
// k36: the status the CLIENT actually received. `native_legacy_sel_shape[sel][5]` and the
// per-client stats are written before emulate/lie-mask rewrite the family's answer, so they
// only say what the driver returned. This table is written last, on the way out:
//   [sel][0] calls   [sel][1] failures (final != 0)   [sel][2] last final status
// Without it the lie-mask A/B cannot be read at all: "client got success and still does not
// swap" and "client never got success" produce the same raw-status table.
volatile unsigned int native_legacy_sel_final[96][4];
volatile unsigned int native_legacy_sel_in[96][16];
volatile unsigned long long native_legacy_sel_scalar[96][4];
volatile unsigned long long native_legacy_sel_out[96][4];
volatile unsigned long long native_legacy_lie_mask;
volatile unsigned long long native_legacy_lie_mask_hi;
volatile unsigned long long droidvm_probe_primary;
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
    if(selector<96) {
        native_legacy_sel_final[selector][0]=native_legacy_sel_final[selector][0]+1;
        if(status)native_legacy_sel_final[selector][1]=native_legacy_sel_final[selector][1]+1;
        native_legacy_sel_final[selector][2]=(unsigned int)status;
        // The two selectors whose *success* is the open question: 8 (getInformation) is the
        // one that already succeeds before any masking, 9 (setVSyncNotifications) has never
        // been called at all, 6 (swap) is called once and then never again.
        native_legacy_sel_final[selector][3]=(unsigned int)(status==kIOReturnSuccess?1:0);
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
                droidvm_probe_primary=primary?1:0;
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



EMULATE_HEAD = """// DroidVM: the selectors' real meanings are documented (IOMobileFramebuffer
// reference): 9 setVSyncNotifications, 12 requestPowerChange, 15 setDebugFlags,
// 17 setGammaTable, 19 setWhiteOnBlackMode, 22 setDisplayDevice,
// 27 getGammaTable, 33 setVideoPowerSaving, 50 setBrightnessCorrection,
// 74 supportedFrameInfo, 79 changeFrameInfo. The family answers all of them
// kIOReturnUnsupported on this backend, so the compositor gets no usable
// display. 3084 bytes is exactly three 0x101-entry gamma tables (3 * 1028).
static int native_legacy_emulate=0;
static int native_legacy_emulate_loaded=0;
// Every user-client selector the display family sees, counted by number. The compositor's
// behaviour is otherwise invisible: 'sel 3/4/5/9 never called' was measured this way, and a
// 64-entry table answers 'what *did* it call, and in what order' for any future question.
// Not volatile: C++20 dropped increment on volatile-qualified types (that was the -Werror
// error in k34). External linkage keeps the stores alive -- the host reads them from memory.
uint32_t droidvm_selector_hits[64];
extern "C" int native_legacy_power_request(void *,unsigned);
// The display this client is attached to: the backend object when it was recorded at start, else
// the client's own provider (the display service it opened). Set on every wrapped call.
static void *droidvm_power_target=nullptr;
static IOReturn droidvm_emulate_selector(uint32_t selector,IOExternalMethodArguments *args) {
    switch(selector) {
    case 12:
        // requestPowerChange: answering success alone told the compositor the display
        // accepted a power change that never happened, so no power-state notification
        // was ever sent (macOS's CoreAnimation waits for exactly that before swapping).
        // Drive the family's own state setter instead (legacy-power=1).
        if(args && args->scalarInputCount>=1)
            native_legacy_power_request(droidvm_power_target,(unsigned)args->scalarInput[0]);
        return kIOReturnSuccess;
    case 9: case 15: case 19: case 22: case 33: case 50:
        return kIOReturnSuccess;
    case 17:
        return kIOReturnSuccess;
    case 27: {
        // Three neutral gamma ramps: each word carries two 10-bit fields.
        if(args && args->structureOutput && args->structureOutputSize>=3084) {
            uint32_t *table=static_cast<uint32_t *>(args->structureOutput);
            for(unsigned k=0;k<0x101;++k) {
                uint32_t v=(uint32_t)((k*1023u)/0x100u);
                table[k]=v|(v<<10);
            }
            memcpy(table+0x101,table,0x101*4);
            memcpy(table+0x202,table,0x101*4);
        }
        return kIOReturnSuccess;
    }
    case 28:
        if(args && args->scalarOutput && args->scalarOutputCount>=1)
            args->scalarOutput[0]=264;      // dot pitch, informational
        return kIOReturnSuccess;
    case 29:
        if(args && args->structureOutput && args->structureOutputSize>=8)
            bzero(args->structureOutput,8);
        return kIOReturnSuccess;
    case 74:
        // supportedFrameInfo: report no optional descriptors (count 0) and clear
        // the caller's 64-byte descriptor block, as the framework expects.
        if(args) {
            if(args->scalarOutput && args->scalarOutputCount>=2) {
                args->scalarOutput[0]=0;args->scalarOutput[1]=0;
            }
            if(args->structureOutput && args->structureOutputSize)
                bzero(args->structureOutput,args->structureOutputSize);
        }
        return kIOReturnSuccess;
    case 79:
        if(args && args->structureOutput && args->structureOutputSize)
            bzero(args->structureOutput,args->structureOutputSize);
        return kIOReturnSuccess;
    case 84:
        return kIOReturnSuccess;
    }
    return kIOReturnUnsupported;
}
// (68) Every legacy selector is a thin forwarder.  Disassembling the family's own dispatch
// table (90 entries x 24 bytes, shared by all three external paths) shows each handler doing
// [this+0xd8] -> [+0xd8] -> vtable[slot] and nothing else, and the k36 image carries no
// IOMobileFramebuffer/AppleDisplay vtable at all -- that chain therefore ends on the GOP nub,
// which is an IOService.  Every slot lands on an unrelated method, so the compositor is handed
// garbage; the display size answer is the one that decides whether it renders at all.
// This records what each selector actually returned and, with legacy-size=1, replaces the
// size answer with the geometry the backend really scans out.
volatile uint64_t droidvm_sel_out_first[96];
volatile uint32_t droidvm_sel_out_calls[96];
volatile uint64_t droidvm_sel_out_second[96];
volatile uint64_t droidvm_sel8_raw[2];
volatile uint32_t droidvm_sel8_calls;
volatile uint64_t droidvm_sel3_port;
volatile uint32_t droidvm_sel3_calls;
volatile uint32_t droidvm_sel_18_out;
volatile uint32_t droidvm_sel_size_fix;
static int droidvm_sel_size_loaded;
static void droidvm_after_dispatch(uint32_t selector,IOExternalMethodArguments *args,IOReturn status) {
    (void)status;
    if(!args)return;
    if(selector<96) {
        droidvm_sel_out_calls[selector]=droidvm_sel_out_calls[selector]+1;
        if(args->scalarOutput && args->scalarOutputCount>0 && !droidvm_sel_out_first[selector])
            droidvm_sel_out_first[selector]=args->scalarOutput[0];
        if(args->scalarOutput && args->scalarOutputCount>1 && !droidvm_sel_out_second[selector])
            droidvm_sel_out_second[selector]=args->scalarOutput[1];
    }
    if(selector==8) {
        droidvm_sel8_calls=droidvm_sel8_calls+1;
        if(!droidvm_sel_size_loaded) {
            droidvm_sel_size_loaded=1;
            unsigned on=0;PE_parse_boot_argn("legacy-size",&on,sizeof on);droidvm_sel_size_fix=on;
        }
        if(args->scalarOutput && args->scalarOutputCount>=2) {
            droidvm_sel8_raw[0]=args->scalarOutput[0];
            droidvm_sel8_raw[1]=args->scalarOutput[1];
            if(droidvm_sel_size_fix) {
                args->scalarOutput[0]=1024;
                args->scalarOutput[1]=1366;
            }
        }
    }
    if(selector==3) {
        droidvm_sel3_calls=droidvm_sel3_calls+1;
        if(args->scalarOutput && args->scalarOutputCount>0) droidvm_sel3_port=args->scalarOutput[0];
    }
    if(selector==18 && args->scalarOutput && args->scalarOutputCount>0)
        droidvm_sel_18_out=(uint32_t)args->scalarOutput[0];
}
"""


def patch_emulate(root):
    path = root / 'UniXNU-droidvm/package/LegacyDisplayProbe.cpp'
    text = path.read_text()
    if 'droidvm_emulate_selector' in text:
        print('selector emulation already present')
        return
    anchor = 'static IOReturn wrappedExternal('
    if anchor not in text:
        fail('wrappedExternal not found: cannot add selector emulation')
    text = text.replace(anchor, EMULATE_HEAD + anchor, 1)
    old = "    if(status==kIOReturnUnsupported) {"
    new = ("    if(status==kIOReturnUnsupported) {\n"
           "        if(!native_legacy_emulate_loaded) {native_legacy_emulate_loaded=1;\n"
           "            unsigned on=0;PE_parse_boot_argn(\"legacy-emulate\",&on,sizeof on);native_legacy_emulate=on;}\n"
           "        if(native_legacy_emulate)status=droidvm_emulate_selector(selector,args);\n"
           "    }\n"
           "    if(status==kIOReturnUnsupported) {")
    if old not in text:
        fail('lie-mask anchor missing')
    text = text.replace(old, new, 1)
    entry = '    ClientStat *stat=statFor(client);'
    if 'droidvm_selector_hits[selector]' not in text:
        if entry not in text:
            fail('wrappedExternal entry anchor missing')
        text = text.replace(entry,
            '    if(selector<64)droidvm_selector_hits[selector]=droidvm_selector_hits[selector]+1;\n' + entry, 1)
    ret_anchor = '    return status;\n}\nstatic IOReturn wrappedRegister'
    if 'droidvm_after_dispatch(selector' not in text:
        if ret_anchor not in text:
            fail('wrappedExternal return anchor missing: cannot sample selector outputs')
        text = text.replace(ret_anchor,
            '    droidvm_after_dispatch(selector,args,status);\n' + ret_anchor, 1)
    path.write_text(text)
    print('selector emulation installed (legacy-emulate=1), selector counters on, output sampler on')


READY_GLOBALS = """// DroidVM (§61): the display-ready plumbing. macOS's DroidVMDisplay, on this same VM and the same
// IOMobileFramebufferLegacy, gets a desktop out of WindowServer by doing three things the iOS build
// never did: it announces the panel as plugged in, it runs a real power-state transition whose
// notification CoreAnimation waits for, and it drives the family's own swap_begin_gated() from a
// VBL timer that is always running. On iOS the equivalents are opt-in boot arguments so one kernel
// can A/B them: legacy-power=1 (state transition on the client's requestPowerChange) and
// legacy-vsync-always=1 (refresh timer independent of whether a client registered a vsync).
extern "C" {
volatile uint64_t droidvm_probe_power_calls=0;
volatile uint64_t droidvm_probe_power_status=0;
volatile uint64_t droidvm_probe_vsync_sent=0;
volatile uint64_t droidvm_probe_vsync_ticks=0;
}
"""

READY_MEMBERS = """    // The original state setter (slot 354, applyStateRequest) takes {skipRequested, requested,
    // skipPowered, powered, changed}. The display only counts as on while both the system's and the
    // API's state want it on, so writing just the powered word (announcePoweredByFirmware, at start,
    // before any client exists) can never produce an "on" notification. Requesting off and then on
    // does, and the notification is sent to whoever is connected at that moment -- which is the
    // client that just asked for the power change.
public:
    void droidvmPowerCycle() {
        OriginalLegacyStateRequest off={0,0,0,0,1,{}};
        IOReturn first=applyStateRequest(&off,false);
        OriginalLegacyStateRequest on={0,1,0,1,1,{}};
        IOReturn second=applyStateRequest(&on,false);
        droidvm_probe_power_calls=droidvm_probe_power_calls+1;
        droidvm_probe_power_status=((uint64_t)((uint32_t)first&0xffffu)<<32)|(uint64_t)((uint32_t)second&0xffffu);
    }
private:
"""

READY_TRAMPOLINE = """
// Called from the user-client hook (LegacyDisplayProbe) when a client issues selector 12
// (requestPowerChange): run the real transition instead of only answering "success".
extern "C" int native_legacy_power_request(void *display,unsigned value) {
    unsigned on=0;PE_parse_boot_argn("legacy-power",&on,sizeof on);
    (void)value;
    if(!on || !display)return -1;
    static_cast<NativeLegacyDisplay *>(display)->droidvmPowerCycle();
    return 0;
}
"""


def patch_display_ready(root):
    """Give the compositor the three signals a working display sends (see READY_GLOBALS)."""
    path = root / 'UniXNU/drivers/GopFramebuffer/NativeLegacyDisplay.cpp'
    if not path.exists():
        fail('NativeLegacyDisplay.cpp not found at %s' % path)
    text = path.read_text()
    if 'droidvmPowerCycle' in text:
        print('display-ready plumbing already present')
        return

    # 1. the probes, next to the other globals the pump patch prepended
    anchor = 'volatile uint64_t droidvm_probe_w2[8]={0};'
    if anchor not in text:
        fail('probe globals missing: run patch_display first')
    text = text.replace(anchor, anchor + '\n' + READY_GLOBALS, 1)

    # 2. the state transition, as a class method, just before the guarded leaves
    anchor = '#define LEAF(n) __attribute__((noreturn,noinline)) void opaque##n()'
    if anchor not in text:
        fail('leaf macro anchor missing')
    text = text.replace(anchor, READY_MEMBERS + anchor, 1)

    # 3. the timer: start it even when nobody registered a vsync (deadlock breaker)
    old = ('    void startVsync() {\n'
           '        unsigned hz=native_legacy_refresh_hz();\n'
           '        if(!hz || hz>240 || !primary || !bootMapping || vsyncTimer)return;')
    new = ('    void startVsync() {\n'
           '        unsigned hz=native_legacy_refresh_hz();\n'
           '        unsigned always=0;PE_parse_boot_argn("legacy-vsync-always",&always,sizeof always);\n'
           '        if(!hz && always)hz=60;\n'
           '        if(!hz || hz>240 || !primary || !bootMapping || vsyncTimer)return;')
    if old not in text:
        fail('startVsync anchor missing')
    text = text.replace(old, new, 1)

    old = '        if(!vsyncTimer || vsyncArmed || !vsyncWanted())return;'
    new = ('        unsigned always=0;PE_parse_boot_argn("legacy-vsync-always",&always,sizeof always);\n'
           '        if(!vsyncTimer || vsyncArmed || (!always && !vsyncWanted()))return;')
    if old not in text:
        fail('armVsync anchor missing')
    text = text.replace(old, new, 1)

    old = '        if(!vsyncWanted()){native_legacy_vsync_observed(2,vsyncHz);return;}'
    new = ('        unsigned always=0;PE_parse_boot_argn("legacy-vsync-always",&always,sizeof always);\n'
           '        droidvm_probe_vsync_ticks=droidvm_probe_vsync_ticks+1;\n'
           '        if(!always && !vsyncWanted()){native_legacy_vsync_observed(2,vsyncHz);return;}')
    if old not in text:
        fail('vsyncTick anchor missing')
    text = text.replace(old, new, 1)

    old = '        native_legacy_vsync_notify(this,now,mach_continuous_time(),vsyncInterval);'
    new = ('        native_legacy_vsync_notify(this,now,mach_continuous_time(),vsyncInterval);\n'
           '        droidvm_probe_vsync_sent=droidvm_probe_vsync_sent+1;')
    if old not in text:
        fail('vsync notify anchor missing')
    text = text.replace(old, new, 1)

    # 4. the trampoline the hook calls
    text = text.rstrip('\n') + '\n' + READY_TRAMPOLINE
    path.write_text(text)
    print('display-ready plumbing installed (legacy-power=1, legacy-vsync-always=1)')

    # 5. the hook: remember which display each client talks to, so selector 12 can reach it
    hook = root / 'UniXNU-droidvm/package/LegacyDisplayProbe.cpp'
    htext = hook.read_text()
    prologue = '    ClientStat *stat=statFor(client);\n'
    if prologue not in htext:
        fail('hook prologue anchor missing')
    if 'droidvm_power_target=(void *)(activeLegacyDisplay' not in htext:
        htext = htext.replace(
            prologue,
            prologue +
            '    if(!droidvm_power_target)droidvm_power_target=(void *)(activeLegacyDisplay\n'
            '        ?(void *)activeLegacyDisplay:(client?(void *)client->getProvider():nullptr));\n', 1)
        hook.write_text(htext)
        print('hook: display target for the power request installed')


def main():
    patch_virtio_3d(ROOT)
    patch_display(ROOT)
    patch_probe(ROOT)
    patch_ios_identity(ROOT)
    patch_emulate(ROOT)
    patch_display_ready(ROOT)


if __name__ == '__main__':
    main()
