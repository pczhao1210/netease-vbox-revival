# 网易三音云音箱操作指南：连接、备份、部署与调试

这是一份按操作顺序写的指南。先读完当前步骤和它的回退方法，再执行命令；**任何检查不符合预期，都停在当前步骤，不要连续粘贴整篇命令。**

> **先分清“刷机”和“换脑”：本项目的正常路线不刷原厂固件。**
> 保留原厂系统、唤醒引擎、播放器和 D-Bus，只在旁边部署自己的控制与语音服务。
> 仓库没有可直接刷入的原厂固件、经过验证的 NAND 恢复镜像或通用救砖流程。
> 当前第三代容器版本仍是离线候选版，**没有完成 R76s、真实音箱和云账号验收**。

## 0. 选择路线，准备工具

| 你的情况 | 操作顺序 |
|---|---|
| 第一次拿到音箱 | 第 1–3 节：USB 连接、识别设备、备份、配网 |
| 想让它重新工作 | 第 4–6 节：确认免刷机路线、部署大脑、按条件接入设备 |
| 已有旧版改造，迁移到 R76s | 先备份，再按第 5–6 节迁移；不能同时运行新旧控制端 |
| 没声音、不能唤醒、连接不稳 | 第 7 节：从网络到播放器逐层排查 |
| 升级前备份或退回旧版本 | 第 8–9 节 |
| 已经无法启动，准备真正刷固件 | 先看第 4 节；**不要把应用备份当成救砖镜像** |

### 准备物品

- 同款网易三音云音箱及其正常电源；不同批次先核对，不能只认外观。
- **Mini-USB 数据线**，不是 Micro-USB，也不是仅充电线。
- 一台维护电脑，安装 ADB。下面电脑命令以 **Linux + Bash** 为例。
- 一台运行“大脑”的 Linux 主机，例如 R76s、NAS 或旧电脑；可以和维护电脑是同一台。
- 2.4 GHz Wi-Fi、足够存放备份的磁盘、可信的局域网。
- 部署容器时需要 Docker Engine 和 Compose **2.24 或更新版本**。

Ubuntu/Debian 维护电脑尚未安装 ADB 时：

```bash
sudo apt update
sudo apt install adb
adb version
```

Windows/macOS 可使用 Android Platform Tools，但本文的 Bash、`md5sum`、`sha256sum` 等命令需要相应工具环境，不能直接整段粘贴进 PowerShell。

### 本文中的两种终端

- **维护电脑**：USB 接着音箱，执行所有 `adb ...` 命令。
- **大脑主机**：保存仓库并运行 Docker，执行所有 `docker ...`、`dc ...` 命令。
- `adb shell '...'` 引号内才是在音箱上执行的命令。**不要在音箱上安装 Docker 或运行主机安装脚本。**
- 示例 `192.168.1.100` 是大脑主机地址，`192.168.1.1` 是网关；都要换成自己的实际值。

动手前读 [危险操作清单](docs/07-危险操作清单.md)。全过程保持当前保守音量，不测试满音量，不盲写串口、DSP/EQ，不抢占声卡，不停止原厂服务。

## 1. 用 USB 连接并确认设备

### 1.1 连接

音箱接正常电源，用 Mini-USB 数据线连接维护电脑，等待它启动。

**维护电脑：**

```bash
adb devices -l
```

预期看到一行设备，状态为 `device`。记录它的序列号，在当前终端指定目标：

```bash
export ANDROID_SERIAL='替换为上一步的序列号'
adb get-state
adb shell id
```

预期分别是 `device` 和 `uid=0(root)`。仓库记录的这批固件通过 USB ADB 直接获得 root，不需要 `su` 或解锁。

**未达到预期：**

| 现象 | 先做什么 |
|---|---|
| 没有设备 | 换确认支持数据传输的线、换电脑 USB 口、检查供电；用 `lsusb` 看枚举 |
| `no permissions` | 检查 Linux ADB/udev 访问权限；不要把 USB 设备节点全局设成可写 |
| `offline` / `unauthorized` | 检查连接和本机授权提示；不要假设不同固件仍开放 root |
| 多台设备 | 用 `ANDROID_SERIAL` 明确选择，不能省略目标 |
| 不是 root | 停止后续写入，记录型号、固件和状态，重新确认适用性 |

**不要执行 `adb tcpip 5555`。** 本设备的历史测试中，这条路可能连 USB 通道一起弄断。日常控制应由设备主动连接大脑，不靠开放网络 ADB。

### 1.2 只读确认系统

```bash
adb shell 'uname -a; cat /etc/os-release'
adb shell 'cat /proc/mtd; cat /proc/partitions'
adb shell 'mount; df -h'
adb shell 'ps w'
```

对照 [设备介绍](docs/01-这台机器是什么.md)：仓库记录的是全志 R16、256 MB 裸 NAND、轻量嵌入式 Linux、只读系统加可写覆盖层，并有 `/mnt/UDISK`。

**通过条件：** 已确认是目标音箱，root 可用，能读出挂载信息和可写分区。若分区布局、路径或系统明显不同，先停下，不套用后面的路径。

## 2. 修改之前先备份

### 2.1 创建电脑上的独立备份目录

**维护电脑，仓库根目录：**

```bash
umask 077
BACKUP="$HOME/spk-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP/lib"
printf '备份目录：%s\n' "$BACKUP"
git rev-parse HEAD > "$BACKUP/repository-commit.txt"
adb shell 'date; uname -a; cat /etc/os-release' > "$BACKUP/system.txt"
adb shell 'cat /proc/mtd; cat /proc/partitions; mount; df -h' > "$BACKUP/storage.txt"
adb shell 'ps w; netstat -antp' > "$BACKUP/process-network.txt"
```

后文的 `$BACKUP` 指这个目录；换终端后需重新设为**同一个实际路径**，不要误建一个空备份。

### 2.2 拉取可写数据和配置

```bash
adb pull /mnt/UDISK "$BACKUP/UDISK"
adb pull /etc "$BACKUP/etc-view"
adb shell 'ls -lnR /etc /mnt/UDISK/spk /mnt/UDISK/wifi' > "$BACKUP/file-metadata.txt"
```

纯原厂设备可能还没有 `/mnt/UDISK/spk`，应记录为“改造前不存在”。其他读失败、空间不足、传输中断必须排查，**不能把部分成功当作备份完成**。

如果以前做过改造，先查看这些库是否存在：

```bash
adb shell 'ls -l /lib/mictap.so /lib/fespatap.so /lib/noyunxin.so'
```

只对实际存在的文件执行对应命令：

```bash
adb pull /lib/mictap.so "$BACKUP/lib/mictap.so"
adb pull /lib/fespatap.so "$BACKUP/lib/fespatap.so"
adb pull /lib/noyunxin.so "$BACKUP/lib/noyunxin.so"
```

重点确认备份里覆盖了：

- Wi-Fi 配置，包括主配置、overlay 配置及其他已有配置。
- 已有设备应用目录：脚本、本地配置、静音设置等。
- 原来的 init 脚本、启动链接、crontab 和注入库；同时保存权限、属主及链接关系记录。

`adb pull` 是文件复制，**不要假定它完整保留了属主、执行位、符号链接或扩展属性**。恢复单个文件时必须对照原始元数据；涉及系统目录整体恢复时，需要另行制作经过验证的、保留元数据的备份，不能直接整目录推回。

### 2.3 检查备份真的可读

```bash
du -sh "$BACKUP"
(
  cd "$BACKUP" || exit 1
  find UDISK etc-view lib -type f -exec sha256sum {} + > SHA256SUMS
  sha256sum -c SHA256SUMS
)
```

这一步确认电脑上已复制文件的后续完整性，**不证明设备端所有文件都已经复制**。还要核对 ADB 传输结果、关键文件清单，并对准备修改的文件比较两端校验值。例如已有旧代理时：

```bash
adb shell 'md5sum /mnt/UDISK/spk/spk_net.sh'
md5sum "$BACKUP/UDISK/spk/spk_net.sh"
```

两端摘要必须一致。MD5 在这里仅用于传输对照，不用于证明下载固件可信。

### 2.4 保存离机副本，理解备份边界

- 再保存一份到**加密**移动盘或其他受控存储；不要只放在音箱自身的 NAND 上。
- UDISK、配置和日志可能包含 Wi-Fi 密码、设备令牌、账号、录音或家庭信息。备份不是可公开分享的诊断包，**不要提交 Git、上传公开网盘或发到聊天**。
- 正在运行的应用可能持续写文件；文件复制不是一致性数据库快照。大脑主机的数据库按第 8 节处理。
- **这是应用/配置级备份，不是完整固件备份**：没有覆盖 NAND 的引导、坏块、OOB/ECC 等恢复需求。`/proc/mtd` 文本只是分区信息，不是分区内容。

**通过条件：** 关键文件已拉取、抽查校验一致、备份可读，且已有离机副本。没有满足这些条件，不进入写入步骤。

## 3. 配置 Wi-Fi 和局域网

### 3.1 已经联网就不要重配

```bash
adb shell 'iwconfig wlan0'
adb shell 'ip -4 addr show wlan0; ip route'
adb shell "sed -n '/^[[:space:]]*ctrl_interface=/p' /mnt/UDISK/wifi/wpa_supplicant.conf"
```

预期：无线已关联正确的 AP，`wlan0` 有 IPv4 地址，路由符合你的网络。

仓库实测控制 socket 在 `/mnt/UDISK/wifi/sockets`。**只有上一步与你的配置相符时**，才原样使用下面的 `-p` 路径：

```bash
adb shell 'wpa_cli -p /mnt/UDISK/wifi/sockets -i wlan0 status'
```

`wpa_state=COMPLETED` 只说明关联状态；还要看 `iwconfig` 的实际 AP 和 `ip addr`。**关联成功不等于拿到了 IP。**

### 3.2 需要换网时，先保留 USB 退路

以下只适用于已确认由该 `wpa_supplicant.conf` 管理的固件；如果服务参数指定了不同文件、overlay 或配置管理器，先理清覆盖关系。

1. 保持 USB 插着，确认第 2 节备份包含原 Wi-Fi 配置。
2. 在维护电脑制作工作副本，用本地编辑器编辑，保留原有全局设置和其他网络：

   ```bash
   cp "$BACKUP/UDISK/wifi/wpa_supplicant.conf" "$BACKUP/wpa.work.conf"
   chmod 600 "$BACKUP/wpa.work.conf"
   ```

   在工作副本中修改目标 `network` 块。WPA/WPA2-PSK 网络示意如下，**不是让你覆盖整份原配置**：

   ```text
   network={
       ssid="你的2.4GHz网络名"
       psk="你的Wi-Fi密码"
   }
   ```

   按实际认证方式填写，正确处理引号和转义；多网络时检查优先级、`disabled` 和旧 `bssid` 限制。不要把密码放进 shell 命令、历史或截图。
3. 推到同目录临时文件，设权限，对比摘要：

   ```bash
   adb push "$BACKUP/wpa.work.conf" /mnt/UDISK/wifi/wpa_supplicant.conf.new
   adb shell 'chmod 600 /mnt/UDISK/wifi/wpa_supplicant.conf.new; md5sum /mnt/UDISK/wifi/wpa_supplicant.conf.new'
   md5sum "$BACKUP/wpa.work.conf"
   ```

4. 确认摘要一致且配置正确，再原子替换、让已有服务重读配置：

   ```bash
   adb shell 'mv /mnt/UDISK/wifi/wpa_supplicant.conf.new /mnt/UDISK/wifi/wpa_supplicant.conf && sync'
   adb shell 'wpa_cli -p /mnt/UDISK/wifi/sockets -i wlan0 reconfigure'
   ```

   预期重读返回 `OK`；若返回 `FAIL` 或 socket 不通就停止。**不要通过杀死或重启原厂网络服务来试错。**
5. 等待关联和 DHCP，再执行第 3.1 节的检查。如果已关联却没有 IP，先检查有无现有 DHCP 客户端。仅在确认没有客户端负责取地址时，手动尝试一次：

   ```bash
   adb shell 'udhcpc -i wlan0 -q -n -t 5'
   ```

6. 不成功就通过 USB 将备份原配置重新推到 `.new`，核对摘要、权限后 `mv` 回去，并用同一个 `wpa_cli ... reconfigure` 重读；随后复查原网络。不要删除整个 Wi-Fi 目录。

旧 [spk_prov.sh](device/spk_prov.sh) 会切换热点并操作网络服务；[switch_ap.sh](device/switch_ap.sh) 和 [pin_ap.sh](device/pin_ap.sh) 也有特定环境前提。**它们不是第三代容器版本的一键配网入口，不要仅凭文件名运行。**

### 3.3 核对大脑地址

建议给大脑主机和音箱设置路由器 DHCP 地址保留，避免重启后地址漂移。不要把大脑写成 `127.0.0.1`、`0.0.0.0` 或 Compose 内部服务名。

第 5 节启动服务并开放受控 LAN 访问后，从音箱检查：

```bash
adb shell 'curl -f --connect-timeout 3 --max-time 5 http://192.168.1.100:8898/health'
adb shell 'curl -f --connect-timeout 3 --max-time 5 http://192.168.1.100:8899/health'
```

此时应成功返回健康响应。服务尚未启动或仍只绑定主机 loopback 时失败是预期的。`ping` 通不能替代 TCP/HTTP 检查；访客 Wi-Fi/AP 隔离也可能阻止设备访问主机。

## 4. “刷机”到底怎么做

### 4.1 本指南采用的方式：免刷固件部署

正常步骤是：

1. 保留原厂 bootloader、分区、系统和本地服务。
2. 在另一台主机运行新大脑。
3. 根据已验证的设备状态，部署或迁移应用脚本。
4. 通过既有播放器出声；语音使用已适配的旁路采音链。
5. 验收后保留备份，失败时退回旧应用配置。

**往 UDISK 放脚本不等于刷固件，但仍然是设备写入，有风险。** 第 6 节只给出已有设备应用的迁移流程，不假装从任意原厂状态都能一键装好。

### 4.2 如果你确实需要重刷系统

按以下顺序完成前置确认，缺一项就停：

1. 确认主板批次、SoC、NAND 型号、实际分区、当前固件版本和设备能否正常启动。
2. 获取**有权使用、来源可信且对应这块硬件**的完整固件，以及相应官方或已验证的烧录说明。
3. 确认镜像格式、签名/摘要、双 rootfs 选择规则、引导副本和设备专属数据如何保留。
4. 明确读取及恢复工具如何处理裸 NAND 的坏块、OOB/ECC，并验证备份可用于该恢复流程。不能把普通 `dd` 文件当成已验证的救砖备份。
5. 确认该批硬件进入恢复模式的方式、驱动、退出方式和失败后的恢复方案；保持稳定供电并有人在场。
6. **只有上述条件全部满足，才按对应固件的说明执行写入。本文不提供猜测性的擦除、分区或烧录命令。**

仓库只记录了全志 FEL 恢复通道及 USB 厂商 ID `1f3a`，**没有提供可通用照抄的进模式按键/短接位置、刷写地址或恢复镜像**。发现该 VID 不能证明已经具备刷机条件，也不能保证一定能救回。

不要尝试伪造 OTA 服务，不要随便执行 [spk_ota.sh](device/spk_ota.sh)、FEL 的 write/exec、裸 NAND 擦写或 bootloader 修改。更多背景见 [拿到 root](docs/02-拿到-root.md) 和 [路线更正表](docs/08-路线更正表.md)。

## 5. 部署大脑主机：第三代容器版

### 5.1 检查主机

**大脑主机，仓库根目录：**

```bash
uname -m
free -h
df -h
date
docker version
docker compose version
```

R76s 若承担家庭主路由，先确认内存、磁盘、端口和网络规则不会影响现有服务。不要为了构建镜像随意执行特权容器或修改路由器的防火墙。

### 5.2 创建本地配置，固定 Compose 调用方式

仅首次创建，已有文件则直接编辑，不能覆盖现有设置：

```bash
test -e config/.env.local || cp config/compose.env.example config/.env.local
chmod 600 config/.env.local
dc() { docker compose --env-file config/.env.local "$@"; }
```

`config/.env.local` 已被忽略规则覆盖。这里配置镜像、绑定地址、端口和路径，**不要写云服务 Key**。以后每个新终端都要在仓库根重新定义 `dc`；不要混用其他环境文件。

首次保留 `SPK_LAN_BIND=127.0.0.1` 和 `SPK_PUBLIC_URL=http://127.0.0.1:8080`。检查 `SPK_MUSIC_PATH`、`SPK_MODELS_PATH` 指向你准备使用的目录，并确保容器 UID 1000 有读取权限。

当前版使用 Compose 配置与页面设置；[config/spk.env.example](config/spk.env.example) 和 [src/install.sh](src/install.sh) 属于旧运行路线，**不要混进这次容器部署**。

### 5.3 构建并启动基础服务

```bash
dc --profile voice --profile search config --quiet
./start.sh build
dc up -d --no-build
dc ps
curl -f http://127.0.0.1:8080/health
curl -f http://127.0.0.1:8899/health
dc logs --tail=100 control media music-resolver
```

`start.sh build` 只构建镜像，不启动服务，也不操作音箱。上述 `up` 默认启动 control、media 和 music-resolver，尚未启用 voice/search。

如果已有获授权访问的 Docker Hub 私有镜像，先在本地配置设置正确的 `SPK_IMAGE`，再以 `docker login`、`dc pull`、`dc up -d --no-build` 代替本机构建。不要公开发布含个人配置的镜像。

**通过条件：** 容器正常运行，健康检查成功，无持续重启或卷权限错误。失败先查日志，不要加 `privileged`、挂宿主声卡或改成 root 来掩盖问题。

### 5.4 初始化页面和 LAN 访问

1. 在大脑主机访问 `http://127.0.0.1:8080`。远程维护电脑可建立 SSH 隧道：

   ```bash
   ssh -N -L 8080:127.0.0.1:8080 登录名@大脑主机地址
   ```

   然后在维护电脑打开同一个 loopback URL。若本地 8080 被占用，先处理端口与 origin 配置，不能只改一边。
2. 按页面完成管理员初始化。页面要求初始化令牌时，仅在可信终端获取：

   ```bash
   dc exec -T control python -m spk_platform.cli export-bootstrap-token
   ```

   令牌不要发到聊天、截图或公共日志。
3. 准备受信任的 HTTPS 反向代理后，将 `SPK_PUBLIC_URL` 设为实际使用的 HTTPS origin；正式输入云 Key 必须走 HTTPS。代理需要允许 SSE，上传和探测也要配置适当的大小/超时限制。
4. 将 `SPK_LAN_BIND` 改为大脑主机的实际 LAN IP，再执行 `dc up -d --no-build`。之后原来的 loopback 健康检查地址可能不再可用，改用实际绑定地址。
5. 只向可信管理端和设备放行必要端口，不在公网做端口转发：

   | 端口 | 用途 |
   |---|---|
   | TCP 8080，或自设 UI 端口 | 管理页面，正式使用由 HTTPS 代理提供入口 |
   | TCP 8898 | 设备轮询/回报 |
   | TCP 8899 | 带票据的媒体下载 |
   | UDP 9998 | 旁路麦克风数据，仅启用 voice 后需要 |
   | TCP/UDP 9997 | 唤醒入口；已有 waketap 实际使用 TCP |

   设备侧 HTTP 令牌及音频 UDP 不应穿过不可信网络；需要隔离 LAN 或另行设计可信隧道。不要公开旧 8896 接口。
6. 在设备设置页填写真实音箱 IP、严格 CIDR 和音箱能访问的媒体地址，例如 `http://192.168.1.100:8899`。不能填 localhost。Docker bridge 下服务看到的来源 IP 需实机核对，不要为了连通放开成所有地址。

设置页先保存草稿，再**显式应用**；仅保存不等于生效。

### 5.5 先做预检，再逐项启用能力

```bash
dc run --rm --no-deps control preflight --strict
dc --profile voice run --rm --no-deps voice preflight --voice --strict
```

预检不访问音箱、不发起云请求。成功也不代表 ARM64 真机、账号、网络和实际音频链已经验收。

- 先用**自己制作或有权使用的本地短音频**验证音乐路径，暂不接云服务。
- LLM 配置实际 API base、模型名和协议；Azure OpenAI 的 model 填部署名。分别测试模型目录、文本、工具和流式能力。
- 当前平台可配置 Azure Speech。Speech Key 与 OpenAI Key 分开；样本识别、TTS、真实对话等计费测试需明确确认费用。
- KWS 模型不随仓库提供；按 [部署文档](docs/11-R76s-部署与验收.md) 准备 `kws/model.json` 和匹配模型。缺少 KWS 时只能在**已有原生唤醒转发链可用**的前提下使用该入口，不等于设备采音会自动安装。
- 搜索/MCP 默认关闭。确有需要时，发现工具、逐项审阅授权并应用配置后再启用。
- 网易云登录和取流需要自己的合法账号；CLI 的已知依赖风险及上线门槛见 [部署文档](docs/11-R76s-部署与验收.md)，不要把容器隔离当作风险已消除。

只启用语音：

```bash
dc --profile voice up -d --no-build
```

语音和搜索都已准备好时：

```bash
dc --profile voice --profile search up -d --no-build
```

## 6. 接入音箱：已有改造的 v2 迁移

### 6.1 先检查适用条件

这一步会改设备应用文件，安排维护窗口并有人在场。先停止旧大脑的命令/语音写入者，保留它的镜像、配置和启动方式；不能让两台大脑同时控制音箱。

```bash
adb shell 'ls -l /mnt/UDISK/spk/spk.conf /mnt/UDISK/spk/spk_conf.sh /mnt/UDISK/spk/spk_net.sh /mnt/UDISK/spk/play601.sh'
adb shell 'command -v curl; command -v dbus-send; command -v start-stop-daemon; command -v md5sum'
adb shell 'ls -l /tmp/dbus-*'
```

**必须已经有适配并可用的 `play601.sh`、设备配置和启动链。** 原厂新机缺少这些文件时，不要直接运行批量部署脚本补齐：

- 第三代文档给的是已有设备迁移，不是经过真机验收的从零安装器。
- 首次安装还需要按本机固件验证 D-Bus、播放、采音库 ABI、原厂 init 注入位置及自启动时序。
- 这些前提未完成时，停在“连接/备份/主机页面可用”，不能声称音箱已经能完整对话。

尤其不要把 [deploy_scripts.sh](device/deploy_scripts.sh)、[deploy_mictap.sh](device/deploy_mictap.sh) 当作当前版本的一键安装器。前者覆盖范围包含 init/crontab 等历史部署项，后者涉及原厂语音服务和注入库，都不属于本节的三脚本迁移。

### 6.2 准备新配置和配套文件

**维护电脑，仓库根目录：**

```bash
umask 077
WORK=$(mktemp -d)
cp "$BACKUP/UDISK/spk/spk.conf" "$WORK/spk.conf"
```

用编辑器修改工作副本，保留无关配置，确保以下值正确且没有重复、冲突的旧定义：

```sh
SPK_SRV=192.168.1.100
SPK_GW=192.168.1.1
SPK_PORT_NETD=8898
SPK_PORT_MP3=8899
SPK_NET_PROTOCOL=v2
```

若原文件定义了 `SPK_NET_SRV`，删除该旧覆盖项或把它同步改成正确的 `大脑IP:8898`；否则只改 `SPK_SRV` 可能仍连旧地址。`spk.conf` 会作为 shell 文件载入，只编辑可信内容。

把三份配套脚本先上传为临时文件，并自动比较摘要：

```bash
(
  for f in spk_conf.sh spk_agent_v2.sh spk_net.sh; do
    sh -n "device/$f" || exit 1
    adb push "device/$f" "/mnt/UDISK/spk/$f.new" || exit 1
    adb shell "chmod 755 /mnt/UDISK/spk/$f.new && sh -n /mnt/UDISK/spk/$f.new" || exit 1
    local_md5=$(md5sum "device/$f" | awk '{print $1}')
    remote_md5=$(adb shell "md5sum /mnt/UDISK/spk/$f.new" | tr -d '\r' | awk '{print $1}')
    [ "$local_md5" = "$remote_md5" ] || { printf '校验失败：%s\n' "$f"; exit 1; }
    printf '校验通过：%s\n' "$f"
  done
)
```

必须看到三份均通过。配置同样先暂存：

```bash
sh -n "$WORK/spk.conf"
adb push "$WORK/spk.conf" /mnt/UDISK/spk/spk.conf.new
adb shell 'chmod 600 /mnt/UDISK/spk/spk.conf.new && sh -n /mnt/UDISK/spk/spk.conf.new && md5sum /mnt/UDISK/spk/spk.conf.new'
md5sum "$WORK/spk.conf"
```

### 6.3 安全传递设备令牌

若维护电脑就是大脑主机，在已定义 `dc` 的同一终端执行：

```bash
dc exec -T control python -m spk_platform.cli export-device-token > "$WORK/net.token"
```

两台主机分开时，使用可信 SSH 将输出保存到**维护电脑**，替换登录名、主机名和远端仓库路径：

```bash
ssh 登录名@大脑主机地址 'cd /远端仓库路径 && docker compose --env-file config/.env.local exec -T control python -m spk_platform.cli export-device-token' > "$WORK/net.token"
```

只执行符合环境的一种。确认命令成功、文件非空后推送；不要 `cat` 令牌或把它粘进命令参数：

```bash
(
  test -s "$WORK/net.token" || { printf '令牌文件为空，停止\n'; exit 1; }
  chmod 600 "$WORK/net.token" || exit 1
  adb push "$WORK/net.token" /mnt/UDISK/spk/net.token.new || exit 1
  adb shell 'chmod 600 /mnt/UDISK/spk/net.token.new' || exit 1
  local_md5=$(md5sum "$WORK/net.token" | awk '{print $1}')
  remote_md5=$(adb shell 'md5sum /mnt/UDISK/spk/net.token.new' | tr -d '\r' | awk '{print $1}')
  [ "$local_md5" = "$remote_md5" ] || { printf '令牌传输校验失败，停止\n'; exit 1; }
  printf '令牌传输校验通过，未显示内容\n'
)
```

令牌缺失、长度异常或传输失败就停止。新令牌工作副本属于秘密材料，不放进普通导出包。

### 6.4 保留设备端回退副本，停止旧应用代理

在设备上创建一次性的回退目录，**已存在就停止，不覆盖上一次备份**：

```bash
adb shell '
  umask 077
  cd /mnt/UDISK/spk || exit 1
  mkdir -p backup && mkdir backup/pre-v2 || exit 1
  for f in spk_conf.sh spk_net.sh spk_agent_v2.sh spk.conf net.token; do
    if [ -f "$f" ]; then
      cp -p "$f" "backup/pre-v2/$f" || exit 1
    else
      printf "%s\n" "$f" >> backup/pre-v2/absent-files.txt
    fi
  done
'
```

目录包含旧令牌，应按敏感备份管理；这只是便于回退的小副本，不能替代第 2 节离机备份。

查找应用代理，按完整脚本路径核实，不用模糊进程名：

```bash
adb shell '
  for p in /proc/[0-9]*; do
    last=$(tr "\000" "\n" < "$p/cmdline" 2>/dev/null | tail -n 1)
    case "$last" in
      /mnt/UDISK/spk/spk_net.sh|/mnt/UDISK/spk/spk_agent_v2.sh)
        printf "%s %s\n" "${p##*/}" "$last"
        ;;
    esac
  done
'
```

对输出的具体 PID，再读取 `/proc/该PID/cmdline` 核实身份，随后只向这个 PID 发 `TERM`，并重新执行上面的检查确认已退出。**不要复制示例 PID，不使用 `killall`/`pkill`，不停止 `netease_voice`、播放器、D-Bus 或网络服务。**

如果旧代理不断被保活机制拉起，或无法确认控制进程身份，先停止迁移并解决应用维护方式；**不要为了迁移关闭整套 crond、run.sh 或原厂服务**。

### 6.5 原子发布并启动

仅在旧写入者已停、没有旧代理、所有暂存文件都验证通过时执行。最后才切换设备配置：

```bash
adb shell '
  cd /mnt/UDISK/spk || exit 1
  mv spk_agent_v2.sh.new spk_agent_v2.sh &&
  mv spk_net.sh.new spk_net.sh &&
  mv spk_conf.sh.new spk_conf.sh &&
  mv net.token.new net.token &&
  mv spk.conf.new spk.conf &&
  sync
'
```

每个 `mv` 是同目录单文件替换，**不是整组文件的事务**；中途失败应保持控制端停用，按第 9 节回退，不启动半套新文件。

再次确认没有旧代理，再启动：

```bash
adb shell 'start-stop-daemon -S -b -x /mnt/UDISK/spk/spk_net.sh'
adb shell 'readlink /tmp/spk/v2/agent.pid'
```

用第 6.4 节的方法确认恰好一个 v2 代理，锁中的 PID 确实对应它；单看锁存在不算成功。大脑主机检查：

```bash
dc logs --since=5m control media
```

完成后删除维护电脑的两份工作副本，保留正式备份：

```bash
rm -- "$WORK/net.token" "$WORK/spk.conf"
rmdir "$WORK"
```

### 6.6 注意：迁移控制地址不等于迁移采音地址

- [waketap.sh](device/waketap.sh) 启动时读取 `SPK_SRV`，向 TCP 9997 发唤醒事件；已经运行的实例不会仅因文件修改就自动切地址。
- [mictap.c](device/mictap.c) 从**原厂语音进程的启动环境**读取 `MICTAP_HOST` / `MICTAP_PORT`，不是读取 `spk.conf`；目标地址在初始化时确定。
- 本节不改原厂 init、不换注入库、不重启原厂语音服务。因此已有采音仍指向旧主机时，**音乐控制可单独验收，语音迁移尚未完成**。
- 音频库必须按设备 ABI 编译并在早期开机可见的 `/lib` 加载。把它放在晚挂载的 UDISK、只改 `LD_PRELOAD` 文本，都不能证明已经注入。

需要变更采音启动环境时，另设维护步骤：先备份该机实际的语音启动脚本和库、明确地址与重启方案，再验收加载和收包。不把历史脚本直接当作通用方案。详见 [音频链路真相](docs/05-音频链路真相.md)。

## 7. 调试：按链路顺序定位

### 7.1 先记录症状和时间

记录一次操作的时间、页面任务状态、是否有灯效、是否实际出声、失败发生在首字/尾字/整段。尽量一次只改一个因素，先本地播放，后语音，再云端搜索。

**大脑主机：**

```bash
dc --profile voice --profile search ps
dc logs --since=10m --tail=200 control media
dc --profile voice logs --since=10m --tail=200 voice
```

仅检查已启动的服务。分享诊断前去掉 Key、令牌、带票据 URL、Wi-Fi/账号信息、转写和家庭内容。

### 7.2 网络和控制代理

1. 按第 3 节确认关联、IP、路由及 8898/8899 健康接口。
2. 按第 6.4 节确认代理数量，再核对锁 PID 和配置中的目标地址。
3. 检查服务端设备 IP/CIDR、媒体 URL、设备令牌文件权限。
4. `401`/`403` 优先查令牌、来源限制和配置应用状态；连接拒绝/超时优先查绑定地址、端口、路由及防火墙，**不要先重装设备**。

不要为了诊断去手工消费 `/device/cmd`，它是取指令接口，不是健康检查。v2 的诊断也不能只看旧 `net.log` 有没有更新。

### 7.3 “页面显示成功，但听不见”

按以下证据逐项查，上一项成功不代表下一项成功：

```text
页面任务创建
  -> 设备接收命令
  -> 音箱请求媒体文件
  -> 原厂播放器状态变化
  -> 人在现场实际听见
```

**维护电脑，只读：**

```bash
adb shell 'ls -lt /tmp/ihwplayer_*.log /tmp/netease_control_center_*.log'
adb shell "amixer -c 0 cget name='HP_L Mux'"
adb shell 'amixer -c 0 cget numid=19'
adb shell "amixer -c 0 cget name='Headphone Switch'"
adb shell "amixer -c 0 cget name='Phoneout Switch'"
```

从第一条输出选最新日志，再用 `adb shell 'tail -n 80 /tmp/实际日志文件名'` 看相应时段。

- 没有媒体请求：查设备收到的 URL、票据、网络和代理动作。
- 有媒体请求但播放器未运行：查格式、当前 playerId、D-Bus 和原厂占用。
- 播放状态正常却无声：对照改动前的 Mux、DAC、输出开关与夜间静音状态。**只读定位，不盲目 `cset`，不把 DAC 拉满。**
- D-Bus 的 `NoReply` 可能只是原厂播放器不回方法结果；脚本退出码/HTTP 200 不能作为“已经出声”的证明。
- 保留音频尾部保护静音，不能为了缩短时长删掉；原厂在尾部的切歌行为可能截断内容。

不要用 `arecord`、`aplay` 抢声卡，也不要遍历未知 D-Bus 命令或串口命令。默认通过页面测试播放/停止，不绕过 v2 调度器并发发控制指令。

### 7.4 有唤醒灯效，但大脑听不到

查看原厂语音进程是否真的加载了旁路库，仅输出相关地址配置：

```bash
adb shell '
  for p in /proc/[0-9]*; do
    [ "$(cat "$p/comm" 2>/dev/null)" = netease_voice ] || continue
    printf "语音进程：%s\n" "${p##*/}"
    grep -E "mictap|fespatap|noyunxin" "$p/maps"
    tr "\000" "\n" < "$p/environ" | grep -E "^MICTAP_(HOST|PORT)="
  done
'
```

**判断：**

- `maps` 没有 `mictap`：没有证据证明库加载；不能因为磁盘上有 `.so` 或环境里写了 `LD_PRELOAD` 就判成功。
- 没有 `MICTAP_HOST`：需要核对该库版本的默认目标；当前源码默认 `192.168.1.100`，不是“自动发现大脑”。
- `/tmp/mictap.log` 不存在不一定故障，当前实现的调试日志默认关闭。
- 检查 `/tmp/spk/mictap.off` 是否存在，避免把主动暂停发送当成网络问题。

必要时在大脑主机做短时**只看包头、不落盘**的抓包，需已安装 tcpdump 且有权限：

```bash
sudo timeout 10 tcpdump -n -i any -c 20 'udp dst port 9998 or tcp dst port 9997'
```

有包只证明抵达抓包位置，不证明 voice 容器已接收或识别；结合 voice 日志确认格式、来源和处理状态。不要使用 `-A`、`-X` 或保存未经授权的家庭音频。

原厂语音服务不能随便 restart：历史固件会在 D-Bus 前提不满足时硬重启。至少需要核对 `/tmp/dbus_env.sh` 可执行、`dbus-daemon --fork` 恰好一个、会话总线可用且只读调用成功，并有针对该机的恢复方案；这不是日常排查第一步。

### 7.5 紧急停止自己的麦克风转发

对本仓库这一版 mictap，可在设备上创建发送暂停标记：

```bash
adb shell 'mkdir -p /tmp/spk && touch /tmp/spk/mictap.off'
```

它不需要重启原厂语音进程，只暂停这个钩子的转发，**不是物理麦克风静音，也不代表其他采音路径被关闭**。恢复时仅删除此标记：

```bash
adb shell 'rm /tmp/spk/mictap.off'
```

标记在 `/tmp`，重启会丢失；不能把它当作持久隐私开关。

### 7.6 重启与持久性验收

先完成一次当前保守音量下的播放/停止，确认有人在场，备份和恢复通道都可用，再安排一次正常重启：

```bash
adb shell sync
adb reboot
adb wait-for-device
```

`wait-for-device` 只说明 ADB 回来了，不代表 Wi-Fi、UDISK、D-Bus 或应用就绪。若长时间无响应可中断等待，检查供电和 USB，不进入反复断电循环。

重新核对第 1、3、6、7 节：文件/执行权限仍在、配置仍正确、网络恢复、代理恰好一份、既有采音库加载、页面能控制且实际出声。

设备如果原本没有自启动链，本指南不会自动创建；“手动启动成功”不能当作重启后可用。迁移应保留已验证的启动链，不用覆盖整套 init/crontab 来凑结果。

## 8. 大脑主机的日常备份

### 8.1 普通设置导出

在管理页面导出设置，保存当前应用 revision 和镜像信息。普通导出包含结构化设置和闹钟，**不包含云 Key、管理员密码、会话、设备令牌和音乐账号**；导入只生成草稿，需要再次应用。

### 8.2 数据库备份

**大脑主机，当前部署目录，使用同一 `dc` 定义：**

```bash
dc exec -T control python -m spk_platform.cli backup
```

它使用 SQLite backup API，会打印类似 `/state/backup-随机标识.sqlite3` 的实际路径。将**该实际文件**复制出容器，不能只留在卷里：

```bash
umask 077
SERVER_BACKUP="$HOME/spk-server-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SERVER_BACKUP"
dc cp control:/state/替换为实际备份文件名.sqlite3 "$SERVER_BACKUP/"
docker image inspect --format '{{.Id}}' neteast-spk-server:latest > "$SERVER_BACKUP/image-id.txt"
```

最后一条的镜像名需与你的 `SPK_IMAGE` 一致。镜像 ID 记录不等于保存了可回滚镜像；升级前还要保留旧镜像及配置，避免仅剩会被覆盖的 `latest`。

不要直接复制正在写入的主数据库文件。数据库只有密钥字段加密，不是全库加密，快照仍按敏感材料保管。

### 8.3 完整恢复还需要密钥与账号

计划做完整快照时，先停用设备控制、停止所有容器写入者，再制作同一维护窗口的备份：

```bash
dc --profile voice --profile search stop
dc run --rm --no-deps control backup
```

第二条在相同数据卷上用当前镜像生成一致的数据库备份，不启动服务。按第 8.2 节复制这一次打印的文件。

另选一个受控的加密存储位置，与数据库备份**分开保管**，例如挂载好的加密盘：

```bash
KEY_BACKUP='/替换为密钥加密存储目录/本次快照'
mkdir -p "$KEY_BACKUP/keys" "$KEY_BACKUP/music-account"
chmod 700 "$KEY_BACKUP" "$KEY_BACKUP/keys" "$KEY_BACKUP/music-account"
dc cp control:/keys/. "$KEY_BACKUP/keys/"
dc cp music-resolver:/accounts/. "$KEY_BACKUP/music-account/"
```

还需按恢复目标保存媒体缓存卷、自己的本地音乐/模型绑定目录，以及私有部署配置。**仅命名目录为“加密”不会使它加密**；实际存储必须有加密保护。核对复制结果、权限和校验值，避免在聊天中展示内容。

没有匹配的主密钥不能恢复云 Key；只保存主密钥也不够，还需要一致的数据库和相关账号/鉴权材料。完整恢复演练应在隔离的新卷中进行，保持音箱控制禁用，验证 schema、权限和配置后才切换，不能让新旧实例同时控制设备。

备份结束，按原来启用的 profile 启动，不要无意开启此前关闭的功能。例如原本同时启用了语音和搜索：

```bash
dc --profile voice --profile search up -d --no-build
```

## 9. 回滚与常见故障

### 9.1 从 v2 迁移退回旧应用

1. 停止新大脑写入者，旧大脑仍保持停止；按第 6.4 节核实并停止设备应用代理。
2. 核对设备 `backup/pre-v2` 及第 2 节离机备份，确认这确实是**本次迁移前**的文件和令牌。
3. 将备份中的 `spk_conf.sh`、`spk_net.sh`、`spk.conf`、`net.token` 以及原先存在的 `spk_agent_v2.sh` 分别复制为目标同目录临时文件；保留原权限，检查 shell 语法和摘要，再逐个 `mv` 回原路径。
4. 原先不存在的文件以 `absent-files.txt` 为准，确认无人引用后只清理本次新增的具体文件；**不要删除整个应用目录或覆盖整个 `/etc`**。
5. 回到旧主机地址和旧协议。已有明确旧配置时恢复原文件；旧版本不带协议字段时，当前加载器默认是 `legacy`，不要误留 `SPK_NET_PROTOCOL=v2`。
6. 按原来已验证的方式启动一份旧设备代理，再恢复旧大脑。检查网络、播放、静音和重启后状态。
7. 如果另行改过采音目标或启动环境，还必须用那次改动自己的备份回退；仅恢复应用配置不会自动恢复 `MICTAP_HOST`。

对单个已确认存在的备份文件，可按这个模式操作；下面以 `spk.conf` 为例，其他文件逐个核实，不能直接套在整个目录：

```bash
adb shell '
  cd /mnt/UDISK/spk || exit 1
  cp -p backup/pre-v2/spk.conf spk.conf.restore &&
  sh -n spk.conf.restore &&
  md5sum backup/pre-v2/spk.conf spk.conf.restore
'
```

两行摘要一致后才替换：

```bash
adb shell 'mv /mnt/UDISK/spk/spk.conf.restore /mnt/UDISK/spk/spk.conf && sync'
```

### 9.2 常见问题速查

| 问题 | 处理顺序 |
|---|---|
| 断 Wi-Fi，但 USB 还在 | 通过 USB 恢复对应 Wi-Fi 配置、重读并检查关联/DHCP，不杀网络服务 |
| 推送后报权限错误 | 检查执行位，脚本按已验证权限恢复；配置和令牌保持 0600 |
| 修改后重启失效 | 查挂载时序、实际活动文件、自启动入口和内容摘要 |
| 音乐可用，语音不可用 | 分别查 `SPK_SRV`、运行中的 waketap、`MICTAP_HOST`、9997/9998 和 voice 配置 |
| 夜里不响 | 先确认静音策略，这是可能的正常行为；不要直接打开输出开关 |
| 忘记管理员密码 | 用下面的受控重置命令，不删数据库或卷 |
| 不再启动或只枚举恢复设备 | 停止写入，记录 USB/启动现象，按第 4 节确认型号专用恢复条件 |

管理员密码重置在大脑主机的可信交互终端执行，**不要加 `-T`，不要把密码作为参数**：

```bash
dc exec control python -m spk_platform.cli reset-admin-password
dc restart control
```

密码需 12–256 个字符，两次隐藏输入确认。该操作保留配置和账号，但会使旧会话失效；重启可能打断当前任务。

**不要执行 `docker compose down -v` 作为“重装/回滚/找回密码”手段，它会删除持久卷。** 也不要为了恢复应用去刷 NAND。

## 10. 最终验收清单

- [ ] USB 能稳定连接 root，设备型号、分区与本指南一致。
- [ ] 修改前的文件和元数据有可读、已校验的离机备份。
- [ ] Wi-Fi、IP、路由和大脑端口可达，重启后仍成立。
- [ ] 只有一台大脑在控制设备，设备应用代理恰好一份。
- [ ] 容器健康，设置已应用，敏感配置不经明文远程页面传输。
- [ ] 本地音频实际可听，播放/停止正常，未改动当前保守音量。
- [ ] 如启用语音：采音真实收包、唤醒链可用、识别/合成通过授权测试，首尾内容完整。
- [ ] 原厂服务仍正常，没有新增抢声卡、写 DSP/EQ、串口探测或固件刷写。
- [ ] 重启后文件、权限、自启动、静音和控制仍正常；不是只验了 ADB 回来。
- [ ] 新旧配置、镜像和令牌恢复关系明确，完成观察期前不删除回退材料。

更详细的版本边界、验收缺口和配置说明见 [R76s 部署与验收](docs/11-R76s-部署与验收.md)；硬件接口查 [协议笔记](docs/PROTOCOL.md)，不要把逆向笔记里的探测操作当成日常操作步骤。
