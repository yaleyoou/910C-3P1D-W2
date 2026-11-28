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

获取仓库后，在 P1 编辑仓库根目录的 `cluster.env`，保留文件里的 `IMAGE` 和 `P_STORE`。若某台 P 节点临时不可用，只需把对应行留空（如 `P2=` 或 `P3=`）即可自动按 **2P1D**（或留空两台按 **1P1D**）启动，无需改动脚本。下列四个地址仅供示例，请全部换成实机地址。

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

### 服务器端解包、一键分发与开跑（适配普通用户 + 相同 `sudo` 密码）

以下以控制机 `61.28.30.27`、运行用户 `c60040001`、部署目录 `/home/c60040001/yxy/910C-3P1D-W2` 为例（其余三台节点为 `61.28.30.xx`）。`setup_nodes.sh` 和 `case.sh` 均支持**需要输密码的 `sudo`**：只在启动时交互式输入一次密码，驻留当前进程内存，不落盘、不写入命令行参数，避免长跑中途 `sudo` 缓存过期卡住。

1. **在 `61.28.30.27`（`c60040001` 用户）解包并校验本地资产**：

```bash
cd /home/c60040001/yxy/910C-3P1D-W2
git pull
mkdir -p data
tar -xzf w2-data.tar.gz -C data
tar -xzf memfabric-complete.tar.gz
bash check_assets.sh
```

2. **打通 `61.28.30.27` 上 `c60040001` 到其余三台的 SSH 免密**（只需做一次）：

```bash
# 在 61.28.30.27（c60040001）生成密钥
test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519

# 若其余三台允许 c60040001 密码 SSH，直接分发（把 P2_IP P3_IP D_IP 换成实际 61.28.30.xx）：
for ip in P2_IP P3_IP D_IP; do
  ssh-copy-id -o StrictHostKeyChecking=accept-new "c60040001@$ip"
done
```

> 若服务器禁用了 `c60040001` 的密码 SSH（只能从外部登录进各台切到 `c60040001`），只需把 `61.28.30.27` 上 `cat ~/.ssh/id_ed25519.pub` 输出的那一行追加到另外三台的 `/home/c60040001/.ssh/authorized_keys`（确保 `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`），再在 `.27` 上执行一次 `for ip in P2_IP P3_IP D_IP; do ssh -o StrictHostKeyChecking=accept-new c60040001@$ip hostname; done` 确认免密即可。

3. **在 `61.28.30.27` 编辑 `cluster.env`**：
   - 将 `P1`、`P2`、`P3`、`D` 填为四台互通的业务网 IPv4（若业务网就是 `61.28.30.xx` 则直接填；若业务网是另一网段如 `10.x.x.x`，则同时取消注释 `SSH_P2=c60040001@61.28.30.xx`、`SSH_P3=...`、`SSH_D=...`）。
   - 将 `MODEL_DIR` 填为宿主机模型权重的实际绝对路径。

4. **一键同步四台、并行导入镜像、开启 D 节点 `device-share` 并体检**：

```bash
cd /home/c60040001/yxy/910C-3P1D-W2
bash setup_nodes.sh
```

按提示输入一次 `c60040001` 的 `sudo` 密码后，脚本会自动将 `/home/c60040001/yxy/910C-3P1D-W2`（含 `image.tar.zst`、解包后的 `data/` 与 `upgrades/`、`cluster.env`、`cases.tsv`）同步到参与实验的其余节点相同目录（无 `rsync` 时自动改用 `tar` over SSH），并行 `docker load`，在 D 上开启 16 个 die 的 `device-share`，并完成所有活跃节点的 `node.sh check` 预检。

5. **一键启动测试（全程只需输一次 `sudo` 密码；若集群装有 `tmux` / `screen` 建议在其内运行）**：

```bash
cd /home/c60040001/yxy/910C-3P1D-W2

# 先跑单组 F1：
bash case.sh F1 r1

# 或者一次指定多组 / 全部 8 组连跑（只在一开始输一次 sudo 密码，中途不会再停下等输入）：
bash case.sh F5,F2,F3,F4,EC1,ES1,EJ1 r1
# 或直接跑 cases.tsv 全部组：
bash case.sh all r1
```

每跑完一组，`runs/summary_r1.md` 都会自动更新为与 `evidence/旧1P1D结果.md` 完全一致的 17 列 Markdown 表格。

### 更换节点 / 已有 4 台可用节点时的标准更新与开跑流程

当更换了新节点或 4 台机器全部空闲就绪时，在选作 **P1 控制机**的那台机器上按下面 4 步操作即可：

```bash
cd /home/c60040001/yxy/910C-3P1D-W2

# 1. 更新最新脚本（保留你本地改好的 cluster.env 和 cases.tsv，避免 git pull 冲突）
git fetch origin && git checkout origin/main -- agentx.sh bench.sh case.sh logs.sh node.sh services/ setup_nodes.sh stop.sh watch.sh tools/

# 2. 确认 4 台目标节点的宿主机 NPU 驱动版本均为 26.1.3（把 IP 换成实际节点）
for ip in P1_IP P2_IP P3_IP D_IP; do echo -n "$ip -> "; ssh -o BatchMode=yes -o ConnectTimeout=3 "$ip" 'head -n1 /usr/local/Ascend/driver/version.info 2>/dev/null || echo N/A' 2>/dev/null || echo UNREACHABLE; done

# 3. 编辑 cluster.env，填好 P1 / P2 / P3 / D（P1 必须是当前登录的这台）及 P_STORE / MODEL_DIR，
#    并确认没有残留指向旧机器的未注释 SSH_P2 / SSH_P3 / SSH_D 行；
#    若 P1 到其余三台尚未配置免密 SSH，先执行一次：
test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519
source cluster.env && for h in "${SSH_P2:-$P2}" "${SSH_P3:-$P3}" "${SSH_D:-$D}"; do ssh-copy-id -o StrictHostKeyChecking=accept-new "$h"; done

# 4. 一键同步 4 台仓库+镜像包、并行 docker load（已有镜像自动跳过）、开 D 的 device-share 并体检：
bash setup_nodes.sh

# （若某台残留旧的 glm52-3p1d 容器，一键清干净）
bash stop.sh

# 5. 开跑（若某台曾跑过 r1，换新编号如 r5 避免 runs/ 目录重名）
bash case.sh F1 r5          # 单组 W2（或 bash case.sh all r5 连跑全部组）
# bash agentx.sh F1 r1      # 或跑 AgentX 3600s 零延迟回放（见第 7 节）
```

跑起来后在 P1 新开终端即可使用三件套：
- **实时进度看板**：`sudo bash watch.sh`
- **本机/远程实时日志**：`bash logs.sh d`、`bash logs.sh p1`、`bash logs.sh p2`、`bash logs.sh p3`、`bash logs.sh proxy`
- **一键安全停全部节点**：`bash stop.sh`

### 临时跳过被占用的 P 节点（切换为 2P1D / 1P1D）

如果某台 P 节点（例如 `P2`）当前被他人占用，无需改动任何脚本，按下面 4 步即可直接切到 **2P1D**（留空 `P2` 和 `P3` 则自动按 **1P1D** 运行）：

```bash
cd /home/c60040001/yxy/910C-3P1D-W2

# 1. 拉取最新脚本（保留本地 cluster.env）
git fetch origin && git checkout origin/main -- agentx.sh bench.sh case.sh logs.sh node.sh services/ setup_nodes.sh stop.sh watch.sh tools/

# 2. 将被占用的 P 置空（以跳过 P2 为例；恢复 3P1D 时把 IP 填回即可）
sed -i 's/^P2=.*/P2=/' cluster.env && grep -E '^(P1|P2|P3|D|P_STORE)=' cluster.env

# （可选）2P1D 下若想保持两台 P 的 Store 合计仍为 192 GiB（与原 F1 总预算 384 GiB 一致）：
# sed -i 's/^P_STORE=.*/P_STORE=96/' cluster.env

# 3. 仅同步配置与脚本到实际参与的远端节点（不碰被占用的 P2，无需 rsync）
source cluster.env && for h in "${SSH_P3:-$P3}" "${SSH_D:-$D}"; do tar -cf - cluster.env cases.tsv agentx.sh bench.sh case.sh logs.sh node.sh setup_nodes.sh stop.sh watch.sh services tools | ssh "$h" "tar -xf - -C '$PWD'"; done

# 4. 用新的 trial 名启动（日志会打印 topology=2P1D，proxy.log 显示 Initialized 2 prefill clients）
bash case.sh F1 r2
```

> 若之前失败的 3P1D 运行在节点上残留了旧容器，用 `bash stop.sh` 或 `node.sh down` 只会清理带 `experiment=glm52-3p1d` 标签的本实验容器，不会影响同节点他人的作业；也可用 `ssh -tt <节点IP> "sudo docker rm -f glm52-3p1d"` 精准删除。

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

在 Linux 部署目录执行 `sha256sum -c assets.sha256`，两项都应显示 OK。随后把恢复好的部署目录连同修改后的 cluster.env 复制到其余节点的**同一个绝对路径**，例如 `/home/c60040001/yxy/910C-3P1D-W2`。各节点若共享这个目录，只复制一次。控制机 P1 需要能通过免密 SSH 登录其余活跃节点（`setup_nodes.sh`、`case.sh`、`agentx.sh` 均已支持密码 `sudo`，启动时只输入一次并仅驻留内存；无 `rsync` 时自动回退为 `tar` over SSH；无需宿主机安装 `docker-init`）。不同登录账号可在 P1 设置 `SSH_P2=user@host`、`SSH_P3=user@host`、`SSH_D=user@host`。若无法配置自动流程，使用文末手动分步流程。

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

### 实时查看进度与排障

在 P1 新开一个终端窗口进入仓库目录即可实时监控（不影响正在运行的测试）：

1. **检查所有活跃节点引擎是否已就绪（无需 `sudo`，全返回 `200` 即进入校准/压测）**：

```bash
cd /home/c60040001/yxy/910C-3P1D-W2
source cluster.env
for ip in ${P1:-} ${P2:-} ${P3:-}; do echo -n "P $ip:8100 -> "; curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' "http://$ip:8100/health"; done
echo -n "D $D:8200 -> "; curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' "http://$D:8200/health"
echo -n "Proxy $P1:8300 -> "; curl --noproxy '*' -s -o /dev/null -w '%{http_code}\n' "http://$P1:8300/healthcheck"
```

2. **一键实时进度看板（推荐，每 5 秒刷新，同时兼容 W2 985 条与 AgentX 3600s）**：

```bash
sudo bash watch.sh
```

会实时显示各节点 HTTP 健康状态（`200` / `DOWN`）、当前组别与阶段（`cal` / `perf` / `blocks`）、W2 完成数/百分比/ETA（或 AgentX 已完成数与时长）、实时输出 `tok/s`、TTFT 均值/P95、TPOT 均值、P/D 队列等待、P 实时前缀命中率、D 逻辑 KV 使用率及 P/D Store 已用容量（按 `Ctrl+C` 退出，不影响测试）。

3. **单独追踪引擎日志、Proxy 日志与 Mooncake Store 指标**：

最简方式直接用辅助脚本 `logs.sh`（自动处理 `sudo` 和远端 SSH，无需手拼引号）：

```bash
bash logs.sh d        # 远程实时看 D 节点 d.log（加载/sparse池/图编译）
bash logs.sh p1       # 本机实时看 P1 p.log
bash logs.sh p2       # 远程实时看 P2 p.log（若启用）
bash logs.sh p3       # 远程实时看 P3 p.log（若启用）
bash logs.sh proxy    # 本机实时看 proxy.log
bash logs.sh store-d  # 远程实时看 D 节点 store-d.log
```

或手动执行单行命令（在仓库目录下，注意**不要**在 `source` 前面加 `sudo`）：

```bash
# P1 引擎加载/推理日志
sudo bash -c 'tail -f "$(ls -dt runs/*/*/*/p1 | head -1)/logs/p.log"'

# D 节点加载/sparse池/图编译日志（在 P1 仓库目录下远程查看）
source cluster.env && ssh -tt "${SSH_D:-$D}" "sudo bash -c 'tail -f \"\$(ls -dt '$PWD'/runs/*/*/*/d | head -1)/logs/d.log\"'"

# Proxy 路由与健康检查日志
sudo bash -c 'tail -f "$(ls -dt runs/*/*/*/p1 | head -1)/logs/proxy.log"'

# Mooncake Master 实时段注册与已用容量（无需 sudo）
curl --noproxy '*' -fsS "http://$(. ./cluster.env; echo "$P1"):50089/metrics" | grep -E '^(master|segment)_(allocated|total_capacity)_bytes'
```

**常见问题速查**：
- **容器启动报 `SystemError: <built-in function get_arch> returned NULL` / `Can't get ascend_hal device count` / `dcmi module initialize failed (-8005)`**：说明该节点宿主机 NPU 驱动版本过旧（例如 `25.5.0`），与本镜像内 CANN 不兼容。检查 `/usr/local/Ascend/driver/version.info`，更换为 **`26.1.3`** 驱动节点，或联系集群管理员将该节点 NPU 驱动与固件升级到 `26.1.3` 并重启。
- **启动报 `zmq.error.ZMQError: Address already in use (addr='tcp://...:4100x')`**：旧版 `kv_port=40000/41000` 落在 Linux 默认临时端口段 `32768–60999` 内可能撞端口。拉取最新版 `services/p.sh` 与 `services/d.sh`（已改为 `27000/28000`）并同步到所有节点即可。
- **读 `runs/` 报 `Permission denied`**：部分系统（如 EulerOS）`root` 默认 `umask=0077`。新版 `node.sh` / `case.sh` / `agentx.sh` 已自动设置 `umask 022` 与 `chmod a+rX runs`；若查看旧版本跑出的目录，执行一次 `sudo chmod -R a+rX runs` 即可。
- **报 `Container already exists on ...` / 需要一键停止全部活跃节点上的服务**：运行 `bash stop.sh`（只需输一次 `sudo` 密码，只停止并清理带 `experiment=glm52-3p1d` 标签的本实验容器，不会影响同节点他人的作业）。
- **所有活跃节点 `200 OK` 但迟迟没发请求**：查看 `proxy.log` 的 `Initialized N prefill clients` 核对实际启用的 P 数量是否与 `cluster.env` 一致；另外 `cal` 校准阶段只发 N 条探针、不生成 `events.jsonl` 与 `metrics.jsonl`，属于正常现象。
- **`tar` 同步时提示「时间戳是未来的 xx 秒之后」**：仅表示节点间系统时钟存在固定偏差，不影响启动；`blocks` 阶段会通过 `clock_check.py` 自动测量并对消 P1 与 D 的时钟偏移。

每轮结果目录由组名、trial、阶段、角色组成。失败时保留容器与日志，查看相应节点的 `logs/p.log`、`logs/d.log`、`logs/store-d.log` 或 proxy 日志。修正后在各活跃节点运行 `sudo bash node.sh down`，重试用新 trial（如 `r2`、`r3`）。停止命令只操作带 `experiment=glm52-3p1d` 标签的容器。

## 5. 配额表、自定义配额与扩容对照

`cases.tsv` 定义各组的配额；总预算自动按 `活跃 P 台数 × P_STORE + D_store_GiB + D_sparse_GiB` 计算（3P1D 默认 `3 × 64 = 192 GiB`）。

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

### 如何自定义 P Store、D Store 和 D Sparse KV Pool

三项配额均支持按需修改（**修改 `cluster.env` 或 `cases.tsv` 后，若四台磁盘独立，需同步到其余三台以通过一致性校验**）：

- **P Store（每台 P 的 Mooncake Store，单位 GiB）**：
  - 全局修改：编辑 `cluster.env` 中的 `P_STORE=64`（总 P Store 为 `活跃 P 台数 × P_STORE` GiB；TP16 下平分给 16 个 worker，建议填 16 的整数倍，如 `16 / 32 / 48 / 64 / 96 / 128`；例如 2P1D 下设 `P_STORE=96` 即可保持总 P Store 为 `192 GiB`）。
  - 单组覆盖：也可在 `cases.tsv` 某一行末尾追加可选第 4 列 `P_store_GiB`（不填则使用 `cluster.env` 的 `P_STORE`）。
- **D Store（D 节点 Mooncake Store，单位 GiB）**：
  - 在 `cases.tsv` 第 2 列设置（`0` 表示不启动 D Store donor；`> 0` 时建议取 16 的整数倍，且 D 节点需按第 1 节开启 16 个 die 的 `device-share`）。
- **D Sparse KV Pool（D 节点 `dram_size_per_dp_GB`，单位 GiB）**：
  - 在 `cases.tsv` 第 3 列设置。受 4 TiB MemFabric 补丁在 TP16 下的虚拟地址预留上限约束，**有效上限为 `256` GiB**（`> 256` 会被 `node.sh` 和 MemFabric 拦截）；考虑 W2 长上下文单请求块需求，建议在 **`64`～`256` GiB** 范围内设置，同时确保 `D Store + D sparse` 不超过 D 主机可用物理内存。
  - 示例：在 `cases.tsv` 新增一行 `CUSTOM1	32	224	80`（表示 `D Store=32 GiB`、`D sparse=224 GiB`、每台 `P Store=80 GiB`），同步四台后直接运行 `bash case.sh CUSTOM1 r1`。

## 6. 取结果（CSV 与 17 列原版 Markdown 表）

每组跑完自动保存：
- 单组三 P 分项：`runs/<组>/<trial>/blocks/p1/per-P.json`
- 单组结果：`runs/<组>/<trial>/blocks/p1/result.csv` 与 `result.md`
- 跨组自动汇总：`case.sh` 每跑完一组都会自动刷新 `runs/summary_<trial>.csv` 和 **`runs/summary_<trial>.md`**（与 `evidence/旧1P1D结果.md` 完全一致的 17 列 Markdown 表格）。

也可在 P1 宿主机手动指定任意已完成组汇总导出：

```bash
python3 tools/result.py runs/{F1,F5,F2,F3,F4,EC1,ES1,EJ1}/r1 > summary.csv
python3 tools/result.py --markdown runs/{F1,F5,F2,F3,F4,EC1,ES1,EJ1}/r1 > summary.md
```

| 字段 | 统计口径 |
|---|---|
| 输出吞吐；TTFT 均值 / P95、平均 TPOT | 首发至最后完成的输出 tokens/s；按请求计算 TTFT 和首 token 后的 TPOT；沿用原 finite client 的分位数算法 |
| P 平均排队数；D 平均容量等待数 | 全部正式时间内三台 P 的 waiting 之和的时间均值；D 只计 reason=capacity |
| P 合计前缀命中率 | 三台的本地命中和外部命中 token 增量求和，除以三台本地查询 token 增量之和 |
| P / D Store 平均对象占用 | 逐 segment 按注册 IP 归属，积分对象分配字节；P 列为三台合计，D Store=0 时为零 |
| D 逻辑 KV 平均使用率 | 正式回放窗口内 D 节点 `vllm:kv_cache_usage_perc` 的时间均值（百分比） |
| 平均活跃主 KV | D 上从 ready 到 finish 的活跃物理 slot 只计一次，以 78 层 × (512+64) × 2 字节/token 折算。步间生成 token 数按原算法插值 |
| 平均重复量；全局重复比例 | D 上每个活跃物理块的内容哈希在同一时刻的全局 Store 主 KV key 集合中出现时，累积对应主 KV 字节并做时间平均；除以全部活跃主 KV 的字节·秒得全局重复比例 |
| 曾重复 KV token 位置数；全 workload 曾重复 KV 占比 | 按请求累计全部主 KV token 位置（W2 共 82,229,306）；每个完整 128-token 块在该请求解码期间任一快照匹配即计一次（共享前缀按请求分别计），除以全 workload 总位置数得占比 |

数据完整性以 `request-audit.json`、`overlap.json`、两份 `finite-client/summary.json` 和 per-P 计数为准。旧报告作为比较放在 `evidence/旧1P1D结果.md`。全部四节点指标需由新机器产生。

## 7. AgentX 3600s 零延迟回放（cc-traces-weka-062126-256k）

若要直接复用当前部署的模型、镜像与 `cases.tsv` 配额表，对全量 AgentX 轨迹（`semianalysisai/cc-traces-weka-062126-256k` 的 `traces.jsonl`）做**忽略调用等待时间（零延迟）的 3600s 压测**，只需把下载好的 `traces.jsonl` 放到 **P1 节点**仓库的 `data/traces.jsonl`（其余节点不需要，无需安装 AIPerf）：

> **注意**：Docker 容器只挂载仓库根目录到 `/deploy:ro`，**不要用软链接 `ln -s` 指向仓库外路径**。同磁盘请用**硬链接 `ln -f`**（零额外空间、瞬间完成），跨磁盘则用 `cp`。

```bash
cd /home/c60040001/yxy/910C-3P1D-W2

# 把已下载的 traces.jsonl 硬链接进仓库 data/ 目录（按实际路径替换）：
mkdir -p data && ln -f /home/c60040001/yxy/agentx/cc-traces-weka-062126-256k/traces.jsonl data/traces.jsonl

# （推荐先跑 120s 快速冒烟确认链路通畅，可选）：
bash agentx.sh F1 smoke1 120 16

# 正式单组 3600s 测试（默认 3600s、并发 16 棵根会话树、data/traces.jsonl）：
bash agentx.sh F1 r1

# 自定义时长与并发（例如 3600s、并发 32 棵会话树）：
bash agentx.sh F1 r1 3600 32

# 连跑多组配额（全程只输一次 sudo 密码）：
bash agentx.sh F1,F5,F2,EC1,ES1,EJ1 r1 3600 16
```

- **实时监控与停服**：同样直接用 `sudo bash watch.sh` 看实时已完成请求数/吞吐/TTFT/TPOT/P&D 排队/Store 占用，用 `bash logs.sh d` 看日志，用 `bash stop.sh` 一键停服。
- **回放机制**：完整保留每棵会话树的顺序依赖与并发 subagent 批次，忽略原轨迹的 `t` / `think_time` 等待；按 64-token `hash_ids` 确定性还原共享前缀 token IDs，走 `/v1/completions` 固定输出长度（`out=0` 自动钳位为 `1`）。到 3600s 停止新发请求并自然收尾在途请求（若希望到点立即中断在途请求，前置 `AGENTX_HARD_STOP=1`）。
- **输出报表**：自动保存单组 `runs/<组>/agentx_<trial>_d3600_c16/perf/p1/result.md`（含收尾总口径）、`result-window.md`（严格 3600s 窗口内口径）、`per-P.json` 及跨组汇总表 `runs/summary_agentx_<trial>_d3600_c16.md`，包含完成请求数、输出 tok/s、TTFT 均值/P95、平均 TPOT、P 平均排队数、D 平均容量等待数、P 合计前缀命中率、P/D Store 平均占用与 D 逻辑 KV 平均使用率。

## 手动分步启动

四台可分别登录并有普通 sudo 权限时，可在四台分别运行 `sudo bash node.sh up p1 F1 cal r1`、`... up p2 ...`、`... up p3 ...`、`... up d ...`。先在 P1 发起，其他三台随即启动。各节点 `sudo docker exec glm52-3p1d ...` 使用同一个本机容器名。P1 执行 `sudo docker exec glm52-3p1d bash /deploy/bench.sh`。cal/blocks 完成后把 D 的 `/run-data/logs/d-blocks.jsonl` 拷到同阶段 P1 的 `logs/` 下，cal 在 P1 容器执行 `python3 /deploy/tools/check_probe.py /run-data`。每个阶段结束在四台分别执行 `sudo bash node.sh down`。

依次将阶段改为 perf、blocks；blocks 回放前后仍需要 P1 能通过 SSH 访问 D 进行对时，在 P1 主机用 `python3 tools/clock_check.py user@D /dev/stdout` 保存为同阶段 P1 输出目录的 `clock-before.json` 和 `clock-after.json`。blocks 完成、D 日志已汇集后，在 P1 容器执行 `bash /deploy/finish.sh` 再停服务。详情以 `case.sh` 中的运行顺序为准。

宿主环境参考 [vLLM-Ascend A3 安装文档](https://docs.vllm.ai/projects/ascend/en/v0.30.0/getting_started/installation.html)。模型需保留原配置、tokenizer 和原权重文件以匹配冻结清单。


## 原节点保存位置与恢复

node25 原归档位于 `/home/youxingye/pd3-export-tJNBHq/`。需要转移的三个文件为 `image.tar.zst`、`w2-data.tar.gz` 和 `memfabric-complete.tar.gz`。其中最后一个由管理员身份读取受限的运行库；首次同名类似的 `memfabric-runtime.tar.gz` 仅含部分库。正式包只使用 complete 版。完整压缩镜像应为 7,007,478,446 字节；解压所得 tar 的 SHA-256 是 `e07158bf61731488e237d2f7a3fe1730779d11c141d045e3c4df8367835e9965`。完整接收后运行 `check_assets.sh`，所有 7 个补丁库须匹配历史哈希。

若上述临时导出目录以后被清理，`export_old_node.sh` 可在原 node25 用共享盘源文件重新生成三份等价归档。重打包后压缩文件哈希可能变化，同步重新生成的 assets.sha256，同时保留对展开镜像、冻结清单、请求体和库文件的原始哈希校验。
