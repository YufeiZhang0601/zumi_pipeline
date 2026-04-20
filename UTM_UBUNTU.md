# UTM + Ubuntu 采集环境配置（M1 Mac）

本文档帮你在 **Apple Silicon (M1/M2/M3) Mac** 上，通过 UTM 虚拟机运行 Ubuntu ARM64，用来做 `zumi_pipeline` 的**实机数据采集**。

> 原则：**用 ARM64 原生镜像**，不要用 x86_64 模拟，性能差距巨大。

---

## 0. 前置硬件检查

采集前确认以下硬件已接到 Mac：

| 硬件 | 接法 | 在 Mac 上的表现 |
|------|------|----------------|
| GoPro 13 | USB-C 线连接，开启 USB 有线模式 | `ifconfig` 里多出一个 `enX` 接口，IP 形如 `172.2x.1xx.xx` |
| 电机驱动 (DM-CAN USB) | USB-A 转串口 | `ls /dev/tty.usbserial-*` 能看到 |
| UVC 相机 | USB-A 摄像头 | 系统偏好设置 → 隐私 → 相机 里能看到 |

> 如果 Mac 原生都认不到这些硬件，先解决硬件，再做 VM。

---

## 1. 安装 UTM

UTM 是 M 系列 Mac 上跑虚拟机最推荐的免费方案，基于 QEMU。

**方式 A（免费，推荐）：** 从官网下载
- https://mac.getutm.app/ → 点击 "Download"

**方式 B：** App Store 付费版（$9.99），内容一致，算是支持开发者

---

## 2. 下载 Ubuntu Server ARM64 镜像

**必须下载 ARM64（aarch64）版本**，不要下 amd64/x86_64。

推荐 Ubuntu 24.04 LTS Server：
- https://cdimage.ubuntu.com/releases/24.04/release/
- 下载文件名形如：`ubuntu-24.04.x-live-server-arm64.iso`

> Server 版够用，采集不需要桌面。想要桌面可以下 Desktop 版，但 VM 里开销更大。

---

## 3. 在 UTM 里创建虚拟机

1. 打开 UTM → 点 "Create a New Virtual Machine"
2. 选 **"Virtualize"**（绝对不是 Emulate，后者慢 10 倍）
3. 选 **"Linux"**
4. Boot ISO Image：选你下载的 `ubuntu-24.04.x-live-server-arm64.iso`
5. 资源分配建议：
   - **Memory**: 8 GB（最低 4 GB）
   - **CPU Cores**: 4
   - **Storage**: 60 GB（视频数据会很大，外挂硬盘也可以）
6. Shared Directory（可选）：先不设置，后面用其他方式传数据
7. 给虚拟机起个名字，比如 `zumi-ubuntu`
8. 点 "Save"

### 3.1 关键：编辑虚拟机设置（创建后）

点开虚拟机右上角的 **编辑（Edit）** 按钮，做以下调整：

**QEMU 选项卡：**
- 勾选 **"UEFI Boot"**

**Devices → USB：**
- **"USB Support"** 改成 `USB 3.0 (XHCI)`
- **"USB Shared Devices"** 数量从默认 3 调到 `5` 或更多（GoPro + 电机 + 相机至少 3 个）

**Network 选项卡：**
- Network Mode: `Shared Network`（默认即可，虚拟机能上网就行）

保存设置。

---

## 4. 安装 Ubuntu

1. 启动虚拟机
2. 按 Ubuntu 安装向导默认走：
   - Language: English
   - Keyboard: English (US)
   - Installation: Ubuntu Server (minimized 也行)
   - Network: 使用默认 DHCP
   - Storage: Use entire disk（在虚拟磁盘上）
   - Profile: 设置用户名和密码，比如 `ubuntu / ubuntu`
   - **SSH: 勾选 "Install OpenSSH Server"** ← 强烈建议，后面可以直接 SSH 进来
   - Snap 阶段：全部不选，快
3. 安装完成后重启，**记得在重启时把 ISO 弹出**（UTM 里点 CD/DVD 图标 → Eject）

重启后用账号密码登录，你会看到纯命令行的 Ubuntu。

---

## 5. 让你的 Mac 能 SSH 进 VM（可选但强烈推荐）

在 Ubuntu 虚拟机里运行：

```bash
ip addr show
```

找到 `enp0s*` 接口下的 `inet` 地址（通常是 `192.168.64.x`）。

回到 Mac 终端：

```bash
ssh ubuntu@<VM的IP>
```

之后所有操作都可以在 Mac 的终端里做，比 UTM 窗口舒服很多。

---

## 6. 安装项目依赖

仓库里已经准备好了自动化脚本 `install_ubuntu.sh`。

### 6.1 把仓库同步到 VM

方式一：直接在 VM 里 clone

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/aod321/rapid.git zumi_pipeline
# 或者你自己的 fork / 本地改动版
```

方式二：从 Mac rsync 过去（推荐，带着你在 Mac 上做的改动）

在 **Mac 终端**运行：

```bash
rsync -av --exclude='.venv' --exclude='data' --exclude='.git' \
  /Users/zyffbk/Documents/GitHub/zumi_pipeline/ \
  ubuntu@<VM的IP>:~/zumi_pipeline/
```

### 6.2 跑安装脚本

在 VM 里：

```bash
cd ~/zumi_pipeline
chmod +x install_ubuntu.sh
./install_ubuntu.sh
```

脚本会做这些事：
- 装系统包：`ffmpeg exiftool python3.10 python3-venv ...`
- 创建 `.venv` 虚拟环境
- 装所有 Python 依赖（`requirements-docker.txt` 里的）
- 给当前用户加 `dialout` 组（可以访问串口）
- 给当前用户加 `video` 组（可以访问 UVC 相机）

脚本跑完，**退出 SSH 重新登一次**（权限组才生效）。

---

## 7. USB 直通配置（关键）

这是 VM 采集能不能用的决定性步骤。

### 7.1 接上硬件

把 GoPro / 电机 / 相机都接到 Mac 上。

### 7.2 在 UTM 里把设备挂给 VM

虚拟机运行时，UTM 顶部菜单栏会出现一个 **USB 图标**（形如一个小 U 盘）。

点开它，你会看到 Mac 上所有 USB 设备列表。

**对每个要采集的设备打勾：**
- GoPro（通常叫 "GoPro HERO13 Black"）
- USB-Serial 转接器（常见名字：`QinHeng`, `FTDI`, `Prolific`, `CP210x`）
- UVC 摄像头（看相机品牌名字）

> ⚠️ 打勾的设备会**从 Mac 解绑、绑到 VM**。这时候 Mac 自己就看不到它们了。

### 7.3 在 VM 里确认设备

SSH 进 VM 里确认：

```bash
# GoPro (应该看到一个新的有线网卡)
ip addr show | grep -A2 "172\."

# 电机串口 (应该看到 /dev/ttyUSB0)
ls -la /dev/ttyUSB*

# UVC 相机
ls -la /dev/video*
```

三个都看到了，说明直通成功。

### 7.4 让直通永久生效

UTM 设置里可以把某些设备固定为 VM 专属：

VM 设置 → Devices → USB → 点 "+ New..." → 选中具体设备 → Save

这样下次开机会自动挂到 VM。

---

## 8. 配置环境变量

在 VM 里，写一份 `.env.capture` 文件，指定硬件路径。

```bash
cd ~/zumi_pipeline
cat > .env.capture <<'EOF'
# 电机串口（在 VM 里用 ls /dev/ttyUSB* 确认）
export ZUMI_SERIAL_PORT=/dev/ttyUSB0

# UVC 相机（VM 里是 /dev/video0，不是 Mac 的数字索引）
export ZUMI_UVC_DEVICE=/dev/video0

# 数据保存目录
export ZUMI_DATA_DIR=/home/ubuntu/zumi_data
EOF
```

每次采集前：

```bash
cd ~/zumi_pipeline
source .venv/bin/activate
source .env.capture
```

---

## 9. 跑一次采集

```bash
cd ~/zumi_pipeline
source .venv/bin/activate
source .env.capture
python orchestrator.py
```

按正常流程：
1. GoPro 自动发现 IP
2. 电机节点启动
3. UVC 节点启动
4. 按键开始 / 结束录制

---

## 10. 把数据传回 Mac 做后处理

VM 里采集完的数据在 `$ZUMI_DATA_DIR`，用 `rsync` 拉回 Mac：

```bash
# 在 Mac 上
rsync -av --progress \
  ubuntu@<VM的IP>:~/zumi_data/ \
  /Users/zyffbk/Documents/GitHub/zumi_pipeline/data/
```

然后在 Mac 用 Docker 做后处理（见 `DOCKER.md`）。

---

## 常见问题

### Q: UTM 里看不到我的 USB 设备？
1. 确认 VM 设置里 USB Support 是 `USB 3.0 (XHCI)`
2. 重启 VM 再试
3. 有些廉价 USB-hub 会在 VM 里行为异常，直接插 Mac 主板 USB 口

### Q: 电机串口打开失败（Permission denied）？
```bash
sudo usermod -aG dialout $USER
# 退出重登
```

### Q: UVC 相机打开失败？
```bash
sudo usermod -aG video $USER
# 退出重登
```
或者 UTM 里只能 USB 2.0 模式跑，相机也会有限制，先尝试改成 USB 3.0。

### Q: GoPro 的虚拟网卡在 VM 里拿不到 IP？
1. 在 VM 里 `sudo dhclient <接口名>` 手动触发
2. UTM 设置里检查 USB 直通是否真的生效（`lsusb` 能看到 GoPro）
3. 实在不行，GoPro 用 WiFi 模式而不是 USB 模式（更慢但更省事）

### Q: VM 太卡？
- 给 VM 多分点 CPU 和内存
- Ubuntu Server 比 Desktop 省资源
- Mac 上关掉其他吃内存的应用（Xcode、Chrome）

---

## 和 Mac 原生采集的对比

| 维度 | Mac 原生 | UTM + Ubuntu ARM64 |
|-----|---------|-------------------|
| 代码改动 | 需要（已做完） | 不需要 |
| 实时性 | 好 | 略有损失 |
| USB 稳定性 | 最好 | 看 UTM 运气 |
| 和论文一致 | 否 | 是 |
| 搭建成本 | 低 | 中 |

如果你跑 Mac 原生发现有诡异问题，再切换 VM 也不晚。
