# unixnu-droidvm-build

在这个仓库的 GitHub runner 上构建 **UniXNU-droidvm** 需要的东西。两个 workflow，解决两个不同的问题：

| workflow | runner | 产出 | 解决什么 |
|---|---|---|---|
| `build-droidvm.yml` | macOS | `droidvm-disk.img`、`os.img`、`systemos.img`、`kernel.macho`、`release.json` | guest payload（iPadOS 那一套） |
| `build-crosvm.yml` | Ubuntu | `crosvm`（带 virtio-mmio）+ 它链接的 `.so` | 宿主侧的 crosvm。DroidVM APK 自带的那个只有 virtio-pci |

---

## 为什么需要第二个（crosvm）

payload 起来之后暴露的真问题：**客机看不到任何磁盘**。

串口走到 `DARWINBOOT: hand-off to the kernel` 都没问题，但 DarwinBoot 打的是
`FDT devices: 0 disk(s), 0 input, 0 media`，四个镜像的 fd 位置全程 `pos: 0`，画面停在 UniXNU 菜单。

原因不是芯片，是**插槽规格不匹配**：

- payload 的驱动与 `board.toml` 要求 `virtio-mmio`（磁盘/输入/网络/Data store/显示全是 `--virtio-mmio-*`）；
- DroidVM APK 自带的 crosvm 只有 `virtio-pci` —— `strings usr/bin/crosvm | grep -c virtio-mmio` 在稳定版和 dev 版 APK 上都是 **0**；
- ADT 是 `--dynamic` 的，DarwinBoot 只把 FDT 里的 `virtio,mmio` 节点补进 `/arm-io`，PCI 设备它不认。

作者自己用的是**另编的一个 crosvm**（手机里叫 `crosvm-mmio`，脚本 `pkill -x crosvm-mmio`，文档写
「宿主 VM 只有 virtio-mmio（crosvm-mmio，无 PCI）」），来源是 AOSP soong。作者没发 Release，公版 APK 里也没有。

> 8 Gen 3 不是问题：上游 README 的状态表写着 `UEFI → UniXNU → DarwinBoot → XNU` **works on SM8650 (8 Gen 3) and SM8850 (8 Elite Gen 5)**，桌面「4 vCPU 约 1 分钟出锁屏（SM8650）」。

---

## 用法：crosvm

Actions → 左侧 **build crosvm (virtio-mmio)** → **Run workflow**

| 输入 | 说明 |
|---|---|
| `stage` | **先跑 `prepare`**：只 `repo sync` 并打印磁盘占用，不编译。确认放得下再跑 `full` |
| `crosvm_ref` | 默认 `wip/virtio-mmio`；某个 repo 没有这个分支就回落到 `droidvm`（和 meta repo 的 `lib_branch.sh` 一致） |
| `manifest_ref` | 默认 `droidvm`（`Droid-VM/crosvm-minimal-manifest`） |
| `jobs` | 并行度，默认 4 |

`full` 的步骤：清 runner 磁盘 → 装依赖 → 装 repo/rust → `repo init/sync`（96 个工程，`--depth 1`）
→ 把 Droid-VM 的 fork 切到 `wip/virtio-mmio` → 编 boot shim（`droidvm-shim`，裸机 crate，soong 编不了）
→ `lunch aosp_arm64 trunk_staging eng` + `m crosvm_device_only`
→ 收集 crosvm 与它 `NEEDED` 的 `.so` → **自检 `strings crosvm | grep -c virtio-mmio` 必须 > 0** → 打包上传。

### 磁盘

这是唯一的未知数，所以有 `prepare` 这一档：

- runner 默认只剩约 14 GB；workflow 第一步会删掉 runner 自带的 Android SDK / .NET / Haskell / 缓存工具链（这一步是"清理"）
- AOSP 树（96 个工程，含 clang、rust、jdk、vndk 预编译）通常 15–30 GB，`out/` 再 10–20 GB
- `prepare` 会打印 `du -sh aosp/*` 和 `du -sh aosp/prebuilts/*` 的排行，直接告诉你差多少
- 如果还是不够，可选的"分步"手段（按代价从小到大）：只 sync 需要的工程（改 manifest）、
  `repo init --partial-clone`（懒加载 blob，慢但省盘）、把预编译工具链换成更小的（risky）、
  换大 runner

### 拿到之后

```bash
# 解包，得到 crosvm + 若干 .so + MANIFEST.txt（六个 fork 的 commit）
tar xzf crosvm-mmio-<run_id>.tar.gz -C crosvm-mmio
cat crosvm-mmio/MANIFEST.txt

# 推到手机（先停掉正在跑的 VM）
adb push crosvm-mmio/ /data/local/tmp/crosvm-mmio/
adb shell "su -c 'mkdir -p /data/data/cn.classfun.droidvm/bin && \
  cp /data/local/tmp/crosvm-mmio/* /data/data/cn.classfun.droidvm/bin/ && \
  chown -R \$(stat -c %u:%g /data/data/cn.classfun.droidvm) /data/data/cn.classfun.droidvm/bin && \
  chmod 755 /data/data/cn.classfun.droidvm/bin/crosvm'"
```

两个部署口径，按需要选：

1. **不动 app**：把二进制命名成 `crosvm-mmio` 放 `bin/`，自己用 `--virtio-mmio-*` 起 VM（作者就是这么干的）；
2. **替换 app 的**：备份 `usr/bin/crosvm` 后覆盖（`crosvm-deploy.sh` 的做法），这样从 UI 起也能用，
   但 app 的 daemon 是按 PCI 生成的参数，混用可能两边都不对——只在确认参数后做。

---

## 用法：payload（第一个 workflow）

### 1. 加一个 secret（约 20 秒）

Settings → Secrets and variables → Actions → **New repository secret**

| Name | Secret |
|---|---|
| `ARTIFACT_KEY` | 自己定一串强密码（解包时要一模一样） |

可选：再加 `UNIXNU_UNIQUE_CHIP_ID`，值用 16 位十六进制（如 `0123456789abcdef`）。

### 2. 运行

Actions → **build droidvm release** → **Run workflow**

- `xcode` 留空用镜像默认；报 SDK 版本问题再填 `26.6` 之类试
- `only` 留空跑全量；超时或想断点续跑时填单步名

一轮大约 1–6 小时（要下 8 GB IPSW）。

### 3. 取产物并解密

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:<你设的ARTIFACT_KEY> \
  -in droidvm-release.tgz.enc -out droidvm-release.tgz
tar -xzf droidvm-release.tgz && ls release/
```

得到 `droidvm-disk.img`、`os.img`、`systemos.img`、`kernel.macho`、`release.json`。

---

## 已知风险

| 风险 | 说明 |
|---|---|
| Xcode 版本（payload） | UniXNU-droidvm README 写 macOS 27 / Xcode 27.1；`macos-26` 镜像只有 26.x。可能在编译阶段失败 |
| 超时（payload） | 给了 350 分钟上限；`build-droidvm-all.py` 步骤很多，可能不够 |
| 磁盘（payload） | 8 GB IPSW + 中间产物，runner 剩余空间是硬约束 |
| 磁盘（crosvm） | AOSP 树比 payload 更吃空间，所以有 `prepare` 档先量再编 |
| 分钟数 | **公开仓库用标准 runner 免费且不计量**；只有私有仓库才按 10 倍折算额度 |

---

## 拿到 payload 之后（2026-11-13 实测修正）

1. 把 `droidvm-disk.img`、`os.img`、`systemos.img` 放到手机的 `/data/media/0/DroidVM/<VM目录>/`
2. `data-store.img` 自己造：16 GiB 稀疏文件，头 4096 字节写 `DroidVMDataStore\001\000\000\000`
3. **固件不需要另找**：`edk2-virtio-gpu.fd` 就是 `usr/share/droidvm/edk2-gunyah.fd` 改个名
   （上游 `fresh-deploy.sh` 第 98 行就是这么 push 的）。实测它走 **virtio-pci** 也能起 UEFI，
   BdsDxe 从 `PciRoot(0x0)/Pci(0x1,0x0)` 起 `BOOTAA64.EFI`
4. 内存别按 5120 试：12 GB 机器上 `gh_hugepage_reserve` 最多只能收到约 1043 页（≈2086 MB），
   实测 `acquire_stop_reason=scanned all present memory`。payload 的 ADT 是 `--dynamic`，
   RAM 由 DarwinBoot 从 FDT 重算，给 2048 是自洽的
5. **真正的前置**是上面那个 mmio crosvm；只装 payload 会停在 `FDT devices: 0 disk(s)`

> 另一个前置（已在 K80 上验证）：`gunyah_host_share_gki_6.1` 模块必须加载，否则伪无保护模式以 `os error 2` 退出。

---

## 参考

- UniXNU: <https://github.com/UniXNU/UniXNU>
- 设备仓库: <https://github.com/Droid-VM/UniXNU-droidvm>
- crosvm fork: <https://github.com/Droid-VM/crosvm>（分支 `wip/virtio-mmio`）
- 构建流程来源: <https://github.com/Droid-VM/droidvm-meta-repo>（`1_`~`2-1_` 脚本）
- 同类先例: <https://github.com/Droid-VM/macOS-DroidVM>
