# 先别编：带 virtio-mmio 的 crosvm 已经有现成的

> 2026-11-13 22:40。写完 `build-crosvm.yml` 之后又找到了下面这条捷径，所以那个 workflow 现在**只是备胎**。

## 现成的在哪

`Droid-VM/DroidVM-Prebuilts` 仓库，分支 **`wip/hackintosh`**，文件 `prebuilt-arm64-v8a.tar.xz`（75,726,164 B）：

```
usr/bin/crosvm          14,490,512 B   sha256 7994700e…
usr/lib/*.so            它旁边的库，RUNPATH=$ORIGIN
```

对比：DroidVM APK 自带的 crosvm 是 13,048,616 B、sha256 `cd3693ba…`，`strings | grep -c virtio-mmio` = **0**；
这一份是 **34**，而且 `run --help` 里有 `--virtio-mmio-block` / `-input` / `-net` / `-gpu`、`--virtio-media`、`--battery`、`--pre-alloc`。

## 下载

单连接会反复断，`curl -C -` 还会把文件拼坏（实测拿到 77 MB 的废包）。用分段并发：

```bash
URL=https://raw.githubusercontent.com/Droid-VM/DroidVM-Prebuilts/wip/hackintosh/prebuilt-arm64-v8a.tar.xz
TOTAL=75726164; N=8; CH=$(( (TOTAL+N-1)/N ))
for i in $(seq 0 $((N-1))); do
  s=$((i*CH)); e=$((s+CH-1)); [ $e -ge $TOTAL ] && e=$((TOTAL-1))
  curl -s --max-time 120 -x socks5h://127.0.0.1:7897 -r "$s-$e" "$URL" > part.$i &
done; wait
cat part.* > prebuilt-arm64-v8a.tar.xz
```

## 部署与起机

```bash
# 手机侧：crosvm 和它的 .so 放同一目录（RUNPATH=$ORIGIN）
adb push crosvm *.so /data/local/tmp/cvm/

# 起机：引导盘走 --block（PCI，EDK2 从它起），三个数据盘走 --virtio-mmio-block
# 顺序必须是 os.img → systemos.img → data-store.img（data-store 是第 3 个 mmio 块）
/data/local/tmp/cvm/crosvm run --name unixnu --mem 2048 --cpus 2 \
  --hypervisor gunyah --protected-vm-pseudo-unprotected --no-smt --disable-sandbox --hugepages \
  --bios /data/data/cn.classfun.droidvm/usr/share/droidvm/edk2-gunyah.fd \
  --block path=/data/local/tmp/ux/droidvm-disk.img,ro=true \
  --virtio-mmio-block path=/data/local/tmp/ux/os.img,ro=true \
  --virtio-mmio-block path=/data/local/tmp/ux/systemos.img,ro=true \
  --virtio-mmio-block path=/data/local/tmp/ux/data-store.img,ro=false
```

效果（串口）：`DARWINBOOT: FDT devices: 2 disk(s) + data store, 0 input, 0 media`
—— 从之前的 `0 disk(s)` 变成能看到盘了。剩下的是输入与显示（`--virtio-mmio-input` / `--gpu` + `--virtio-mmio-gpu`）。

## 调试：直接读 guest RAM

guest 内存是 `memfd:crosvm_guest`（fd 5）。`/proc/<pid>/mem` 被 `PR_SET_DUMPABLE=0` 挡住，
但**走 fd 路径能读**：`guest PA = memfd 偏移 + 0x80000000`。

```bash
P=$(pidof crosvm); dd if=/proc/$P/fd/5 bs=4096 skip=$(( (0x84A7C000-0x80000000)/4096 )) count=1
```

客机没有串口控制台（上游 rig 也是靠这个方法读 msgbuf / panic）。

## 那 build-crosvm.yml 还要吗

留着。它是另一条路（从 AOSP soong 自己编 `wip/virtio-mmio`），用途：
现成的这份缺少 `--virtio-mmio-snd`/`-media`，或者以后需要跟上游 commit 对齐时。
`stage=prepare` 只 sync 不编译，代价很低。
