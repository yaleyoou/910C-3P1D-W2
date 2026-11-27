# 四台 Ascend 910C 的 3P1D 双池配额实验

> 本地交付已校验。原 Docker 镜像压缩包与解压 tar 均通过 SHA-256 校验，985 个 W2 请求体与 7 个实机补丁库均匹配历史记录。

先跑 F1。P1、P2、P3 各启动一个 TP16 prefill 实例，D 启动一个 TP16 decode 实例。每个 P 贡献 64 GiB Mooncake Store，D Store 配 0 GiB，D sparse 配 192 GiB。这样两池总预算仍是 **384 GiB**。三台 P 的 HBM、本地前缀缓存和算力都参与实验，和旧 1P1D 的比较包含这项拓扑变化。四台新机器尚未实跑，旧 F1 的 106.39 tok/s 和 80.93% 是历史参考值。

## 四台新 910C 如何指定 P、D 和权重目录

先在四台分别运行 `hostname; ip -br -4 addr`。从集群已配置的地址中，选出每台能与另外三台互通的业务 IPv4。角色由你在 `cluster.env` 中指定。脚本会绑定这些地址，宿主机的 IP 由集群管理员配置。每台独立运行一个 TP16 实例，需要能看到该机的 16 个 die。

| 登录哪台机器 | 在 cluster.env 填什么 | 本机角色和端口 |
|---|---|---|
| 第 1 台（控制机） | `P1=第1台业务IP` | `p1` 运行 prefill 8100、代理 8300、Mooncake Master 50088/50089 |
| 第 2 台 | `P2=第2台业务IP` | `p2` 运行 prefill 8100 |
| 第 3 台 | `P3=第3台业务IP` | `p3` 运行 prefill 8100 |
| 第 4 台 | `D=第4台业务IP` | `d` 运行 decode 8200，按配额启动 D Store |

获取仓库后，在 P1 编辑仓库根目录的 `cluster.env`，保留文件里的 `IMAGE` 和 `P_STORE`。下列四个地址仅供示例，请全部换成实机地址。

```bash
P1=192.0.2.11
P2=192.0.2.12
P3=192.0.2.13
D=192.0.2.14
IFACE=auto
MODEL_DIR=/home/caofei/GLM-5.2-w4a8
```

`IFACE=auto` 在各台按本机角色的 IP 查找网卡。把 `MODEL_DIR` 改成**宿主机上直接包含 config.json、tokenizer 和权重分片的目录**。本次四台的目录都是 `/home/caofei/GLM-5.2-w4a8`，共用这一行配置即可。已有完整权重时可跳过下载和同步步骤。若第 4 台实际下载到另一块盘，只需在同一份配置中补一行 `MODEL_DIR_D=/mnt/models/GLM-5.2-w4a8c8`。其他角色同理。容器内均挂载为 `/models/glm-q`，API 模型名均为 `glm-5`。

四台使用相同的部署目录和同一份配置。若共享挂载，只编辑、解包一次。若为独立磁盘，先把恢复好的部署目录同步到各台的相同绝对路径，再同步权重。管理网与业务网分开时，在配置中按实际登录方式设置 `SSH_P2`、`SSH_P3`、`SSH_D`。下载、驱动及镜像准备见下文第 1–3 节。

## Windows 手动下载 Release 全部文件后上传与服务器端还原

如果已在 Windows 浏览器手动下载了 [w2-v1 Release](https://github.com/yaleyoou/910C-3P1D-W2/releases/tag/w2-v1) 的全部附件并放到本地 `images` 目录（共 **839** 个文件：`image.seg-0000`～`image.seg-0835` 共 836 个分片、`memfabric-complete.tar.gz`、`w2-data.tar.gz`，以及可忽略的 `910C-Windows-Downloader.zip`），可按下面两种方式二选一上传到服务器（以下示例假设 SSH 别名为 `910a3_27`，服务器仓库目录为 `/你的仓库路径/910C-3P1D-W2`，按实际情况替换）。

### 方案 A（推荐）：先在 Windows 合并校验，再只 `scp` 3 个文件

避免 `scp` 逐个传输 836 个小分片的开销，上传前即可发现缺失或损坏的分片，也能为服务器节省约 6.5 GiB 临时空间（Windows 当前盘需至少 7 GiB 空闲空间）。

#### 使用 Git Bash（最简洁，推荐）

在 `images` 文件夹右键打开 **Git Bash** 执行：

```bash
# 1. 检查 838 个必需文件是否齐全（也能发现浏览器重复下载带 "(1)" 后缀的情况）
missing=0
for f in $(printf 'image.seg-%04d\n' {0..835}) memfabric-complete.tar.gz w2-data.tar.gz; do
  if [[ ! -f "$f" ]]; then echo "MISSING: $f"; missing=$((missing + 1)); fi
done
echo "Missing count: $missing"

# 2. Missing count 为 0 后，按 0000 -> 0835 顺序合并为 image.tar.zst（约 6.53 GiB）
printf 'image.seg-%04d\n' {0..835} | xargs cat > image.tar.zst

# 3. 二进制模式校验三个核心文件的 SHA-256
sha256sum -c <<'EOF'
0e7cfc4416208ccb7b7c90cac31d0d0bc5d8e2dd224058c8b6d9c03d1f7351e4 *image.tar.zst
afcf64db37d5ec1e1c2edecc2ba9fa101b9ec73c673a30ecc25530bf07a1ed85 *memfabric-complete.tar.gz
9d4293a9ca6d3a1570e25f42c69191503b0553589c6d866f1ea241b5d797e54b *w2-data.tar.gz
EOF

# 4. 三项全部 OK 后，只传这 3 个文件到服务器仓库根目录
scp image.tar.zst memfabric-complete.tar.gz w2-data.tar.gz 910a3_27:/你的仓库路径/910C-3P1D-W2/
```

<details>
<summary>如果改用 Windows PowerShell（展开查看）</summary>

```powershell
cd C:\你的路径\images
$ErrorActionPreference = 'Stop'
$dir = (Get-Location).ProviderPath

$missing = @(0..835 | ForEach-Object { 'image.seg-{0:D4}' -f $_ } | Where-Object { -not (Test-Path $_) })
foreach ($f in @('memfabric-complete.tar.gz', 'w2-data.tar.gz')) {
    if (-not (Test-Path $f)) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Host "缺少 $($missing.Count) 个文件（前 20 个）：" -ForegroundColor Red
    $missing | Select-Object -First 20
    throw "请先补齐文件或去掉浏览器重复下载后缀再合并"
}

$out = [IO.File]::Create((Join-Path $dir 'image.tar.zst'))
try {
    foreach ($i in 0..835) {
        $f = [IO.File]::OpenRead((Join-Path $dir ('image.seg-{0:D4}' -f $i)))
        try { $f.CopyTo($out) } finally { $f.Dispose() }
    }
} finally {
    $out.Dispose()
}

$expected = @{
    'image.tar.zst'             = '0e7cfc4416208ccb7b7c90cac31d0d0bc5d8e2dd224058c8b6d9c03d1f7351e4'
    'memfabric-complete.tar.gz' = 'afcf64db37d5ec1e1c2edecc2ba9fa101b9ec73c673a30ecc25530bf07a1ed85'
    'w2-data.tar.gz'            = '9d4293a9ca6d3a1570e25f42c69191503b0553589c6d866f1ea241b5d797e54b'
}
foreach ($name in $expected.Keys) {
    $h = (Get-FileHash $name -Algorithm SHA256).Hash.ToLower()
    if ($h -eq $expected[$name]) { Write-Host "OK   $name" -ForegroundColor Green }
    else { Write-Host "FAIL $name (got $h)" -ForegroundColor Red }
}

scp.exe .\image.tar.zst .\memfabric-complete.tar.gz .\w2-data.tar.gz 910a3_27:/你的仓库路径/910C-3P1D-W2/
```

</details>

上传完成后直接跳到下方「服务器端解包、校验与导入镜像」。

### 方案 B：直接 `scp` 整个 `images` 目录到服务器后再合并

适合 Windows 本地盘空间不足时使用。服务器目标盘需至少 15 GiB 空闲空间（836 个分片与合并后的 `image.tar.zst` 会短暂并存）。

1. 在 Windows Git Bash 上传到与仓库同盘的临时目录：

```bash
scp -r /c/你的路径/images 910a3_27:/你的仓库路径/w2-images
```

2. 登录 `910a3_27`，离线校验 838 个文件并拼接镜像：

```bash
ssh 910a3_27

REPO=/你的仓库路径/910C-3P1D-W2
cd "$REPO"
df -h .

# 移入 838 个必需文件（忽略 910C-Windows-Downloader.zip）
mv ../w2-images/image.seg-* ../w2-images/memfabric-complete.tar.gz ../w2-images/w2-data.tar.gz .

# 纯离线校验全部 838 个文件（不联网）
sha256sum -c release.sha256 | grep -v ': OK$' || echo "ALL 838 FILES OK"

# 按清单顺序拼接为 image.tar.zst 并校验
awk '$2 ~ /^image[.]seg-/ {print $2}' release.sha256 | xargs cat > image.tar.zst
sha256sum -c assets.sha256

# 校验通过后删除 836 个分片，释放约 6.5 GiB 空间
rm -f image.seg-*
```

### 服务器端解包、校验与导入镜像

方案 A 或方案 B 完成后，在 `910a3_27` 仓库根目录执行（需宿主机已安装 `zstd`）：

```bash
cd /你的仓库路径/910C-3P1D-W2

# 1. 解压 W2 测试集与 MemFabric 4T 补丁运行库
mkdir -p data
tar -xzf w2-data.tar.gz -C data
tar -xzf memfabric-complete.tar.gz

# 2. 全量离线校验（7 个 MemFabric 库、镜像压缩包与解压 tar 的 SHA-256、985 条 W2 请求）
bash check_assets.sh

# 3. 导入 Docker 镜像（正常输出 sha256:b3c4ed45ab4433bda7a306fdaabdd33481d32df9fd41b54dc0c6066602c34fc6 arm64）
sudo bash node.sh load
```

镜像导入完成后，按本页第 2 节编辑 `cluster.env` 并同步部署目录到其余三台 910C（若四台共享目录则跳过同步），在其余三台**各自执行一次** `sudo bash node.sh load`，随后按「首次启动和单请求验收」完成 `sudo bash node.sh check p1|p2|p3|d` 与试跑。

## Windows 一键下载镜像、MemFabric 和 W2

用浏览器下载 [Windows 下载器 ZIP](https://github.com/yaleyoou/910C-3P1D-W2/releases/download/w2-v1/910C-Windows-Downloader.zip)，右键选择“全部解压”，放在至少有 14 GiB 可用空间的目录。在解压目录双击 `Download-W2.cmd`，同一目录只运行一个窗口。脚本使用 curl.exe、certutil.exe 和 Windows PowerShell。中断后在原目录双击即可继续。看到 READY 后，把 `910C-files` 里的三个文件传到服务器的仓库根目录。

| 文件 | 用途 |
|---|---|
| image.tar.zst | Docker 镜像 |
| memfabric-complete.tar.gz | 原实验 MemFabric 运行库和补丁 |
| w2-data.tar.gz | 985 条冻结请求及依赖清单 |

## Windows 只下载 Docker 镜像

在 Windows 浏览器中打开 [download-image.ps1](download-image.ps1)，在文件右上角点 **Download raw file**，存到空间至少 14 GiB 的目录。打开该目录的 PowerShell，执行：

```powershell
.\download-image.ps1
```

脚本从 w2-v1 下载 836 个镜像分片，校验 SHA-256 后在脚本旁生成 `image.tar.zst`。中断后重跑会复用完整分片。出现 `Verified:` 后，只传合并好的镜像文件。在 PowerShell 中把下面的账号和服务器地址替换成实际 SSH 登录目标：

```powershell
scp.exe .\image.tar.zst user@SERVER:~/
```

在 910C 的 Linux 终端导入。若四台共用存放镜像的目录，只需传一次，但每台都要执行加载。

```bash
set -o pipefail
zstd -dc ~/image.tar.zst | sudo docker load
```

## 从 GitHub 恢复完整部署包

在 P1 或能访问 GitHub 的 Linux 登录节点执行。公开仓库和 Release 附件均可直接用 HTTPS 下载，无需 gh、GitHub 账号或上传权限。以下示例假定当前账户可写 /data。

```bash
cd /data
git clone --depth 1 https://github.com/yaleyoou/910C-3P1D-W2.git
cd 910C-3P1D-W2
bash fetch_assets.sh
```

**已经克隆过**：在仓库目录运行 `git pull --ff-only && bash fetch_assets.sh`。脚本使用 curl 下载并校验 836 个镜像分片、W2 回放包和 MemFabric 运行库，随后拼接镜像并解包。中断后重跑同一条脚本命令即可，已通过 SHA-256 校验的文件会被复用。已有完整权重时直接使用 cluster.env 中的模型路径。

如果克隆成功但 Release 下载失败，可在同一终端用下面的命令检查附件地址及其重定向。若只有登录节点能下载，在共享盘的仓库目录完成下载，再到每台 910C 执行第 3 节的镜像加载。

```bash
curl -IL --connect-timeout 10 https://github.com/yaleyoou/910C-3P1D-W2/releases/download/w2-v1/memfabric-complete.tar.gz
```

### git pull 成功，但下载第一个分片时报 curl (60)

出现 `self signed certificate in certificate chain` 时，curl 无法用当前信任的 CA 验证所见证书链。先更新下载脚本并重试。新版会尝试使用 Git 为 GitHub 配置的 CA 文件，也支持手动指定集群管理员提供的可信 PEM 证书包。

```bash
git pull --ff-only https://github.com/yaleyoou/910C-3P1D-W2.git main && bash fetch_assets.sh
```

如果仍报 (60)，向集群管理员取得用于 GitHub HTTPS 出口的 CA bundle 文件或其本机路径，将下例的路径替换为实际路径后重跑。该设置只对这次运行生效，无需 sudo。

```bash
CURL_CA_BUNDLE=/实际路径/cluster-ca-bundle.pem bash fetch_assets.sh
```

也可以在已配好 CA 的登录节点上向共享盘下载。下载前用 `df -h .` 查看当前目录的可用空间。首次还原时镜像分片和合并文件同时存在，仅这两份就占约 13.1 GiB，此外还需为 W2 解包、运行库及后续 Docker 导入留空间。

### git clone 成功，但 fetch_assets.sh 连接超时

在 node07 或下载节点的已有仓库执行。探测请求只申请第一个分片的第一个字节，并打印 HTTP 状态行。脚本尝试使用 Git 为 GitHub 配置的可信 CA 和 HTTPS 代理，已有的代理环境变量优先。

```bash
git pull --ff-only https://github.com/yaleyoou/910C-3P1D-W2.git main
bash fetch_assets.sh --probe
```

最后出现 `206 Partial Content` 时运行 `bash fetch_assets.sh`。若出现 302 后超时，先向管理员确认是否允许 GitHub Release 附件重定向的下载出口。若节点仅允许 Git 服务，请用集群批准的文件导入途径，将完整离线包和对应 .sha256 文件放到共享数据盘。在有足够空间的新目录解压，保留已配置的 cluster.env；在四台各自进入还原的目录加载镜像。

```bash
sha256sum -c 910C-3P1D-W2-offline.tar.sha256
mkdir -p offline-deploy
tar -xf 910C-3P1D-W2-offline.tar -C offline-deploy
cd offline-deploy/910C-3P1D-W2
bash check_assets.sh
sudo bash node.sh load
```

## 1. 准备四台主机

每台需 Linux ARM64、可用的 910C 驱动和固件、8 张卡的 16 个可见 die，以及可用的 Docker Engine。容器提供 vLLM、vLLM-Ascend、Mooncake、CANN 用户态环境。精确软件栈是 vLLM 0.30.0+empty、vLLM-Ascend 0.30.1.dev22+g4ebfc1f4c、Mooncake 0.3.13，以及随包挂载的 MemFabric 1.3.1 4T 补丁。四台需要能通过原传输方式访问彼此的 NPU fabric；仅管理 IP 能 ping 通还不足以验证 SDMA/HCCS。D Store 大于 0 时，Store donor 和模型共用设备，核对设备共享设置。

在每台机器执行：

```bash
uname -m
npu-smi info
npu-smi info -t device-share -i 0 -c 0
ls /dev/davinci{0..15} /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc
ip -br -4 addr
sudo docker info
df -h /var/lib/docker
free -h
```

四台建议沿用原实验 2 TiB 主机内存级别；新节点按实际 HBM、主机余量核对模型启动和池规划。预留 Docker 展开镜像、模型缓存和运行日志的磁盘空间。新系统若缺宿主软件，由集群管理员按 Linux 发行版安装 Docker Engine、zstd、rsync、curl、Python 3 和匹配固件的 Ascend 驱动。核对各节点时间同步。原实验启动脚本挂载的驱动、固件和设备路径见 `node.sh`。检查 P1 可连三台 P 的 8100、D 的 8200、四台可连 P1 的 8300、50088/50089，并允许集群内部的 HCCL、MemFabric、Mooncake 数据面端口。8311–8313 是校准期间 P1 上的三个临时 proxy。

历史上 D 同时启动 TP16 模型和 TP16 Store donor 时开启了设备共享。若新 D 的查询显示共享关闭，先确认 8 张卡都归本实验且无作业，再由管理员按节点驱动的帮助核对并应用。例如原 910C 的命令如下；F1 的 D Store 为 0，可先跑 F1 的握手校准。

```bash
for card in {0..7}; do
  for chip in 0 1; do
    sudo npu-smi set -t device-share -i "$card" -c "$chip" -d 1
    npu-smi info -t device-share -i "$card" -c "$chip"
  done
done
```

## 2. 分发文件和模型

在 Linux 部署目录执行 `sha256sum -c assets.sha256`，两项都应显示 OK。随后把恢复好的部署目录连同修改后的 cluster.env 复制到其余三台机器的**同一个绝对路径**，例如 `/data/910C-3P1D-W2`。四台若共享这个目录，只复制一次。控制机 P1 需要能用普通 SSH 登录 P2、P3、D。自动流程需要 P1 免交互 SSH 到其余三台，并在各机通过 `sudo -n` 执行 node.sh、docker 等命令，P1 还用到 sudo tee。不同登录账号可以在 P1 设置 `SSH_P2=user@host`、`SSH_P3=user@host`、`SSH_D=user@host`。若这些 sudo 权限不可用，使用后面的手动流程。

若 P1 已在 /data 恢复完整部署包且四台目录不共享，在 P1 执行下例。三个目标登录名换成实际 SSH 别名或 user@host，目标账号需可写 /data。若已配置共享目录，跳过复制。

```bash
for host in NEW_P2 NEW_P3 NEW_D; do
  rsync -a --exclude='.git/' --exclude='runs/' --exclude='image.seg-*' /data/910C-3P1D-W2/ "$host:/data/910C-3P1D-W2/" || break
done
```

需要重新下载时，在**能访问 ModelScope 且能写目标磁盘**的机器上安装 ModelScope CLI 并下载。第二个可选参数是模型仓库 revision，省略时下载上游默认版本。复现实验优先使用原共享盘的权重。

```bash
python3 -m pip install modelscope
bash tools/download_model.sh /home/caofei/GLM-5.2-w4a8
```

只通 GitHub 的节点可从本仓库取得脚本，但下载权重需要访问 [ModelScope 模型仓库](https://www.modelscope.cn/models/Eco-Tech/GLM-5.2-w4a8c8)。这些节点直接挂载已有权重，或使用下面的 rsync 步骤。

原权重保存在 node25 共享盘的 `/home/youxingye/youxingye_data/models/GLM-5.2-w4a8c8`。新节点可见同一个挂载时直接设置 MODEL_DIR。否则在能读到权重且能 SSH 到新节点的机器上执行，目标目录由当前账号持有：

```bash
bash tools/sync_model.sh /home/youxingye/youxingye_data/models/GLM-5.2-w4a8c8 /home/caofei/GLM-5.2-w4a8 user@NEW_P1 user@NEW_P2 user@NEW_P3 user@NEW_D
```

在四台设置一致的 `cluster.env`。将 P1、P2、P3、D 改成四个不同的**业务网 IPv4 地址**，IFACE 默认按本机业务地址自动选择网卡，MODEL_DIR 改为实际绝对路径。三台 P 的 Store 配额默认各 64 GiB。模型在容器内固定挂载到 `/models/glm-q`，方便沿用原来的 Store key 命名空间。

## 3. 加载离线镜像和 W2

原镜像 ID 为 `sha256:b3c4ed45ab4433bda7a306fdaabdd33481d32df9fd41b54dc0c6066602c34fc6`。本地文件名 `image.tar.zst`，源自 node25 上的旧镜像归档。通过 `fetch_assets.sh` 恢复的目录已完成解包和校验。四台各自有独立的 Docker daemon，所以即使目录共享，也要在每台加载一次镜像。

```bash
cd /data/910C-3P1D-W2
sudo bash node.sh load
```

校验脚本检查原始 23 GiB Docker tar 的 SHA-256，并按冻结清单解压、逐个校验实际 HTTP body。W2 固定 985 个请求、20 个根会话实例、1006 条依赖边、81,245,507 输入 tokens 和 984,784 输出 tokens。回放并发为 16 棵会话树，保留依赖、原始请求体和目标输出长度。整批跑完后计算吞吐。

MemFabric 通过 `/deploy/upgrades/memfabric/enable_4t.sh` 加载；服务启动时也在容器可写层应用旧实验的一行 logger 修改。主 KV 为 BF16，Index K 为 INT8，top-k 热缓冲 4096。P seq16、batched tokens 4096、eager；D seq32、batched tokens 2048、watermark 0.2。Mooncake 使用 ascend 后端。四节点分别启动独立引擎，共用一个 Master，D 的活跃 KV 由 SfaRemoteD2HConnector 拉取。

## 首次启动和单请求验收

以下命令在**镜像加载完毕后**执行。四台终端均进入部署目录，例如 `cd /data/910C-3P1D-W2`。先在每台运行对应的检查命令，再在 P1 发起启动，随后在其他三台执行各自的启动命令。

| 所在机器 | 启动前检查 | 启动模型和本机服务 |
|---|---|---|
| P1 | `sudo bash node.sh check p1` | `sudo bash node.sh up p1 F1 perf smoke1` |
| P2 | `sudo bash node.sh check p2` | `sudo bash node.sh up p2 F1 perf smoke1` |
| P3 | `sudo bash node.sh check p3` | `sudo bash node.sh up p3 F1 perf smoke1` |
| D | `sudo bash node.sh check d` | `sudo bash node.sh up d F1 perf smoke1` |

`up` 返回后引擎仍在后台加载。等三台 P 和一台 D 的健康请求都成功，再从 P1 发真实请求。第一次模型加载和 D 图编译可能较久，可在对应机器查看 `runs/F1/smoke1/perf/<角色>/logs/p.log` 或 `d.log`。

```bash
source ./cluster.env
for ip in "$P1" "$P2" "$P3"; do curl --noproxy '*' -fsS -o /dev/null -w "$ip P HTTP %{http_code}\n" "http://$ip:8100/health"; done
curl --noproxy '*' -fsS -o /dev/null -w 'D HTTP %{http_code}\n' "http://$D:8200/health"
curl --noproxy '*' -fsS "http://$P1:8300/healthcheck"
curl --noproxy '*' -fsS --max-time 300 "http://$P1:8300/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-5","messages":[{"role":"user","content":"请用一个词描述天空的颜色。"}],"max_tokens":64,"temperature":0,"stream":false}' \
  | python3 -m json.tool
```

健康检查通过后再单独运行上面的 POST 命令。四个引擎应各返回 HTTP 200，代理应报告 `prefill_instances=3` 和 `decode_instances=1`。最后一条请求应返回非空的 `choices` 和正数 `usage.completion_tokens`。代理健康接口只报告已配置的后端数。单请求验证一次 P→D 推理，正式校准阶段再逐一验证 P1、P2、P3 到 D 的三条路径。

验收完在**四台各执行一次** `sudo bash node.sh down`。要开始正式实验，在 **P1 普通用户终端**运行 `bash case.sh F1 r1`。它自行启动四台并依次完成校准、性能和重复量阶段。性能和重复量阶段各完整回放同一批 985 条请求。结果在 P1 的 `runs/F1/r1/blocks/p1/result.csv`。正式测量使用新容器和新试验目录，以免计入冒烟请求。再次运行冒烟或正式试验时换用新目录名，例如 `smoke2` 或 `r2`。

## 4. 先跑 F1

在 P1 上确认新节点由本实验使用，并检查已配置的 P2/P3/D 登录方式。四台皆需持有同一镜像和代码。

```bash
cd /data/910C-3P1D-W2
bash case.sh F1 r1
```

每组按顺序完成三轮独立冷启动：

1. **cal 校准**。三条已知请求经分别绑定 P1、P2、P3 的临时 proxy 交给同一个 D，逐条核对 D scheduler 记录的 128-token 块哈希与 Mooncake Master 里的主 KV 命名空间。要求三条路径都能成功返回；保存校准记录后停止本组容器。
2. **perf 性能**。走一个正式 proxy，由原负载分配逻辑选择 P，三台 P 共同服务同一份 W2。正式发送前检查 Store 为空且每个节点注册配额吻合。采集吞吐、延迟、排队、P 命中和各节点 Store 分配量。
3. **blocks 重复量**。重新冷启动，再跑完同一份 W2；在 D 采集块分配、ready、finish，在 P1 每 5 秒抓取 Store 全局 key。P1/D 时钟做独立对时。按请求和块哈希关联后计算活跃主 KV 与重复比例。两个大回放分开，沿用原实验性能表的口径。

每个阶段只有一个 Master、一个 D。每台 P 各绑定自己的 8100，P1 的正式 proxy 是 8300。因 P 侧路由和命中分布会变，检查最终 `per-P.json` 中三台各自的前缀查询量、命中数、队列和 Store 占用。一个根会话不保证固定落在同一个 P。

查看进度（以 F1 perf 为例）：

```bash
tail -f runs/F1/r1/perf/p1/finite-client/events.jsonl
tail -n 20 runs/F1/r1/perf/p1/logs/proxy.log
curl --noproxy '*' -fsS "http://$(. ./cluster.env; echo "$P1"):50089/metrics" | grep -E '^master_(allocated|total_capacity)_bytes '
```

每轮结果目录由组名、trial、阶段、角色组成。失败时保留容器与日志，查看相应节点的 `logs/p.log`、`logs/d.log`、`logs/store-d.log` 或 proxy 日志。检查 D 是否实际规划到了目标 sparse 容量、四台设备健康、不同 P 的传输会话能否正确连到 D。修正后各有容器的节点运行 `sudo bash node.sh down`，重试用新 trial，如 r2。停止命令只操作标记为本实验的容器。

## 5. 配额表和扩容对照

`cases.tsv` 是唯一的组别配额表；总预算 = 3 × 64 + D Store + D sparse，单位 GiB。

| 组 | D Store | D sparse | 总预算 | 对照目的 |
|---|---:|---:|---:|---|
| F1 | 0 | 192 | 384 | 历史基准 |
| F5 | 16 | 176 | 384 | 靠近 F1 的中间点 |
| F2 | 32 | 160 | 384 | 固定总预算 |
| F3 | 64 | 128 | 384 | 固定总预算 |
| F4 | 128 | 64 | 384 | 固定总预算 |
| EC1 | 64 | 192 | 448 | 从 F1 单加 Store |
| ES1 | 0 | 256 | 448 | 从 F1 单加 sparse |
| EJ1 | 64 | 256 | 512 | 从 F1 两侧都加 |

```bash
for g in F5 F2 F3 F4 EC1 ES1 EJ1; do bash case.sh "$g" r1 || break; done
```

## 6. 取结果

每组的三 P 分项保存在 `blocks/p1/per-P.json`。每组输出 `runs/<组>/<trial>/blocks/p1/result.csv`。P1 宿主机将多组汇总：

```bash
python3 tools/result.py runs/{F1,F5,F2,F3,F4,EC1,ES1,EJ1}/r1 > summary.csv
```

| 字段 | 统计口径 |
|---|---|
| 输出吞吐；TTFT 均值 / P95、平均 TPOT | 首发至最后完成的输出 tokens/s；按请求计算 TTFT 和首 token 后的 TPOT；沿用原 finite client 的分位数算法 |
| P 平均排队数；D 平均容量等待数 | 全部正式时间内三台 P 的 waiting 之和的时间均值；D 只计 reason=capacity |
| P 合计前缀命中率 | 三台的本地命中和外部命中 token 增量求和，除以三台本地查询 token 增量之和 |
| P / D Store 平均占用 | 逐 segment 按注册 IP 归属，积分对象分配字节；P 列为三台合计，D Store=0 时为零 |
| D 平均活跃主 KV | D 上从 ready 到 finish 的活跃物理 slot 只计一次，以 78 层 × (512+64) × 2 字节/token 折算。步间生成 token 数按原算法插值 |
| 平均重复量 | D 上每个活跃物理块的内容哈希在同一时刻的全局 Store 主 KV key 集合中出现时，累积对应主 KV 字节并做时间平均 |
| 活跃主 KV 重复率 | 重复主 KV 的字节·秒 ÷ 全部活跃主 KV 的字节·秒。5 秒快照与输出插值构成估计误差；输出中保留上下界与隔点采样结果 |

数据完整性以 `request-audit.json`、`overlap.json`、两份 `finite-client/summary.json` 和 per-P 计数为准。旧报告作为比较放在 `evidence/旧1P1D结果.md`。全部四节点指标需由新机器产生。

## 手动分步启动

四台可分别登录并有普通 sudo 权限时，可在四台分别运行 `sudo bash node.sh up p1 F1 cal r1`、`... up p2 ...`、`... up p3 ...`、`... up d ...`。先在 P1 发起，其他三台随即启动。各节点 `sudo docker exec glm52-3p1d ...` 使用同一个本机容器名。P1 执行 `sudo docker exec glm52-3p1d bash /deploy/bench.sh`。cal/blocks 完成后把 D 的 `/run-data/logs/d-blocks.jsonl` 拷到同阶段 P1 的 `logs/` 下，cal 在 P1 容器执行 `python3 /deploy/tools/check_probe.py /run-data`。每个阶段结束在四台分别执行 `sudo bash node.sh down`。

依次将阶段改为 perf、blocks；blocks 回放前后仍需要 P1 能通过 SSH 访问 D 进行对时，在 P1 主机用 `python3 tools/clock_check.py user@D /dev/stdout` 保存为同阶段 P1 输出目录的 `clock-before.json` 和 `clock-after.json`。blocks 完成、D 日志已汇集后，在 P1 容器执行 `bash /deploy/finish.sh` 再停服务。详情以 `case.sh` 中的运行顺序为准。

宿主环境参考 [vLLM-Ascend A3 安装文档](https://docs.vllm.ai/projects/ascend/en/v0.30.0/getting_started/installation.html)。模型需保留原配置、tokenizer 和原权重文件以匹配冻结清单。


## 原节点保存位置与恢复

node25 原归档位于 `/home/youxingye/pd3-export-tJNBHq/`。需要转移的三个文件为 `image.tar.zst`、`w2-data.tar.gz` 和 `memfabric-complete.tar.gz`。其中最后一个由管理员身份读取受限的运行库；首次同名类似的 `memfabric-runtime.tar.gz` 仅含部分库。正式包只使用 complete 版。完整压缩镜像应为 7,007,478,446 字节；解压所得 tar 的 SHA-256 是 `e07158bf61731488e237d2f7a3fe1730779d11c141d045e3c4df8367835e9965`。完整接收后运行 `check_assets.sh`，所有 7 个补丁库须匹配历史哈希。

若上述临时导出目录以后被清理，`export_old_node.sh` 可在原 node25 用共享盘源文件重新生成三份等价归档。重打包后压缩文件哈希可能变化，同步重新生成的 assets.sha256，同时保留对展开镜像、冻结清单、请求体和库文件的原始哈希校验。
