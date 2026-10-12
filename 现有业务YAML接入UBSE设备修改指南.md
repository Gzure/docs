# 现有业务 YAML 接入 UBSE 设备修改指南

目标是在原有业务 YAML 上增加旧版 UBSE bonding 设备接入，保留业务镜像、环境变量、配置挂载及网络设置。无需上传二进制；UBSE 查询、分配使用 Python ctypes。

文内导航：[修改 YAML](#3-修改原-yaml-的目标服务) · [附录 A：环境准备与 UBSE 命令](#附录-a-环境准备与-ubse-命令) · [附录 B：权限与日志](#附录-b-权限与日志)

**尚未拿到原 YAML 和启动命令，因此本文是修改模板，不是已适配现场的部署文件。** 先由一线确认格式，再修改对应业务容器；不要用示例整体替换原文件。

## 1. 先确认 YAML 格式

| 现场文件 | 处理方法 |
| --- | --- |
| 顶层有 `services:`，由 `docker compose` 加载 | 使用本文示例 |
| 有 `apiVersion:`、`kind: Pod` 或 `kind: Deployment` | 属于 Kubernetes 风格，不能加入 Compose 的 `devices` 字段；需确认实际管理平台、设备插件及设备映射方式 |
| 由 `docker stack`、Podman 或自定义平台加载 | 不直接套用本文；先确认该工具支持的设备配置和启动行为 |

仅知道底层使用 Docker，不能判断 YAML 格式。不能修改由 Kubernetes/平台管理的容器后再直接用 Docker 重启，否则可能被平台重建覆盖。

以下步骤仅适用于已确认的 **Docker Compose、单个目标服务、一个副本、一个 UBSE 逻辑设备**。多个服务不能默认共用同一次分配。

## 2. 分配设备并记录结果

在实际运行 Docker daemon 的 Linux 宿主机，用管理员账号按本文末尾的[附录 A](#附录-a-环境准备与-ubse-命令)检查环境、确认 C ABI、建立 Bash 会话并粘贴 ctypes 函数定义，再返回本节。后续在同一会话操作，沿用 `LOG_DIR`、`ubse_query`、`ubse_allocate`。

```bash
ubse_query 2>&1 | tee "$LOG_DIR/ubse-discovery.log"
DEVICE_ID='REPLACE_WITH_CONFIRMED_UBSE_NAME'
case "$DEVICE_ID" in REPLACE_*|'') echo '请填写已确认可用的逻辑设备名'; exit 1;; esac
ubse_allocate "$DEVICE_ID" 2>&1 | tee "$LOG_DIR/ubse-allocation.log"
```

**`Healthy: true` 不代表空闲。** 分配前由现场负责人确认设备未被其他业务使用；分配可能激活设备，不要为重复取结果而反复执行。

从本次 `UbsAllocateDevice result` 填入实际值。下面按两条 VFE 路径编写，与现场响应不一致时先确认设备形态，不要猜测或填空值：

```bash
export VFE_PATH_1='REPLACE_WITH_FIRST_VfePaths'
export VFE_PATH_2='REPLACE_WITH_SECOND_VfePaths'
export BONDING_PATH='REPLACE_WITH_BondingPath'
BONDING_EID='REPLACE_WITH_BondingEid'
DEVICE_PATHS=("$VFE_PATH_1" "$VFE_PATH_2" "$BONDING_PATH" /dev/ummu/tid /dev/ubcore/ubcore)
case "$BONDING_EID" in REPLACE_*|'') echo '请填写完整 BondingEid'; exit 1;; esac
for dev in "${DEVICE_PATHS[@]}"; do
  test -c "$dev" || { echo "不是字符设备: $dev"; exit 1; }
  ls -l "$dev"
done
```

逻辑设备名不等于实际路径，`eid0` 不等于完整 BondingEid。不要根据另一台机器的结果填写。`export` 的路径供 Compose 展开配置；它们不会因此自动成为业务容器的环境变量。

## 3. 修改原 YAML 的目标服务

只合并下面列出的字段，服务名替换成原文件中的名字。已有 `devices`、`tmpfs` 列表应保留原项并追加，不要创建重复 YAML 键。`image`、`environment`、`volumes`、`networks`、`ports`、`user`、`working_dir` 等原配置保持不变。

| 字段 | 修改目的 |
| --- | --- |
| `devices` | 映射实际 VFE、bonding 及两个辅助字符设备 |
| `tmpfs` | 存放本次启动的放行文件，重启后清空 |
| `entrypoint` | 先等待 expose 完成，再启动原业务 |
| `command` | 填入原业务的完整入口和参数，不能遗漏镜像入口脚本 |
| `restart` | 手动流程暂设为 `"no"`，避免未经重新核验自动恢复 |

以下只是修改片段，没有镜像等原有配置，不能单独启动：

```yaml
services:
  REPLACE_WITH_SERVICE_NAME:
    devices:
      - "${VFE_PATH_1:?set VFE_PATH_1}:${VFE_PATH_1:?set VFE_PATH_1}:rw"
      - "${VFE_PATH_2:?set VFE_PATH_2}:${VFE_PATH_2:?set VFE_PATH_2}:rw"
      - "${BONDING_PATH:?set BONDING_PATH}:${BONDING_PATH:?set BONDING_PATH}:rw"
      - "/dev/ummu/tid:/dev/ummu/tid:rw"
      - "/dev/ubcore/ubcore:/dev/ubcore/ubcore:rw"
    tmpfs:
      - /run/ubse-start:rw,nosuid,nodev,noexec,mode=1777
    restart: "no"
    entrypoint:
      - /bin/sh
      - -c
      - |
        trap "exit 143" TERM
        trap "exit 130" INT
        printf '%s\n' 'WAITING_FOR_UBSE_EXPOSE'
        until [ -f /run/ubse-start/ready ]; do sleep 1; done
        exec "$$@"
      - ubse-wait
    command: ["REPLACE_WITH_EXECUTABLE", "REPLACE_WITH_ARGUMENT"]
```

**入口填写示例：**原镜像入口为 `/opt/app/start.sh`，原参数为 `--config /etc/app/config.yaml`，则填 `command: ["/opt/app/start.sh", "--config", "/etc/app/config.yaml"]`。修改前结合原 YAML 和 `docker image inspect` 核对完整入口；不能只保留原来的参数。镜像必须有 `/bin/sh`、`sleep`。

`$$@` 必须保留双美元符号，让 Compose 把 `$@` 留给容器内 shell。如果业务 `command` 中也有需要容器 shell 展开的变量，同样用 `$$`，例如 `$$APP_CONFIG`；设备路径的 `${VFE_PATH_1...}` 则刻意由宿主机 Compose 展开。设备映射和入口字段见 [Compose 服务规范](https://docs.docker.com/reference/compose-file/services/)，变量规则见 [Compose 插值说明](https://docs.docker.com/reference/compose-file/interpolation/)。

不默认加 `privileged: true`，也不为本次设备接入修改网络模式。设备访问仍受 UID/GID、安全策略等约束；用户态库沿用原业务的兼容配置。镜像健康检查在等待期间可能失败，现场平台不能据此自动重建或提前放行业务。

## 4. 校验 YAML 并创建等待容器

修改前备份原 YAML，记录原容器的镜像、入口和部署方式。已有运行中的业务需要安排停机/重建窗口；修改 YAML 不会给现有容器热添加设备。

以下示例适用于单个 Compose 文件。若现场原部署有 `-p`、多个 `-f`、`--env-file` 或特定工作目录，必须将原选项保留在 `COMPOSE` 数组中，避免创建另一个项目。路径变量和原项目变量也须在本次启动会话中可用。

```bash
YAML='REPLACE_WITH_EXISTING_COMPOSE_FILE'
SERVICE='REPLACE_WITH_SERVICE_NAME'
case "$YAML:$SERVICE" in *REPLACE_*) echo '请填写原配置路径和服务名'; exit 1;; esac
COMPOSE=(docker compose -f "$YAML")
"${COMPOSE[@]}" config --services
"${COMPOSE[@]}" config > "$LOG_DIR/compose-resolved.yaml"
```

检查展开结果：五个设备路径正确、原镜像/配置/网络仍在、入口无 `REPLACE_`、副本数为一，且业务原入口被完整保留。展开结果可能有凭据，日志须限制权限。不要在变量缺失或目标服务不明确时继续。

检查通过、允许重建目标业务后执行。`--no-deps` 不启动依赖服务，依赖须已就绪：

```bash
"${COMPOSE[@]}" up -d --no-deps "$SERVICE"
mapfile -t CIDS < <("${COMPOSE[@]}" ps -q "$SERVICE")
test "${#CIDS[@]}" -eq 1
CID="${CIDS[0]}"
test "$(docker inspect -f '{{.State.Running}}' "$CID")" = true
docker logs --tail 30 "$CID"
```

**通过标准：**目标容器运行中，日志显示 `WAITING_FOR_UBSE_EXPOSE`，业务尚未启动。没有等待标记时先检查配置是否真正生效，不执行 expose/放行。

## 5. expose 后放行业务

```bash
PID=$(docker inspect -f '{{.State.Pid}}' "$CID")
test "$PID" -gt 0
test -e "/proc/$PID/ns/net"
NETNS=$(readlink "/proc/$PID/ns/net")
docker inspect "$CID" --format 'Network={{.HostConfig.NetworkMode}} Devices={{json .HostConfig.Devices}}'
printf 'ContainerNetns=%s\nHostNetns=%s\n' "$NETNS" "$(readlink /proc/self/ns/net)"
test "$(docker inspect -f '{{.State.Pid}}' "$CID")" = "$PID"
urma_admin agg expose "$BONDING_EID" "/proc/$PID/ns/net" \
  2>&1 | tee "$LOG_DIR/urma-expose.log"
nsenter -t "$PID" -n -- urma_admin show --all \
  2>&1 | tee "$LOG_DIR/urma-target-netns.log"
docker exec "$CID" /bin/sh -c '
  for dev do test -c "$dev" || exit 1; ls -l "$dev"; done
' ubse-check "${DEVICE_PATHS[@]}"
```

host 网络应与宿主机网络命名空间相同，独立网络通常不同。expose 成功后，对照分配结果检查 EID 和设备，主次设备号应与宿主机一致。host 网络下出现重复 expose 等错误时，请驱动负责人确认，不直接忽略。

确认上面检查通过后，单独执行放行命令：

```bash
test "$(docker inspect -f '{{.State.Running}}' "$CID")" = true
test "$(docker inspect -f '{{.State.Pid}}' "$CID")" = "$PID"
test "$(readlink "/proc/$PID/ns/net")" = "$NETNS"
docker exec "$CID" /bin/sh -c ': > /run/ubse-start/ready'
docker logs --timestamps --tail 200 "$CID"
```

最终按原业务方法验证真实 URMA 通信，排除自动回退 TCP。节点存在、容器运行均不能代替通信验收。

## 6. 重启和恢复原配置

- 容器重启后重新取得 CID/PID，确认设备仍有效，再完成第 5 步；不能复用旧 PID。Compose 停止/删除容器不等于 UBSE 设备已释放。
- UBSE/宿主机/驱动重启后先按现场恢复规则确认分配；路径或设备号改变要更新变量并重建容器，不盲目重放 Allocate。
- 需要恢复原配置时，在停机窗口还原已备份 YAML，按原项目方式重建目标服务。此前分配设备的回收须单独交由 UBSE/驱动负责人确认，不手动删除节点。
- ABI 核对和完整命令见本文[附录 A](#附录-a-环境准备与-ubse-命令)，权限与日志说明见[附录 B](#附录-b-权限与日志)。

验证记录：已在本地 ARM64 openEuler Docker 环境检查 Compose 配置，并以模拟字符设备验证非 root 容器的等待/放行、参数传递、环境变量和工作目录保留，以及重启后重新等待。Bash 命令通过语法检查；没有调用真实 UBSE 或执行 URMA expose。

本文未验证现场 YAML、项目启动参数或真实 URMA 通信；Compose 示例需在上述适用条件确认后使用。

## 附录 A 环境准备与 UBSE 命令

按 A.1、A.2、A.3 顺序执行一次，再返回正文第 2 节查询和分配。无需再打开其他手册。

### A.1 检查环境

只在实际 Docker daemon 所在的 Linux 宿主机操作，确认 Docker context/环境变量没有指向远端。需要 Python 3、ctypes、UBSE 客户端库、URMA 工具及驱动；不覆盖 rootless Docker、用户命名空间重映射或 Docker Desktop 设备接入。

```bash
uname -m
docker context show
docker context inspect
printenv DOCKER_HOST DOCKER_CONTEXT || true
docker info
command -v urma_admin
command -v nsenter
command -v python3
python3 -c 'import ctypes, json; print("Python standard library OK")'
ls -l /usr/lib64/libubse-client.so.1
ls -l /dev/uburma/
ls -l /dev/ummu/tid /dev/ubcore/ubcore
```

库或工具缺失时先排查；部分 bonding 节点可能在分配后出现，正文会再次检查。Python 不可用时停止并反馈，不要跳过 UBSE 分配。

### A.2 建立 Bash 会话

先单独执行 `bash`，再执行代码块中的其余命令。后续全程使用此会话；失败退出后应先排查并恢复变量，不能直接跳到业务放行。

```bash
bash
set -euo pipefail
umask 077
LOG_DIR="$HOME/ubse-docker-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"
UBSE_SOCKET=''
date -Is | tee "$LOG_DIR/start-time.txt"
```

`UBSE_SOCKET=''` 使用默认 `/var/run/ubse/ubse.sock`，现场不同则填写实际路径。无需访问 containerd socket。

### A.3 定义查询和分配函数

本命令用 Python 标准库 `ctypes` 调用本机 `/usr/lib64/libubse-client.so.1`。一次完整粘贴到上方建立的 Bash 会话，只定义函数，不立即查询或分配；换会话需重新定义。

**使用前确认 C ABI 兼容。** 可用 `rpm -qf /usr/lib64/libubse-client.so.1` 查包归属，再按现场头文件或发布记录核对。文件名相同不保证兼容，不同软件包版本也可能使用相同 ABI。

| SDK v0.0.9 结构 | 字段顺序与布局 |
| --- | --- |
| `ubs_urma_dev_t` | `char name[32]`、`uint32_t healthy`、`uint64_t hw_res_id`；64 位大小 48 字节，最后字段偏移 40 |
| `ubs_urma_dev_info_t` | `char bonding_path[64]`、`char bonding_eid[64]`、`char vfe_path[2][64]`；大小 256 字节 |

代码的大小检查只校验 Python 端布局，不能自动探测未知动态库的结构。C 健康值 `0` 转为 `Healthy: true`，与旧 Go SDK 一致。

```bash
ubse_call() {
  python3 - "$UBSE_SOCKET" "${1-}" <<'PY'
import ctypes as C
import json
import os
import sys

class Device(C.Structure):
    _fields_ = [("name", C.c_char * 32),
                ("healthy", C.c_uint32), ("hw_res_id", C.c_uint64)]

class DeviceInfo(C.Structure):
    _fields_ = [("bonding_path", C.c_char * 64),
                ("bonding_eid", C.c_char * 64),
                ("vfe_path", (C.c_char * 64) * 2)]

def decode(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8")

def check_rc(operation: str, code: int) -> None:
    if code != 0:
        raise SystemExit("ERROR: {} returned {}".format(operation, code))

socket = sys.argv[1].encode("utf-8")
name = sys.argv[2].encode("utf-8")
if len(socket) > 107 or b"\0" in socket:
    raise SystemExit("ERROR: invalid UBSE socket path")
if len(name) >= 32 or b"\0" in name:
    raise SystemExit("ERROR: device name must be under 32 bytes")
if (C.sizeof(C.c_void_p) != 8 or C.sizeof(Device) != 48
        or Device.hw_res_id.offset != 40 or C.sizeof(DeviceInfo) != 256):
    raise SystemExit("ERROR: unsupported local ABI layout")

try:
    lib = C.CDLL("/usr/lib64/libubse-client.so.1", mode=os.RTLD_LAZY)
    initialize = lib.ubs_engine_client_initialize
    initialize.argtypes, initialize.restype = [C.c_char_p], C.c_int32
    finalize = lib.ubs_engine_client_finalize
    finalize.argtypes, finalize.restype = [], None
    get = lib.ubs_urma_dev_get
    get.argtypes = [C.POINTER(C.POINTER(Device)), C.POINTER(C.c_uint32)]
    get.restype = C.c_uint32
    allocate = lib.ubs_urma_dev_alloc
    allocate.argtypes, allocate.restype = [C.c_char_p, C.POINTER(DeviceInfo)], C.c_uint32
    libc = C.CDLL(None)
    free = libc.free
    free.argtypes, free.restype = [C.c_void_p], None
except (OSError, AttributeError) as exc:
    raise SystemExit("ERROR: load UBSE library/symbols: {}".format(exc))

check_rc("ubs_engine_client_initialize", initialize(socket))
try:
    devices = C.POINTER(Device)()
    count = C.c_uint32()
    try:
        check_rc("ubs_urma_dev_get", get(C.byref(devices), C.byref(count)))
        if count.value > 65535 or (count.value and not devices):
            raise SystemExit("ERROR: invalid device list returned by UBSE")
        rows = [{"Name": decode(devices[i].name),
                 "Healthy": devices[i].healthy == 0,
                 "HwResId": devices[i].hw_res_id}
                for i in range(count.value)]
    finally:
        if devices:
            free(devices)
    print("UbsGetSharedDevice equivalent result:", flush=True)
    print(json.dumps(rows, indent=2), flush=True)
    if name:
        selected = [row for row in rows if row["Name"] == decode(name)]
        if len(selected) != 1 or not selected[0]["Healthy"]:
            raise SystemExit("ERROR: requested name is absent, duplicate or unhealthy; no allocation")
        print("WARNING: calling ubs_urma_dev_alloc; MAY ACTIVATE device", flush=True)
        info = DeviceInfo()
        check_rc("ubs_urma_dev_alloc", allocate(name, C.byref(info)))
        print("UbsAllocateDevice result:", flush=True)
        print(json.dumps({"VfePaths": [decode(bytes(p)) for p in info.vfe_path],
                          "BondingPath": decode(info.bonding_path),
                          "BondingEid": decode(info.bonding_eid)}, indent=2), flush=True)
finally:
    finalize()
PY
}
ubse_query() { ubse_call ''; }
ubse_allocate() { ubse_call "${1:?must supply a confirmed UBSE Name}"; }
```

`ubse_query` 只查询；`ubse_allocate "$DEVICE_ID"` 才分配。`free` 释放查询结果的内存，`finalize` 关闭客户端连接，均不等于释放设备。本仓库所用版本没有官方 Python URMA 封装；不能用 `urma_admin show` 代替 UBSE 分配。

## 附录 B 权限与日志

**设备权限：**`devices` 的 `rw` 不会绕过 UID/GID 或宿主机安全策略。旧插件会将 `/dev/ummu/tid` 改为 `0666`，本文不自动修改；确需复现时，由管理员记录原权限、评估影响后执行 `chmod 0666 /dev/ummu/tid`。不要默认增加特权或关闭安全策略。

**动态库：**设备映射不会安装用户态库。保留原业务依赖和经过确认的具体库挂载，不整体覆盖容器 `/usr/lib64`。

**日志收集：**在正文的同一个会话中执行。若尚未创建容器或取得 CID，跳过前两条，只收集已有的 UBSE/分配日志和宿主机日志。

```bash
docker logs --timestamps --tail 500 "$CID" 2>&1 | tee "$LOG_DIR/business.log"
docker inspect "$CID" > "$LOG_DIR/container-inspect.json"
journalctl -u docker --since '30 minutes ago' --no-pager > "$LOG_DIR/docker-daemon.log"
```

非 systemd 部署改查实际 Docker 日志。UBSE 服务名用 `systemctl list-units --type=service --all | grep -i ubse` 查候选后核对；容器部署则取对应 UBSE 容器日志。inspect、展开的 Compose 配置和业务日志可能含凭据，提交前脱敏。

**故障定位：**分配失败看客户端返回码及 UBSE 日志；节点缺失看驱动/创建结果；expose 失败看完整 EID、当前 PID/netns 和工具版本。不要重复分配来探测状态。恢复原 YAML 和设备回收按正文第 6 节分别处理。
