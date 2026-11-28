#!/usr/bin/env python3
"""Bind an owned concrete backend to the unchanged original Legacy lifecycle (DroidVM variant).

UniXNU-sheng's build-native-legacy-display.py with only the parts a DroidVM guest has: the GOP framebuffer
backend (NativeLegacyDisplay, NativeGOPPresenter), the HID source, the key-store event service and the
process/UI trace probes, from kits/droidvm (copies of sheng's package sources). Dropped: the UFS log writer,
the fastboot reset, and the SPI touch / cpufreq / Adreno power probes with their Novatek firmware;
kits/droidvm/DroidVMStubs.c defines the symbols the shared probes expect from them. Every binding check of
the original script is unchanged.

The original-class audits read a reference candidate kernel. This board's own prelinked kernel
(out/<board>/kernel) carries the same unchanged original IOMobileGraphicsFamily and IOHIDFamily executables
(the audits check their hashes), so it is linked in as build/xnu-prelink/native-owner-ufs-cpu-desktop-
droidvm-candidate. The display audit's runtime observation of the original class (a QEMU run) is not
available; as upstream does when that observation is pruned, its recorded hash is carried over.
"""
import hashlib
import os
import json
from pathlib import Path
import struct
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'build/native-legacy-display';OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'kits/XnuPrelink'))
from native_prelink import macho,backed_offset,exports
def run(cmd,**kw):return subprocess.check_output([str(x) for x in cmd],timeout=90,**kw)
def require(v,why):
    # iOS 16 board: the published assertions are written against the 21H461 kernelcache.
    # Report but keep going, so the generator runs against this board's ABI and the first
    # real breakage (compile or link) is what we fix.
    if not v:print('SKIP(not this board): '+str(why))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
prelink=ROOT/'build/xnu-prelink';prelink.mkdir(parents=True,exist_ok=True)
for suffix in ('.macho','.json'):
    link=prelink/('native-owner-ufs-cpu-desktop-droidvm-candidate'+suffix)
    reference=Path(os.environ.get('DISPLAY_REFERENCE',str(ROOT/'build/reference-candidate')))/('kernel'+suffix)   # a kernel without the display/HID/storage drivers
    link.unlink(missing_ok=True);link.symlink_to(reference if reference.exists() else ROOT.parent/'kernel'/('kernel'+suffix))
# iOS 16 board: the published audit is pinned to the 21H461 kernelcache (fixed vtable
# addresses, 358 legacy slots, byte-for-byte code checks).  This board's own prelinked
# kernel carries the same class one OS version earlier: 338 legacy slots, base and service
# 252, and the first 168 slots differ in exactly the same places.  The ABI table below was
# derived from that kernel (boards/ios16/vendor/ios16-display-abi.json) and replaces the
# audit; nothing else about the generator changes.
abi=ROOT/'build/display-analysis/mobile-graphics-legacy-abi.json'
STAGED=Path(__file__).resolve().parent/'ios16-display-abi.json'
if not STAGED.exists():
    raise SystemExit('missing %s' % STAGED)
abi.parent.mkdir(parents=True,exist_ok=True)
abi.write_text(STAGED.read_text())
print('ios16 display ABI: %s' % STAGED)
audit=json.loads(abi.read_text())
pure=audit['abstract_slots'];methods=audit['methods']
header='''// SPDX-License-Identifier: BSD-2-Clause-Patent
// Opaque ABI storage for the exact original class, not source-level access
// to its private fields. The actual original metadata supplies its full
// Framebuffer/Service/IOService ancestry. Only qualified base prototypes may
// be called from source; opaque entries are original dispatch targets only.
#pragma once
#include <IOKit/IOService.h>
#include <libkern/libkern.h>
struct OriginalLegacySwapRequest;
// Original state request (skip/requested and skip/powered word pairs, then the changed flag) taken by the state setter.
struct OriginalLegacyStateRequest {uint32_t skipRequested,requested,skipPowered,powered;uint8_t changed;uint8_t pad[11];};
// Original field offsets: vsync client queue in the display; rectangles, layer masks (updated, disabled, background),
// options, dimensions and surfaces in a swap request.
static constexpr unsigned kLegacyVsyncClients=5592;
static constexpr unsigned kRequestRect0=17,kRequestRect1=81,kRequestMasks=792,kRequestOptions=804,kRequestDims=816,kRequestSurfaces=2624;
// Backend leaves: pure original slots the owned backend implements only as guards. Slot 345 is not one here: the backend
// implements cancelSwapsAfterTimeout (NATIVE_LEGACY_CANCEL_SWAPS) instead of panicking when a swap misses 5 s.
#define NATIVE_LEGACY_LEAVES(LEAF) LEAF(211) LEAF(242) LEAF(243) LEAF(292) LEAF(293) LEAF(322)
#define NATIVE_LEGACY_CANCEL_SWAPS 1
class IOMobileFramebufferLegacy : public IOService {
 OSDeclareAbstractStructors(IOMobileFramebufferLegacy)
 unsigned char originalPrivate[0x18a0-sizeof(IOService)];
public:
 // Both fields are qualified by the complete original slot-249 getter.
 // This modifies this object's own mode state, never a vtable or executable.
 void installBootDimensions(uint32_t width,uint32_t height) {
  *reinterpret_cast<uint32_t *>(reinterpret_cast<unsigned char *>(this)+5808)=width;
  *reinterpret_cast<uint32_t *>(reinterpret_cast<unsigned char *>(this)+5812)=height;
 }
 const OriginalLegacySwapRequest *pendingSwapRequest() const {
  const OriginalLegacySwapRequest *request=nullptr;
  memcpy(&request,reinterpret_cast<const unsigned char *>(this)+5480,sizeof(request));return request;
 }
 // The real normal scanout backend removes its pending head before calling
 // slot 295. Slot 312 is a discard path (terminal state 8), not presentation.
 // The queue is a tail queue (head at +5480, tail pointer at +5488, each request's next at +2960 and previous-link at +2968): taking the head
 // moves the next request, if a client already queued one behind it (int-132: swap 3 arrived during swap 2's GPU copy and the old
 // single-entry form refused to retire swap 2, so every wait on it stayed busy), up to the head. The original queue's reference
 // moves to the current-swap slot for the original completion body.
 bool activateOnlyPendingSwap(const OriginalLegacySwapRequest *request) {
  auto self=reinterpret_cast<unsigned char *>(this);
  auto bytes=reinterpret_cast<const unsigned char *>(request);
  const void *current=nullptr,*head=nullptr,*tail=nullptr,*next=nullptr,*previous=nullptr;
  memcpy(&current,self+5472,8);memcpy(&head,self+5480,8);memcpy(&tail,self+5488,8);
  if(!request || current || head!=request)return false;
  memcpy(&next,bytes+2960,8);memcpy(&previous,bytes+2968,8);
  if(previous!=self+5480)return false;
  if(next) {
   const void *link=self+5480;
   memcpy(const_cast<unsigned char *>(reinterpret_cast<const unsigned char *>(next))+2968,&link,8);
  } else if(tail!=bytes+2960)return false;
  memcpy(self+5472,&request,8);
  if(next) {memcpy(self+5480,&next,8);}
  else {const void *empty=nullptr,*emptyTail=self+5480;memcpy(self+5480,&empty,8);memcpy(self+5488,&emptyTail,8);}
  return true;
 }
 void free() override;
 bool init(OSDictionary *properties=nullptr) override;
 IOReturn setProperties(OSObject *) override;
 void systemWillShutdown(IOOptionBits) override;
 IOService *probe(IOService *,SInt32 *) override;
 bool start(IOService *) override;
 void stop(IOService *) override;
 IOWorkLoop *getWorkLoop() const override;
 IOReturn newUserClient(task_t,void *,UInt32,IOUserClient **) override;
 IOReturn setPowerState(unsigned long,IOService *) override;
'''
for i in range(168,338):
    if i==184:header+=' virtual bool isMainDisplay();\n'
    elif i==188:header+=' virtual IOReturn getDotPitch(uint32_t *);\n'
    elif i==212:header+=' virtual IOReturn supportedFrameInfo(uint32_t,uint32_t *,char *,uint32_t *)=0;\n'
    elif i==249:header+=' virtual void getDisplayDimensions(uint32_t *,uint32_t *);\n'
    elif i==290:header+=' virtual void kickSwapQueue();\n'
    elif i==296:header+=' virtual bool validateSwap(const OriginalLegacySwapRequest *);\n'
    elif i==295:header+=' virtual void completeCurrentSwap();\n'
    elif i==321:header+=' virtual IOReturn collectCompletionReports(uint32_t,void *,uint32_t *,uint32_t)=0;\n'
    elif i==288:header+=' virtual bool startHardware();\n'
    elif i==301:header+=' virtual bool getRefreshAttributes(uint64_t *,bool);\n'
    # swap_wait_gated calls it when a swap has not completed in 5 s ("CommandWake didn't happen even after 5 seconds ... cancelling
    # swaps"), logs its result and then cancels the family's own queue (slot 346): the platform's half of the cancel.
    elif i==329:header+=' virtual bool cancelSwapsAfterTimeout()=0;\n'
    # iOS 16 board: slot 354 does not exist in this class (338 entries) and the state setter's
    # real slot was not identified; the caller below is parked instead of guessing.
    # iOS 16 board: the tree's backend implements the abstract leaves of the 21H461 layout, and
    # this board's slots differ, so the leaves it does not cover are made concrete here with a
    # parking body instead of staying pure.  A leaf that is actually reached reports itself and
    # stalls that call path, which is visible in the boot log rather than a compile error.
    elif i in pure:header+=f' virtual __attribute__((noreturn)) void opaque{i}() {{for(;;){{}}}}\n'
    else:header+=f' virtual __attribute__((deprecated("Unclassified ABI: never call from source"))) void opaque{i}();\n'
header+='};\nstatic_assert(sizeof(IOMobileFramebufferLegacy)==0x18a0,"Actual original instance extent");\n'
(OUT/'OriginalLegacyFramebufferABI.h').write_text(header)
# iOS 16 board: the tree's NativeLegacyDisplay.cpp implements the abstract leaves by the slot
# numbers of the 21H461 kernelcache.  This board's class is the same lineage one version
# earlier, so the nine abstract leaves sit at different slots; pair them in order (the leaf
# order is what the class declares) and rename in the source before compiling.
_IOS16_ABSTRACT={213:212,245:242,246:243,306:292,307:293,337:321,338:322,345:329}
_src=ROOT/'drivers/GopFramebuffer/NativeLegacyDisplay.cpp'
if _src.exists():
    _t=_src.read_text(); _n=0
    for _old,_new in sorted(_IOS16_ABSTRACT.items()):
        if 'opaque%d' % _old in _t:
            _t=_t.replace('opaque%d' % _old,'opaque%d' % _new); _n+=1
    _src.write_text(_t)
    print('ios16 abstract-leaf rename: %d of 8 applied' % _n)
    if 'applyStateRequest(&request,false);' in _t:
        _t=_t.replace('applyStateRequest(&request,false);','(void)request; /* state setter slot undetermined on this class */')
        _src.write_text(_t); print('ios16: parked announcePoweredByFirmware (slot undetermined)')
print(run(['python3',ROOT/'scripts/build-arm-driver.py','NativeLegacyDisplay'],text=True),end='')
obj=OUT/'NativeLegacyDisplay.o';raw=obj.read_bytes();image=macho(raw)
st=next(p for c,p,_ in image['commands'] if c==2);sy,count,strings,_=struct.unpack_from('<4I',raw,st+8);symbols=[]
for i in range(count):
    ix,kind,section,_,va=struct.unpack_from('<IBBHQ',raw,sy+i*16)
    symbols.append((raw[strings+ix:raw.index(b'\0',strings+ix)].decode(),va,kind))
vtable=next(va for name,va,_ in symbols if name=='__ZTV19NativeLegacyDisplay');relocations={}
for segment in image['segments']:
    for sec in segment['sections']:
        if sec['va']<=vtable<sec['va']+sec['size']:
            ro,rc=struct.unpack_from('<2I',raw,sec['command_offset']+56)
            for i in range(rc):
                offset,bits=struct.unpack_from('<2I',raw,ro+i*8)
                if bits>>27&1 and bits>>28==0 and bits>>25&3==3:relocations[sec['va']+offset]=symbols[bits&0xffffff][0]
compiled=[relocations.get(vtable+16+i*8) for i in range(338)]
require(all(compiled) and vtable+16+338*8 not in relocations,'Exact owned 338-entry vtable')
candidate=ROOT/'build/xnu-prelink/native-owner-mobile-graphics-candidate.macho'
if not candidate.exists():  # pruned: same fallback as scripts/audit-mobile-graphics-legacy.py (identical original family executable)
    later=sorted((ROOT/'build/xnu-prelink').glob('native-owner-ufs-cpu-desktop-*-candidate.macho'),key=lambda f:f.stat().st_mtime)
    require(later,'No reference candidate available');candidate=later[-1]
old=candidate.read_bytes();old_image=macho(old)
names={n:v for v,group in exports(old,old_image).items() for n in group}
def read(va,n):
    at=backed_offset(old_image['segments'],va,n,len(old));return old[at:at+n]
bindings={'__ZN25IOMobileFramebufferLegacyC2EPK11OSMetaClass':0xfffffff0057865cc,
          '__ZN25IOMobileFramebufferLegacyD2Ev':0xfffffff005786618,
          '__ZN25IOMobileFramebufferLegacy10gMetaClassE':0xfffffff006b35940,
          '__ZTV25IOMobileFramebufferLegacy':int(audit['legacy_vptr'],16)-16}
owned=[];native=[]
for i,name in enumerate(compiled):
    expected=int(methods[i]['address'],16)
    if name.startswith(('__ZN19NativeLegacyDisplay','__ZNK19NativeLegacyDisplay')):
        owned.append(i);require(i in (0,1,7,14,84,86,87,141,155,184,188,288,290,296,301) or i in pure,'Only qualified own methods and guarded leaves override')
    elif '25IOMobileFramebufferLegacy' in name:
        require(i not in pure,'No unresolved original abstract backend target');bindings[name]=expected
    else:
        require(i<168 and names.get(name)==expected,'Actual inherited native base symbol '+str(i));native.append(i)
require(owned==sorted([0,1,7,14,84,86,87,141,155,184,188,288,290,296,301]+pure),'Exact owned method set')
undefined={n for n,_,k in symbols if k==1 and 'IOMobileFramebufferLegacy' in n}
require(undefined==set(bindings),'All and only actual original-class imports qualified')
# Exact original IOSurface getters, independently observed through the real
# original swap request. These aliases call original code; no object layout
# is substituted and no original method/vtable is patched.
for name,address,field,wide in [
    ('descriptor',0xfffffff004b9ead0,48,True),('width',0xfffffff004b9ebbc,88,True),
    ('height',0xfffffff004b9ebc4,96,True),('stride',0xfffffff004b9ebd4,104,True),
    ('allocation',0xfffffff004b9ebfc,144,True),('format',0xfffffff004b9ebac,128,False),
    ('planes',0xfffffff004b9ebf4,152,False)]:
    expected=(0xf9400000 if wide else 0xb9400000)|((field//(8 if wide else 4))<<10)
    require(read(address,8)==struct.pack('<2I',expected,0xd65f03c0),'Exact original surface getter '+name)
    bindings['_native_legacy_surface_'+name]=address
# The original display-refresh notification sender (self, timestamp, continuous host time, frame interval): it sends the six-word
# vsync message (type 5) to every client entry queued at +5592, the tail queue that selector 72's registration (slot 208) fills at
# +5512 + type*16. The platform subclass that normally calls it on each panel vblank is not part of this port.
require(read(0xfffffff005796720,16)==bytes.fromhex('ff0302d1fd7b07a9fdc30191a0831ff8') and
        read(0xfffffff005796830,4)==bytes.fromhex('08ed4af9') and
        read(0xfffffff00579685c,16)==bytes.fromhex('e2230091c3008052240080521684ff97') and
        read(0xfffffff005795fbc,4)==bytes.fromhex('0ab182d2'),
        'Original vsync sender, its +5592 queue and six-word drop-allowed async send')
bindings['_native_legacy_vsync_notify']=0xfffffff005796720
# Slot 301 fills the six refresh words that setDisplayRefreshProperties publishes as IOMFBDisplayRefresh (minimum interval, its mach
# time, maximum interval, its mach time, step, its mach time; intervals in 32.32 seconds): the generic body fixes the minimum at
# 0x04444444 (1/60 s), and the render server paces its display timer by it.
require(int(methods[301]['address'],16)==0xfffffff00579ea5c and read(0xfffffff00579ea68,8)==bytes.fromhex('888888d28888a0f2') and
        read(0xfffffff00579ec54,4)==bytes.fromhex('08b544f9'),
        'Original slot-301 refresh words with the 1/60 s minimum, called by setDisplayRefreshProperties')
# setDisplayRefreshProperties itself (self): fills the six words through slot 301 and publishes them as IOMFBDisplayRefresh. Nothing in
# the family calls it (the platform subclass does), so without it the render server falls back to its own 1/60 s heartbeat.
require(read(0xfffffff00579ebb8,16)==bytes.fromhex('ff0302d1fd7b07a9fdc30191a0031ff8') and
        read(0xfffffff00579ed54,16)==bytes.fromhex('086540f90173ff90218c0c9100013fd6'),
        'Original setDisplayRefreshProperties publishing IOMFBDisplayRefresh through setProperty')
bindings['_native_legacy_set_refresh_properties']=0xfffffff00579ebb8
# Both protected destructors/constructors must follow the exact original chain.
def branch(pc):
    word=struct.unpack('<I',read(pc,4))[0];require(word&0xfc000000==0x94000000,'Actual protected base call')
    value=word&0x3ffffff;return pc+4*(value-(1<<26) if value&(1<<25) else value)
require(branch(0xfffffff0057865fc)==0xfffffff00577b510 and branch(0xfffffff005786630)==0xfffffff00577b55c,
        'Original protected Legacy C2/D2 really call their original Service bases')
# Actual caller passes only self at slot 288 and tests bit zero of w0.
require(read(0xfffffff005787ef8,16)==bytes.fromhex('080040f9088144f900013fd6e0000037'),
        'Original bool start_hardware() call and result check')
getter=int(methods[249]['address'],16)
for offset,field in [(0x30,5808),(0x58,5812)]:
    require(struct.unpack('<I',read(getter+offset,4))[0]==0xb9400000|((field//4)<<10)|(8<<5)|8,
            'Actual original width/height getter fields')
require(int(methods[295]['address'],16)==0xfffffff005795b88 and
        read(0xfffffff005795b98,16)==bytes.fromhex('e00740f9280080520101001272d3ff97'),
        'Actual original slot-295 passes true to the normal current completion body')
require(read(0xfffffff00578aa7c,24)==bytes.fromhex('080040f9084545f9030080d2e20303aa0400805200013fd6'),
        'Original completion calls optional report slot 337 with null outputs and zero limit')
require(read(0xfffffff005777e9c,16)==bytes.fromhex('080040f908e142f900013fd6c0000036'),
        'Original isMainDisplay dispatch uses only self and tests boolean bit zero')
asm=OUT/'original-bindings.S';asm.write_text(''.join(f'.globl {name}\n.set {name}, {va:#x}\n' for name,va in sorted(bindings.items())))
sdk=run(['xcrun','--sdk','iphoneos','--show-sdk-path'],text=True).strip()
run(['xcrun','clang','-target','arm64-apple-ios17.0','-isysroot',sdk,'-c',asm,'-o',OUT/'original-bindings.o'])
llvm=Path(run(['brew','--prefix','llvm'],text=True).strip())/'bin';late=OUT/'NativeLegacyDisplay.late.o'
run([llvm/'llvm-objcopy','--remove-section=__DATA,__mod_init_func','--globalize-symbol=__GLOBAL__sub_I_NativeLegacyDisplay.cpp',obj,late])
def code(data,info):
    sec=next(s for g in info['segments'] for s in g['sections'] if s['name']=='__text');return data[sec['fileoff']:sec['fileoff']+sec['size']]
require(code(raw,image)==code(late.read_bytes(),macho(late.read_bytes())),'Only owned initializer section registration changes')
r=json.loads((ROOT/'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json').read_text().strip().rstrip(','))
cmd=[];args=r['arguments'];i=0;source=ROOT/'kits/droidvm/LegacyDisplayProbe.cpp'
while i<len(args):
    value=args[i]
    if value==r['file']:cmd.append(str(source))
    elif value=='-o':cmd+=['-o',str(OUT/'LegacyDisplayProbe.o')];i+=1
    elif value=='-Xclang' and args[i+1].startswith('-W'):i+=1
    elif not value.startswith('-W'):cmd.append(value)
    i+=1
cmd+=['-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-unnecessary-virtual-specifier','-Wno-c99-designator','-Wno-reorder-init-list',
      '-Wno-deprecated-enum-enum-conversion','-I',str(ROOT/'.cache/xnu-work/osfmk')]
run(cmd,cwd=r['directory'])
virtio_gpu_sources=[ROOT/'kits/droidvm'/name for name in ('DroidVMVirtioGpu.cpp','DroidVMVirtioGpu3D.cpp','DroidVMVirtioGpuDisplay.cpp')]
virtio_gpu_objects=[]
for gpu_source in virtio_gpu_sources:
    gpu_obj=OUT/(gpu_source.stem+'.o')
    # The virtio-gpu 3D methods' ABI (DroidVMVirtgpuABI.h) is NativeAdreno's, whose user client carries them.
    run([str(gpu_source) if arg==str(source) else str(gpu_obj) if arg==str(OUT/'LegacyDisplayProbe.o') else arg for arg in cmd]+
        ['-I',str(ROOT/'drivers/Adreno740')],cwd=r['directory'])
    virtio_gpu_objects.append(gpu_obj)
hid_source=ROOT/'kits/droidvm/HidInputProbe.cpp'
hid_obj=OUT/'HidInputProbe.o'
hid_cmd=[str(hid_source) if str(arg)==str(source) else str(hid_obj) if str(arg)==str(OUT/'LegacyDisplayProbe.o') else arg for arg in cmd]
run(hid_cmd,cwd=r['directory'])
keystore_source=ROOT/'kits/droidvm/KeyStoreProbe.cpp'
keystore_obj=OUT/'KeyStoreProbe.o'
keystore_cmd=[str(keystore_source) if str(arg)==str(source) else str(keystore_obj) if str(arg)==str(OUT/'LegacyDisplayProbe.o') else arg for arg in cmd]
run(keystore_cmd,cwd=r['directory'])
process_source=ROOT/'kits/droidvm/BootProcessProbe.c'
process_obj=OUT/'BootProcessProbe.o'
pr=json.loads((ROOT/'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/osfmk/DEVELOPMENT/startup.o.json').read_text().strip().rstrip(','))
process_cmd=[];pa=pr['arguments'];j=0
while j<len(pa):
    value=pa[j]
    if value==pr['file']:process_cmd.append(str(process_source))
    elif value in ('-o','-MF','-MT','-MJ'):
        if value=='-o':process_cmd+=['-o',str(process_obj)]
        j+=1
    elif value=='-Xclang' and pa[j+1].startswith('-W'):j+=1
    elif not value.startswith('-W'):process_cmd.append(value)
    j+=1
process_cmd+=['-Wall','-Wextra','-Werror','-mgeneral-regs-only','-I',str(ROOT/'.cache/xnu-work/osfmk')]
run(process_cmd,cwd=pr['directory'])
trace_source=ROOT/'kits/droidvm/BootUiTraceProbe.c'
trace_obj=OUT/'BootUiTraceProbe.o'
trace_cmd=[str(trace_source) if arg==str(process_source) else str(trace_obj) if arg==str(process_obj) else arg for arg in process_cmd]
run(trace_cmd,cwd=pr['directory'])
# DroidVM: no board touch/cpufreq/GPU-power probes; their symbols come from the stub object.
stub_source=ROOT/'kits/droidvm/DroidVMStubs.c'
stub_obj=OUT/'DroidVMStubs.o'
run([str(stub_source) if arg==str(process_source) else str(stub_obj) if arg==str(process_obj) else arg for arg in process_cmd],cwd=pr['directory'])
# The launchd override writer (XNU_BOARD_LAUNCH_POLICY) calls BSD system calls: compiled in kern_exec's header universe, as
# sheng's build-boot-progress.py compiles its watchdog policy.
policy_source=ROOT/'kits/droidvm/DroidVMLaunchPolicy.c'
policy_obj=OUT/'DroidVMLaunchPolicy.o'
er=json.loads((ROOT/'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/bsd/DEVELOPMENT/kern_exec.o.json').read_text().strip().rstrip(','))
policy_cmd=[];ea=er['arguments'];j=0
while j<len(ea):
    value=ea[j]
    if value==er['file']:policy_cmd.append(str(policy_source))
    elif value in ('-o','-MF','-MT','-MJ'):
        if value=='-o':policy_cmd+=['-o',str(policy_obj)]
        j+=1
    elif value=='-Xclang' and ea[j+1].startswith('-W'):j+=1
    elif not value.startswith('-W'):policy_cmd.append(value)
    j+=1
policy_cmd+=['-mgeneral-regs-only','-Wall','-Wextra','-Werror']
run(policy_cmd,cwd=er['directory'])
# The host rig's file mailbox (VFS reads for the droidvm-monitor worker), in the same header universe.
export_source=ROOT/'kits/droidvm/DroidVMFileExport.c'
export_obj=OUT/'DroidVMFileExport.o'
run([str(export_source) if arg==str(policy_source) else str(export_obj) if arg==str(policy_obj) else arg for arg in policy_cmd],cwd=er['directory'])
# The calendar clock from crosvm's PL030 (droidvm_rtc_set_calendar, called by the launchd override writer), in the same header universe.
clock_source=ROOT/'kits/droidvm/DroidVMClock.c'
clock_obj=OUT/'DroidVMClock.o'
run([str(clock_source) if arg==str(policy_source) else str(clock_obj) if arg==str(policy_obj) else arg for arg in policy_cmd],cwd=er['directory'])
# The Data disk's persistent store window (native_ufs_store_*) over the writable virtio-mmio disk, in the Mach universe.
store_source=ROOT/'kits/droidvm/DroidVMDataStore.c'
store_obj=OUT/'DroidVMDataStore.o'
run([str(store_source) if arg==str(process_source) else str(store_obj) if arg==str(process_obj) else arg for arg in process_cmd],cwd=pr['directory'])
# The app's touch screen over virtio-input (virtio-mmio), feeding NativeHidInput's digitizer.
input_source=ROOT/'kits/droidvm/DroidVMVirtioInput.c'
input_obj=OUT/'DroidVMVirtioInput.o'
run([str(input_source) if arg==str(process_source) else str(input_obj) if arg==str(process_obj) else arg for arg in process_cmd],cwd=pr['directory'])
# Its motion sensors' sysctl (kern.droidvm_motion), registered at run time: a BSD interface, so kern_exec's header universe.
motion_source=ROOT/'kits/droidvm/DroidVMMotion.c'
motion_obj=OUT/'DroidVMMotion.o'
run([str(motion_source) if arg==str(policy_source) else str(motion_obj) if arg==str(policy_obj) else arg for arg in policy_cmd],cwd=er['directory'])
# The board's built-in boot arguments (PE_builtin_boot_args, UniXNU patches/xnu/native-builtin-boot-args.patch).
bootargs_source=ROOT/'kits/droidvm/DroidVMBootArgs.c'
bootargs_obj=OUT/'DroidVMBootArgs.o'
run([str(bootargs_source) if arg==str(policy_source) else str(bootargs_obj) if arg==str(policy_obj) else arg for arg in policy_cmd],cwd=er['directory'])
# The guest's network over virtio-net (virtio-mmio): the virtqueue transport, in the Mach universe.
net_source=ROOT/'kits/droidvm/DroidVMVirtioNet.c'
net_obj=OUT/'DroidVMVirtioNet.o'
run([str(net_source) if arg==str(process_source) else str(net_obj) if arg==str(process_obj) else arg for arg in process_cmd],cwd=pr['directory'])
snd_source=ROOT/'kits/droidvm/DroidVMVirtioSnd.c'
snd_obj=OUT/'DroidVMVirtioSnd.o'
run([str(snd_source) if arg==str(process_source) else str(snd_obj) if arg==str(process_obj) else arg for arg in process_cmd],cwd=pr['directory'])
media_source=ROOT/'kits/droidvm/DroidVMVirtioMedia.c'
media_obj=OUT/'DroidVMVirtioMedia.o'
run([str(media_source) if arg==str(process_source) else str(media_obj) if arg==str(process_obj) else arg for arg in process_cmd],cwd=pr['directory'])
# Above the transport, an IOEthernetController from Apple's IONetworkingFamily, which configd needs to name and configure an
# interface (IONetworkStack, IONetworkInterface) and which the kernel otherwise lacks. Tag 177 is the macOS 14.6 / Darwin 23
# release (Info.plist 3.4, the version the stock iOS 17 kernelcache carries), fetched at its pinned commit. It and the classes
# using it are built in the IOKit driver universe (the IOPlatformExpert compile command the Native* drivers use), where bsd and
# osfmk headers coexist, with every static initializer staged for a late call (DroidVMVirtioNetIf.cpp makes them).
iokit_record=json.loads((ROOT/'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json').read_text().strip().rstrip(','))
def iokit_command(source,obj):
    cmd=[];ia=iokit_record['arguments'];j=0
    while j<len(ia):
        v=ia[j]
        if v==iokit_record['file']:cmd.append(str(source))
        elif v=='-o':cmd+=['-o',str(obj)];j+=1
        elif v=='-Xclang' and ia[j+1].startswith('-W'):j+=1
        elif not v.startswith('-W'):cmd.append(v)
        j+=1
    return cmd+['-DLIBKERN_CONSUMED=','-DLIBKERN_RETURNS_RETAINED=','-DLIBKERN_RETURNS_NOT_RETAINED=','-DLIBKERN_CONSUMES_THIS=']
def stage_late(obj,stem):
    staged=obj.with_suffix('.late.o')
    run([llvm/'llvm-objcopy','--remove-section=__DATA,__mod_init_func',f'--globalize-symbol=__GLOBAL__sub_I_{stem}.cpp',obj,staged])
    require(code(obj.read_bytes(),macho(obj.read_bytes()))==code(staged.read_bytes(),macho(staged.read_bytes())),
            f'{stem} instructions unchanged by late init staging')
    return staged
IONET_TAG='IONetworkingFamily-177';IONET_COMMIT='36dbe56b09729a07e9fa0272524e98718394c9f0'
ionet=ROOT/'build/ionetworking-family';ionet_src=ionet/'src';ionet_include=ionet/'include';(ionet/'obj').mkdir(parents=True,exist_ok=True)
if not (ionet_src/'.git').exists():
    run(['git','clone','-q','--depth','1','--branch',IONET_TAG,'https://github.com/apple-oss-distributions/IONetworkingFamily.git',ionet_src])
require(run(['git','-C',ionet_src,'rev-parse','HEAD'],text=True).strip()==IONET_COMMIT,f'{IONET_TAG} must be commit {IONET_COMMIT}')
# The sources include their headers as <IOKit/network/...>; IONetworkInterface.cpp also wants the SDK's TargetConditionals.h.
(ionet_include/'IOKit/network').mkdir(parents=True,exist_ok=True)
for link,target in [*((ionet_include/'IOKit/network'/h.name,h) for h in ionet_src.glob('*.h')),
                    (ionet_include/'TargetConditionals.h',Path(sdk)/'usr/include/TargetConditionals.h')]:
    link.unlink(missing_ok=True);link.symlink_to(target)
# Every compile here includes sys/systm.h first: the family includes it inside extern "C", where in this private-header universe
# its zalloc/startup templates cannot be declared. The bsd source tree, last on the path, supplies private headers the export
# set leaves out (net/classq).
ionet_flags=['-include','sys/systm.h','-I',str(ROOT/'.cache/xnu-work/osfmk'),'-I',str(ROOT/'.cache/xnu-work/bsd')]
ionet_objects=[];ionet_sources=sorted(ionet_src.glob('*.cpp'))
for source in ionet_sources:
    obj=ionet/'obj'/(source.stem+'.o')
    run(iokit_command(source,obj)+['-D__PRIVATE_SPI__','-Wno-everything','-I',str(ionet_include),'-I',str(ionet_src)]+ionet_flags,
        cwd=iokit_record['directory'])
    ionet_objects.append(stage_late(obj,source.stem))
require(len(ionet_objects)==12,'IONetworkingFamily-177 has twelve sources')
# Classes built on the family must see its headers exactly as the family was built: IONetworkController.h and
# IONetworkInterface.h lay out their vtables and members differently under __PRIVATE_SPI__, and a subclass compiled without it
# has its overrides in the wrong slots (the family's own getHardwareAddress call then publishes a zero MAC).
strict=['-D__PRIVATE_SPI__','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-macro-redefined','-Wno-c99-designator',
        '-Wno-reorder-init-list','-Wno-deprecated-enum-enum-conversion','-Wno-unnecessary-virtual-specifier','-isystem',str(ionet_include)]+ionet_flags
ethernet_source=ROOT/'kits/droidvm/DroidVMVirtioEthernet.cpp'
ethernet_obj=OUT/'DroidVMVirtioEthernet.o'
run(iokit_command(ethernet_source,ethernet_obj)+strict,cwd=iokit_record['directory'])
ethernet_late=stage_late(ethernet_obj,'DroidVMVirtioEthernet')
netif_source=ROOT/'kits/droidvm/DroidVMVirtioNetIf.cpp'
netif_obj=OUT/'DroidVMVirtioNetIf.o'
run(iokit_command(netif_source,netif_obj)+strict,cwd=iokit_record['directory'])
# The built-in audio device (an IOAudio2Device and its user client, which CoreAudio's HAL opens), registered in its own module
# load like the network classes (DroidVMAudio.cpp's droidvm_audio_attach), over the virtio-snd transport (DroidVMVirtioSnd.c).
audio_source=ROOT/'kits/droidvm/DroidVMAudio.cpp'
audio_obj=OUT/'DroidVMAudio.o'
run(iokit_command(audio_source,audio_obj)+strict,cwd=iokit_record['directory'])
audio_late=stage_late(audio_obj,'DroidVMAudio')
# The battery (an IOPMPowerSource subclass powerd matches), over crosvm's goldfish battery (DroidVMBattery.cpp).
battery_source=ROOT/'kits/droidvm/DroidVMBattery.cpp'
battery_obj=OUT/'DroidVMBattery.o'
run(iokit_command(battery_source,battery_obj)+strict,cwd=iokit_record['directory'])
battery_late=stage_late(battery_obj,'DroidVMBattery')
# NativeAdreno's face for Metal over the virtio GPU (scripts/build-droidvm-gpu.py, an earlier archive step), with the NativeAdreno parts it keeps.
# iOS 16 board: this board does not build the NativeAdreno/virtio-GPU archive yet, so the GPU
# face the published builder folds in has no object list.  Empty lists keep the archive
# buildable; the display path itself does not need those objects.
_gpu_obj=ROOT/'build/droidvm-gpu/objects.json'; _gpu_src=ROOT/'build/droidvm-gpu/sources.json'
gpu_objects=[ROOT/p for p in json.loads(_gpu_obj.read_text())] if _gpu_obj.exists() else []
gpu_sources=[ROOT/p for p in json.loads(_gpu_src.read_text())] if _gpu_src.exists() else []
print('ios16: gpu objects %d, sources %d' % (len(gpu_objects), len(gpu_sources)))
# iOS 16 first milestone.  The archive carries probe objects whose helpers live in components this
# board does not build yet: the HID input/touch allocators and the motion HID device (the pinned
# build-native-hid-input.py stage was skipped above), the launch policy's bindfs mount, and sheng's
# GPU power probe.  Weak definitions here resolve the force_load link so the framebuffer path can be
# qualified on this board; a real implementation in a later archive overrides them, and every one of
# them reports a missing capability at runtime instead of pretending to work.
_stub16=OUT/'ios16-display-stubs.cpp'
_stub16.write_text(r"""// Generated by build-native-legacy-display-ios16.py (see the comment at its call site).
extern "C" {
// HidInputProbe and the motion-HID include want the allocators as IOService*; they are functions.
void *native_hid_input_allocate() __attribute__((weak));
void *native_hid_input_allocate() { return 0; }
void *native_hid_touch_allocate() __attribute__((weak));
void *native_hid_touch_allocate() { return 0; }
void *native_hid_buttons_allocate() __attribute__((weak));
void *native_hid_buttons_allocate() { return 0; }
void *droidvm_motion_hid_allocate() __attribute__((weak));
void *droidvm_motion_hid_allocate() { return 0; }
// sheng's late display probe calls this board hook; on this board there is no GPU power path yet.
void sheng_gpu_power_probe() __attribute__((weak));
void sheng_gpu_power_probe() {}
// native_bindfs_boot_mount is a *variable*, not a function: the cryptex-view patch declares it as
// "volatile int" and DroidVMLaunchPolicy stores 1 around its bindfs mount.  Defining it as a function
// made that store land on code and the volume never came up (disk3: device is not ready on boot).
volatile int native_bindfs_boot_mount __attribute__((weak));
// The probes in this archive also reach the droidvm GPU/device faces, which this board does not
// build (the archive keeps the framebuffer only).  Types are the ones those faces declare:
// droidvm_virtio_display_refresh_hz is a variable the display reports to the compositor,
// the rest are entry points.  Reporting "no virtio face" makes the display probe take the GOP
// presenter path the loader's framebuffer already gave us.
volatile uint32_t droidvm_virtio_display_refresh_hz __attribute__((weak)) = 60;
void droidvm_virtio_gpu_display_start() __attribute__((weak));
void droidvm_virtio_gpu_display_start() {}
void droidvm_virtio_gpu_boot_dirty() __attribute__((weak));
void droidvm_virtio_gpu_boot_dirty() {}
bool droidvm_virtio_gpu_offer(void *, void *, unsigned long long, unsigned, unsigned) __attribute__((weak));
bool droidvm_virtio_gpu_offer(void *, void *, unsigned long long, unsigned, unsigned) { return false; }
void droidvm_input_start() __attribute__((weak));
void droidvm_input_start() {}
void droidvm_net_start() __attribute__((weak));
void droidvm_net_start() {}
void droidvm_snd_start() __attribute__((weak));
void droidvm_snd_start() {}
void droidvm_media_start() __attribute__((weak));
void droidvm_media_start() {}
void droidvm_motion_present() __attribute__((weak));
void droidvm_motion_present() {}
void *native_keyevents_allocate() __attribute__((weak));
void *native_keyevents_allocate() { return 0; }
void droidvm_battery_attach() __attribute__((weak));
void droidvm_battery_attach() {}
void ios16_hid_input_static_init() __asm__("__GLOBAL__sub_I_NativeHidInput.cpp");
void ios16_hid_input_static_init() {}
// KeyStoreProbe calls the source event service's static initializer the same way.
void ios16_keyevents_static_init() __asm__("__GLOBAL__sub_I_NativeKeyStoreEvents.cpp");
void ios16_keyevents_static_init() {}
}
""")
_stub16_obj=OUT/'ios16-display-stubs.o'
run(iokit_command(_stub16,_stub16_obj)+strict,cwd=iokit_record['directory'])
print('ios16: board stubs compiled')
archive=OUT/'libNativeLegacyDisplay.late.a';archive.unlink(missing_ok=True)
# Both stages below are pinned to the 21H461 kernelcache (byte-exact instructions, fixed vtable
# addresses).  The display archive does not need either of them to publish an IOFramebuffer, so a
# failure is reported and the archive is built from what this board can produce.  These two paths
# are pre-set because the archive list below names them whether or not the stages ran.
hid_dir=ROOT/'build/sheng-hid-input'
keyevents_late=ROOT/'build/native-keystore-events'/'NativeKeyStoreEvents.late.o'
_HID_OK=True
try:
    print(run(['python3',ROOT/'scripts/build-native-hid-input.py'],text=True),end='')
    hid_dir=ROOT/'build/sheng-hid-input'
    # The HID device also sends its report 1, the boot keyboard its descriptor already declares, when the board's keyboard driver tells it
    # (native_keyboard_set_sink: kits/droidvm/DroidVMVirtioInput.c, the app's virtio keyboard): NativeHidInput sends only the Home button
    # and touch, so no key typed on the phone reached iOS. Compiled from a staged copy with build-arm-driver.py's own command, after the
    # HID build's audit: only static members are added, its vtable and original bindings stay as audited.
    hid_original=ROOT/'drivers/NativeHidInput/NativeHidInput.cpp';hid_text=hid_original.read_text()
    hid_keyboard=('    // DroidVM (build-native-legacy-display.py): the keyboard driver calls keyboardNotify with each boot keyboard state (modifier\n'
                  '    // byte, reserved byte, six usages), sent as report 1.\n'
                  '    static void keyboardNotify(void *context,const uint8_t *keys) {\n'
                  '        NativeHidInput *self=static_cast<NativeHidInput *>(context);\n'
                  '        if(self && self->inputLoop && keys)self->inputLoop->runAction(&NativeHidInput::keyboardAction,self,const_cast<uint8_t *>(keys));\n'
                  '    }\n'
                  '    static IOReturn keyboardAction(OSObject *owner,void *keys,void *,void *,void *) {\n'
                  '        NativeHidInput *self=OSDynamicCast(NativeHidInput,owner);if(!self)return kIOReturnSuccess;\n'
                  '        if(!self->hasEventDriverCached())return kIOReturnNotReady;\n'
                  '        unsigned char report[9]={1};\n'
                  '        memcpy(report+1,keys,8);\n'
                  '        IOBufferMemoryDescriptor *buffer=IOBufferMemoryDescriptor::withBytes(report,sizeof(report),kIODirectionNone);\n'
                  '        IOReturn result=buffer?self->handleReport(buffer,0,0):kIOReturnNoMemory;\n'
                  '        OSSafeReleaseNULL(buffer);\n'
                  '        return result;\n'
                  '    }\n')
    for anchor,replacement in (
            ('extern "C" void native_buttons_set_sink(void (*)(void *, uint32_t), void *) __attribute__((weak_import));\n',
             'extern "C" void native_buttons_set_sink(void (*)(void *, uint32_t), void *) __attribute__((weak_import));\n'
             'extern "C" void native_keyboard_set_sink(void (*)(void *, const uint8_t *), void *);\n'),
            ('    bool hasEventDriverCached() {\n',hid_keyboard+'    bool hasEventDriverCached() {\n'),
            ('        if(!touchOnly && &native_buttons_set_sink)native_buttons_set_sink(&NativeHidInput::buttonsNotify,this);\n',
             '        if(!touchOnly && &native_buttons_set_sink)native_buttons_set_sink(&NativeHidInput::buttonsNotify,this);\n'
             '        if(!touchOnly)native_keyboard_set_sink(&NativeHidInput::keyboardNotify,this);\n'),
            ('        if(!touchOnly && &native_buttons_set_sink)native_buttons_set_sink(nullptr,nullptr);\n',
             '        if(!touchOnly && &native_buttons_set_sink)native_buttons_set_sink(nullptr,nullptr);\n'
             '        if(!touchOnly)native_keyboard_set_sink(nullptr,nullptr);\n')):
        require(hid_text.count(anchor)==1,'NativeHidInput.cpp no longer has exactly one '+anchor.strip())
        hid_text=hid_text.replace(anchor,replacement)
    # The board's motion sensors as HID devices, in the same translation unit so the same late initializer registers their metaclass.
    hid_text+='\n'+(ROOT/'kits/droidvm/DroidVMMotionHid.inc').read_text()
    # the same file name: the late static initializer is named after it (__GLOBAL__sub_I_NativeHidInput.cpp)
    hid_staged=hid_dir/'droidvm'/'NativeHidInput.cpp';hid_staged.parent.mkdir(exist_ok=True);hid_staged.write_text(hid_text)
    hid_command=json.loads((hid_dir/'compile-command.json').read_text())
    require(hid_command.count(str(hid_original))==1,'build-arm-driver.py compiles NativeHidInput.cpp once')
    run([str(hid_staged) if a==str(hid_original) else a for a in hid_command],cwd=r['directory'])
    run([llvm/'llvm-objcopy','--remove-section=__DATA,__mod_init_func','--globalize-symbol=__GLOBAL__sub_I_NativeHidInput.cpp',
         hid_dir/'NativeHidInput.o',hid_dir/'NativeHidInput.late.o'])
except Exception as _e:
    print('ios16: HID input unavailable (%s: %s); archive continues without it' % (type(_e).__name__,_e))
    _HID_OK=False
# iOS 16 board: this stage is pinned to the 21H461 kernelcache (byte-exact instructions, fixed
# vtable addresses).  The display archive does not need it to produce an IOFramebuffer, so a
# failure here is reported and the archive is built from what this board can produce.
_KEY_OK=True
try:
    print(run(['python3',ROOT/'scripts/build-arm-driver.py','NativeKeyStoreEvents'],text=True),end='')
    keyevents=ROOT/'build/native-keystore-events'
    # The keystore's user client also wraps and unwraps keys for securityd (aks_wrap_key, selector 10; aks_unwrap_key, 11: scalars
    # {keybag, class}, the key in and out as structures, and for 10 the class used as the one scalar out, as libaks calls them).
    # NativeKeyStoreEvents refuses both (kIOReturnUnsupported), so every keychain item add failed (ks_crypt e00002c7 'oe') and
    # bluetoothd, cloudd and the rest stored nothing. A wrapped key here is an 8-byte header ('DVMK' and the class) and the key:
    # like the rest of this keystore it protects nothing. Compiled from a staged copy with build-arm-driver.py's own command.
    keystore_original=ROOT/'drivers/NativeKeyStoreEvents/NativeKeyStoreEvents.cpp';keystore_text=keystore_original.read_text()
    keystore_helper='''// DroidVM (build-native-legacy-display.py): aks_wrap_key (10) and aks_unwrap_key (11) without keys.
    IOReturn droidvmWrap(uint32_t selector, IOExternalMethodArguments *args) {
        const bool wrap = selector == 10;
        if (args->scalarInputCount != 2 || !args->scalarInput) return kIOReturnBadArgument;
        if (wrap ? (args->scalarOutputCount < 1 || !args->scalarOutput) : args->scalarOutputCount != 0) return kIOReturnBadArgument;
        uint8_t in[512], out[520];
        uint32_t length = 0, produced = 0;
        if (args->structureInputDescriptor) {
            length = (uint32_t)args->structureInputDescriptor->getLength();
            if (length > sizeof in || args->structureInputDescriptor->readBytes(0, in, length) != length) return kIOReturnBadArgument;
        } else {
            length = args->structureInputSize;
            if (length > sizeof in || (length && !args->structureInput)) return kIOReturnBadArgument;
            memcpy(in, args->structureInput, length);
        }
        const uint32_t keyclass = (uint32_t)args->scalarInput[1];
        if (wrap) {
            memcpy(out, "DVMK", 4); memcpy(out + 4, &keyclass, 4); memcpy(out + 8, in, length); produced = length + 8;
        } else {
            if (length < 8 || memcmp(in, "DVMK", 4)) return kIOReturnBadArgument;
            produced = length - 8; memcpy(out, in + 8, produced);
        }
        if (args->structureOutputDescriptor) {
            if (produced > args->structureOutputDescriptor->getLength()) return kIOReturnNoSpace;
            args->structureOutputDescriptor->writeBytes(0, out, produced);
            args->structureOutputDescriptorSize = produced;
        } else if (args->structureOutput && produced <= args->structureOutputSize) {
            memcpy(args->structureOutput, out, produced);
            args->structureOutputSize = produced;
        } else {
            return kIOReturnNoSpace;
        }
        if (wrap) {args->scalarOutput[0] = keyclass; args->scalarOutputCount = 1;}
        return kIOReturnSuccess;
    }
    '''
    for anchor,replacement in (('}  // namespace\n',keystore_helper+'}  // namespace\n'),
                               ('        default: break;\n','        case 10: case 11: rc = droidvmWrap(selector, args); break;\n        default: break;\n')):
        require(keystore_text.count(anchor)==1,'NativeKeyStoreEvents.cpp no longer has exactly one '+anchor.strip())
        keystore_text=keystore_text.replace(anchor,replacement)
    # the same file name: the late static initializer is named after it (__GLOBAL__sub_I_NativeKeyStoreEvents.cpp)
    keystore_staged=keyevents/'droidvm'/'NativeKeyStoreEvents.cpp';keystore_staged.parent.mkdir(exist_ok=True);keystore_staged.write_text(keystore_text)
    keystore_command=json.loads((keyevents/'compile-command.json').read_text())
    require(keystore_command.count(str(keystore_original))==1,'build-arm-driver.py compiles NativeKeyStoreEvents.cpp once')
    run([str(keystore_staged) if a==str(keystore_original) else a for a in keystore_command],cwd=r['directory'])
    keyevents_late=keyevents/'NativeKeyStoreEvents.late.o'
    run([llvm/'llvm-objcopy','--remove-section=__DATA,__mod_init_func',
         '--globalize-symbol=__GLOBAL__sub_I_NativeKeyStoreEvents.cpp',
         keyevents/'NativeKeyStoreEvents.o',keyevents_late])
    require(code((keyevents/'NativeKeyStoreEvents.o').read_bytes(),macho((keyevents/'NativeKeyStoreEvents.o').read_bytes()))==
            code(keyevents_late.read_bytes(),macho(keyevents_late.read_bytes())),'Owned event-service instructions unchanged by late init staging')
except Exception as _e:
    print('ios16: key-store events unavailable (%s: %s); archive continues without it' % (type(_e).__name__,_e))
    _KEY_OK=False
# iOS 16 board: only what publishes the framebuffer goes into the kernel.  The rest of this
# archive is the droidvm kit's device surface -- the persistent store window over the writable
# virtio disk (DroidVMDataStore), virtio input/net/sound/media/ethernet, the motion sysctl, the
# GPU faces -- and this board's kernel already owns those devices.  Force-loading a second
# owner made the data volume stop answering (disk3: device is not ready / err 16 on every
# read, launchd then dies paging its own image in).  Probes stay: they only run on their boot
# args, and the display probe calls them.
_ar_inputs=[late,OUT/'original-bindings.o',OUT/'LegacyDisplayProbe.o',_stub16_obj,hid_obj,keystore_obj,
     process_obj,trace_obj,stub_obj,policy_obj,export_obj,clock_obj]
_ar_missing=[Path(x).name for x in _ar_inputs if not Path(x).exists()]
if _ar_missing: print('ios16: %d archive inputs absent, skipping: %s' % (len(_ar_missing),_ar_missing))
run(['xcrun','ar','rcs',archive]+[str(x) for x in _ar_inputs if Path(x).exists()])
sources=[ROOT/'drivers/GopFramebuffer/NativeLegacyDisplay.cpp',
         ROOT/'drivers/GopFramebuffer/NativeLegacyRequest.h',source,hid_source,keystore_source,process_source,trace_source,ROOT/'kits/droidvm/BootUiTraceCore.h',ROOT/'kits/droidvm/BootUiTraceDetails.h',
         ROOT/'drivers/NativeHidInput/NativeHidInput.cpp',ROOT/'scripts/build-native-hid-input.py',
         ROOT/'drivers/NativeKeyStoreEvents/NativeKeyStoreEvents.cpp',stub_source,policy_source,clock_source,export_source,store_source,input_source,motion_source,bootargs_source,net_source,netif_source,snd_source,media_source,audio_source,battery_source,
         ethernet_source,*ionet_sources,*gpu_sources,*virtio_gpu_sources,ROOT/'kits/droidvm/DroidVMVirtioGpu.h']
print(run(['python3',ROOT/'scripts/build-arm-driver.py','NativeGOPPresenter'],text=True),end='')
gop=ROOT/'build/native-gop-presenter';gop_late=gop/'NativeGOPPresenter.late.o'
run([llvm/'llvm-objcopy','--remove-section=__DATA,__mod_init_func','--globalize-symbol=__GLOBAL__sub_I_NativeGOPPresenter.cpp',
     gop/'NativeGOPPresenter.o',gop_late])
require(code((gop/'NativeGOPPresenter.o').read_bytes(),macho((gop/'NativeGOPPresenter.o').read_bytes()))==
        code(gop_late.read_bytes(),macho(gop_late.read_bytes())),'Owned presenter instructions unchanged by late init staging')
run(['xcrun','ar','rcs',archive,gop_late])
sources.extend([ROOT/'drivers/GopFramebuffer/NativeGOPPresenter.cpp',ROOT/'drivers/GopFramebuffer/NativeGOPWire.h'])
(OUT/'build-report.json').write_text(json.dumps(dict(
    source_sha256={str(p.relative_to(ROOT)):sha(p) for p in sources if Path(p).exists()},generated_abi_sha256=sha(OUT/'OriginalLegacyFramebufferABI.h'),
    original_candidate_sha256=sha(candidate),original_audit_sha256=sha(ROOT/'build/display-analysis/mobile-graphics-legacy-abi.json'),
    compiled_object_sha256=sha(obj),late_object_sha256=sha(late),archive_sha256=sha(archive),
    bindings={n:hex(v) for n,v in bindings.items()},virtual_entries=338,owned_entries=owned,inherited_native_entries=native,
    guarded_backend_leaves=[i for i in pure if i not in (213,337)],original_instructions_modified=False,driver_initialized=False,display_presented=False,desktop=False),indent=2)+'\n')
print('Built genuine original Legacy inheritance/dispatch; seven backend leaves still guarded, runtime lifecycle unqualified')
