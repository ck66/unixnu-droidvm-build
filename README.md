# unixnu-droidvm-build

在 GitHub 的 macOS runner 上构建 **UniXNU-droidvm** 的完整发布集（iPadOS 17.7.11 跑在 crosvm + Gunyah 里）。

产物就是我们缺的那套：`droidvm-disk.img`、`os.img`、`systemos.img`、`kernel.macho`、`release.json`。

---

## 为什么自己写这个 workflow

官方 `UniXNU/.github/workflows/macos.yml` 明确**只上传脱敏日志**：

> Kernels, payloads, disk images, kernelcaches and IPSW members are never uploaded: they derive from Apple software.

`Droid-VM/macOS-DroidVM` 的 `mainline.yml` 会传加密二进制，但它只跑 `unixnu build kernel`，不产出 droidvm 的完整发布集。

所以这个 workflow 直接跑 `UniXNU-droidvm/scripts/build-droidvm-all.py`，然后把 `out/droidvm/release/` 打包加密上传。

---

## 用法

### 1. 加一个 secret（约 20 秒）

Settings → Secrets and variables → Actions → **New repository secret**

| Name | Secret |
|---|---|
| `ARTIFACT_KEY` | 自己定一串强密码（解包时要一模一样） |

可选：再加 `UNIXNU_UNIQUE_CHIP_ID`，值用 16 位十六进制（如 `0123456789abcdef`）。
不加也行 —— Run 的时候填在 `unique_chip_id` 输入框里。

### 2. 运行

Actions → 左侧 **build droidvm release** → **Run workflow**

- `xcode` 留空用镜像默认；报 SDK 版本问题再填 `26.6` 之类试
- `only` 留空跑全量；超时或想断点续跑时填单步名

一轮大约 1–6 小时（要下 8 GB IPSW）。

### 3. 取产物并解密

跑完在 Run 页面底部下载 `droidvm-release-enc`，然后本地：

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:<你设的ARTIFACT_KEY> \
  -in droidvm-release.tgz.enc -out droidvm-release.tgz

tar -xzf droidvm-release.tgz
ls release/
```

得到 `droidvm-disk.img`、`os.img`、`systemos.img`、`kernel.macho`、`release.json`。

---

## 已知风险

| 风险 | 说明 |
|---|---|
| Xcode 版本 | UniXNU-droidvm README 写 macOS 27 / Xcode 27.1；`macos-26` 镜像只有 26.x。可能在编译阶段失败 |
| 超时 | 给了 350 分钟上限；`build-droidvm-all.py` 步骤很多，可能不够 |
| 磁盘 | 8 GB IPSW + 中间产物，runner 剩余空间是硬约束 |
| 分钟数 | **公开仓库用标准 runner 免费且不计量**；只有私有仓库才按 10 倍折算额度 |

---

## 拿到产物之后

1. 把 `droidvm-disk.img`、`os.img`、`systemos.img` 放到手机的 `/data/media/0/DroidVM/<VM目录>/`
2. `data-store.img` 用 `tools/rig/make-data-store.sh` 生成（或按 `release.json` 里的说明）
3. 在 DroidVM 里新建一台 VM：`backend=crosvm`、`hypervisor=gunyah`、`protected_vm=pseudo_unprotected`、`cpu_count=4`、`memory_mb` 先给 **2048**（5120 在 SM8650 上会整机复位）
4. 固件用 `edk2-virtio-gpu.fd`（随 release 提供）；拿不到时先用 `edk2-gunyah.fd` 验证 UEFI 能到 BdsDxe

> 前置条件（已在 K80 上验证）：`gunyah_host_share_gki_6.1` 模块必须加载，否则伪无保护模式会以 `os error 2` 退出。

---

## 参考

- UniXNU: <https://github.com/UniXNU/UniXNU>
- 设备仓库: <https://github.com/Droid-VM/UniXNU-droidvm>
- 同类先例: <https://github.com/Droid-VM/macOS-DroidVM>
