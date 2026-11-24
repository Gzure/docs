---
name: urma-develop
description: 把 URMA（灵衢 UB 内存语义通信）接入到业务项目并做性能调优时使用：资源建模、建链、内存注册、收发与 poll、bonding、异常恢复、参数调优与定位，参考 ubsocket/UMQ 与 yuanrong-datasystem 的做法。
---

# URMA 接入开发与调优手册

本 skill 用于**在其他项目中使能 URMA**（而不是修改 umdk 里的 URMA 源码）：设计接入层、写建链/收发/完成处理代码、选参数、做性能调优和问题定位。
经验来自两个已落地的接入方：

- **ubs-comm**：`src/hcom/umq`（UMQ，C，消息队列语义）与 `src/ubsocket`（兼容 socket，叠在 UMQ 上；`csrc/core/urma` 是一层轻量 C++ RAII 封装）。
- **yuanrong-datasystem**：`src/datasystem/common/rdma/urma_*`（对象缓存数据面，RDMA WRITE/READ 零拷贝），调用链路详见其 `docs/urma_call_flow.md`。

权威来源：API 以 umdk `src/urma/lib/urma/core/include/urma_api.h`、`urma_types.h`、`urma_opcode.h` 为准；bonding 扩展以 `src/urma/lib/urma/bond/include/urma_ubagg.h` 为准；行为说明见 `doc/ch/urma/URMA User Guide.ch.md`、`URMA API Guide.ch.md`。写代码前先核对目标环境安装的头文件版本，不要凭记忆编造字段。
从 ibverbs 迁移时另见 umdk `skills/verbs-to-urma-converter`。

---

## 0. 先做接入形态决策

| 业务诉求 | 推荐形态 |
|---|---|
| TCP/socket 应用（bRPC 等），尽量不改代码 | 直接用 **ubsocket**（LD_PRELOAD/链接 + `UBSOCKET_*` 环境变量），不要自己写 URMA |
| 消息/RPC 语义，需要内存池、流控、多平面 | 用 **UMQ / URPC**，只在其不满足时下沉 |
| 大块数据零拷贝、对端内存地址可事先交换（存储、缓存、KV、参数传输） | **直接用 URMA 单边 WRITE/READ**（yuanrong 模式），控制信息走现有 RPC |

直接用 URMA 时，接入层建议分三层（两个项目都是这样组织的）：

```
业务层        只看到 Write/Read/Send(地址, 长度, 对端ID) + 完成回调/等待
管理层(单例)  建链握手、连接表、WR 提交与分块、poll 线程、事件通知、降级策略
资源层        context/JFC/JFCE/JFR/Jetty/Segment 的 RAII 封装、Jetty 池、异常事件线程、退休/补池
 Provider       liburma（直接链接或 dlopen 符号表）
```

---

## 1. 依赖、加载与初始化

- **链接方式**
  - dlopen + 函数指针表：ubsocket（`under_api/urma/dl_urma_api.*`）和 UMQ（`umq_symbol_private.*`）都这样做。好处是未安装 umdk 时也能启动并降级 TCP，缺符号时可以按版本开关特性。
  - 直接 `-lurma`：yuanrong 的真实路径。更简单，但会引入硬依赖。
  - 不论哪种方式，统一经过一层 `xx_urma_*` 转发。这样既方便 mock，也方便在每个调用上统一打点和记录耗时。
- **编译与运行双门控**：编译宏（如 `USE_URMA`）加运行开关（如 `-enable_urma`）。关闭时必须能正常走 TCP。
- **初始化只执行一次**：用 mutex 或 CAS 保护，顺序为：
  1. `urma_init(&attr)`；
  2. `urma_register_log_func(cb)`，把 URMA 日志导入业务日志；
  3. `urma_log_set_level()`，与业务日志级别对齐。
  - 参考 ubsocket `urma_backend.cpp` 对级别映射的处理：URMA 是 0=EMERG … 7=DEBUG。
  - 进程退出时按第 9 节的顺序释放资源，最后调用 `urma_uninit()`。
- **CI 和无硬件环境**：提供 mock backend。
  - yuanrong 用 `USE_URMA_MOCK`，并通过弱符号分发表把 `ds_urma_*` 指到进程内 mock，见 `tests/support/.../urma_mock`。
  - UMQ 有 `stub/urma`。
  - 注入点用来模拟 `JETTY_ERR`、`JFC_ERR` 异常事件和 CQE 失败。

---

## 2. 设备与 EID 选择

- **默认用聚合（bonding）设备**。鲲鹏 950 上只有一个聚合设备，固定名为 `bonding_dev_0`。它屏蔽了 2 平面、IODie、primary/port EID 等拓扑细节，并提供带宽聚合和故障切换。普通 udma 设备一般只在调试时使用。
- **设备名和 EID index 做成可配置**：
  - ubsocket 用 `UBSOCKET_UB_DEV`；yuanrong 用 `DS_UB_DEV_NAME`（默认 `bonding_dev_0`）和 `DS_UB_DEV_EID`。
  - 可以用 `urma_admin show` 或 `urma_admin show --whole` 查设备和能力。
- **候选回退**：裸机上默认的 bonding 设备可能已被容器占用。此时按「配置的名字→其他 `bonding*`」的顺序依次尝试，参考 yuanrong `UrmaGetEffectiveDevices`。
- **标准流程**：
  1. `urma_get_device_by_name()`；
  2. `urma_query_device()`，缓存 `dev_cap`，后面的队列深度、SGE、inline、单次最大读写长度都从这里取；
  3. `urma_get_eid_list()`，校验 eid_index 是否越界；
  4. `urma_create_context(dev, eid_index)`。
- **一个 (设备, eid) 只建一个 context**，进程级缓存，参考 ubsocket `UrmaContext::CreateContext`。
- **设置 bonding 模式**：在 context 创建后、创建任何队列之前，用 `urma_user_ctl` 完成。
  ```c
  bondp_set_bonding_mode_in_t in_mode = { .bonding_mode = BONDP_BONDING_MODE_BALANCE /* 或 ACTIVE_BACKUP */,
                                          .bonding_level = BONDP_BONDING_LEVEL_PORT };
  urma_user_ctl_in_t in = { .addr = (uint64_t)(uintptr_t)&in_mode, .len = sizeof(in_mode),
                            .opcode = BONDP_USER_CTL_SET_BONDING_MODE };
  urma_user_ctl_out_t out = {0};
  urma_user_ctl(ctx, &in, &out);
  ```
  - **Balance**：多设备并发，提升吞吐。yuanrong 的大块数据面用这个模式。
  - **ActiveBackup**：只用主设备，故障时切到备设备。UMQ/ubsocket 用这个模式。
  - 不需要 MSN 时，UMQ 还会调用 `BONDP_USER_CTL_DISABLE_MSN`。
  - `BONDP_USER_CTL_QUERY_PORT` 可以查询当前已启用和已激活的端口，用于健康检查。
- **NUMA/芯片亲和**：需要控制从哪个 IODie 发出时，把 WR 包成 `bondp_jfs_wr_t`，填写 `src_chip_id`/`dst_chip_id`，并设置 `flag.bs.has_drv_ext=1`。
  - 创建队列时可以用 `bondp_jetty_cfg_t`/`bondp_jfr_cfg_t` 中的 `port_ids` 指定端口。
  - yuanrong 的做法：轮转选择源芯片，内存所在芯片优先，对应 `ub_numa_rr_type`/`ub_numa_src_chip_policy`。
  - ubsocket 的做法：按业务线程所在 CPU 亲和的 IODie 优先，对应 `UBSOCKET_SCHEDULE_POLICY=affinity_priority`。

---

## 3. 队列资源模型与推荐参数

### 3.1 创建顺序
```
context → [jfce]（仅事件模式需要）→ jfc → jfr（使用 recv jfc）→ jetty（jfs_cfg + share_jfr）
```
两个项目都采用 **Jetty + 共享 JFR**：设置 `jetty_cfg.flag.bs.share_jfr = 1`，并把 `jetty_cfg.shared.jfr` 指向这个 JFR。
- 多个连接共享同一个接收队列和接收内存池，连接数增长时内存和 RQE 不会跟着线性膨胀。
- 大规模连接场景下，这是首选。

### 3.2 参数参考（所有 cfg 先零初始化）

| 参数 | UMQ/ubsocket | yuanrong | 说明 |
|---|---|---|---|
| `jfc.depth` | 与队列深度一致（默认 1024/2048） | `dev_cap.max_jfc_depth` | 必须 ≥ 挂在它上的所有 JFS/JFR 在途 WR 总数，否则会 CQ 溢出 |
| `jfs.trans_mode` | 可配，默认 RM（`UBSOCKET_UB_TRANS_MODE=RM_CTP`） | RM | 见 3.3 |
| `jfs.depth` | `UBSOCKET_TX_DEPTH`（默认 1024） | 发送 jetty 为 256，接收 jetty 的 jfs 为 32 | 上限是 `max_jfs_depth` 与 `max_jfc_depth` 中的较小值 |
| `jfs.max_sge` / `max_rsge` | 单 SGE，或用 `dev_cap` 上限 | 按 gather 需求 | SGE 越多，WQE 越大 |
| `jfs.max_inline_data` | `dev_cap.max_jfs_inline_len` | 0（纯大块 WRITE 不需要） | 小消息开 inline 可以省掉一次 DMA |
| `jfs.rnr_retry` | 6 | 6 | |
| `jfs.err_timeout` | 2 | 0 | |
| `jfs.priority` | `UBSOCKET_LINK_PRIORITY`（默认 4，映射到 SL） | 可配 | 业务流量分级 |
| `jfs.flag.bs.multi_path` | 取决于模式 | 1 | 两端必须一致，否则不通 |
| `jfs.flag.bs.order_type` | `URMA_DEF_ORDER` | 默认 | 有保序要求时在 WR 上设置 `place_order=URMA_STRONG_ORDER` |
| `jfr.depth` | `UBSOCKET_RX_DEPTH`（默认 2048） | 按需 | 双边通信的关键参数。RQE 不足会触发 RNR |
| `jfr.min_rnr_timer` | 19（约 1.05s） | `URMA_TYPICAL_MIN_RNR_TIMER` | |
| `jfr/seg token_policy` | 可开关，token 随机生成 | `URMA_TOKEN_PLAIN_TEXT` | 见第 5 节 |
| `flag.bs.lock_free` | 若队列只在单线程使用，设为 `io_lock_free/rq_lock_free=true` | — | 省去 provider 内部锁。**有多线程并发访问时不要开** |

### 3.3 传输模式与传输层选择

| 组合 | 特点 | 适用 |
|---|---|---|
| RM + CTP（bonding 多路径） | 无需 bind，一个 jetty 可以对接多个对端。单包最大 4kB | 公知 jetty、自举建链、大量对端、单边大块传输（yuanrong 默认） |
| RC + RTP（单路径） | 两端都要 `urma_bind_jetty` 后才能做双边通信。单包最大 64kB，支持 loopback | 点对点长连接、对延迟敏感的双边消息 |

- 使用 bonding 时，只有创建 jetty/jfs/jfr 时的参数决定走 TP 还是 CTP；`urma_import_jetty` 的 rjetty 中的 CTP 标志会被忽略。
- 单路径和多路径的 jetty 之间不能互通。
- 内存池分片要对齐单包上限：ubsocket 在 CTP 下分片为 4k，TP 下为 8k（`UBSOCKET_BLOCK_TYPE`）。
- bonding 设备不支持 `urma_query_jfs` 和 `urma_flush_jfs`，请用 jetty 版本的接口。

### 3.4 Jetty 池化
不要每个请求新建一个 jetty，创建和销毁都要走内核，开销很大。
- yuanrong：进程级 `SendJettyPool`，启动时预先填充（`urma_send_jetty_lane_pool_size`，默认 200）。
  - 按 lane 借用和归还。
  - 单个对端最多占用 `urma_send_lane_count_per_peer` 个 lane。
  - 后台 `RefillLoop` 线程负责补池。
- ubsocket：`UBSOCKET_JETTY_TYPE=pool`，配合 `UBSOCKET_JETTY_POOL_SIZE`（默认 800）。
  - 每个 jetty 都占 fd，需要调大 `ulimit -n`（建议 65535）。

---

## 4. 建链（带外交换 + import + bind）

URMA 本身不提供带外通道，需要用业务现有的 TCP/RPC 交换建链信息。
- ubsocket 在 TCP 握手中携带，对应 `UBSOCKET_UB_HANDSHAKE_MODE`。
- yuanrong 通过 `WorkerWorkerExchangeUrmaConnectInfo` RPC 携带。

1. **本端导出**：`urma_get_rjetty(jetty, &rjetty, &len)`，序列化后立即调用 `urma_put_rjetty(rjetty)`。
   - **必须按返回的 `len` 拷贝完整缓冲**。bonding 场景下 rjetty 后面带有变长扩展（`urma_bond_jetty_ext_*`），只拷贝 `sizeof(urma_rjetty_t)` 会丢失多路径和健康检查信息。
   - 自行补充 `flag.bs.token_policy` 和 `tp_type` 后再发送。
   - 还要一起发送：jetty token、要暴露的 segment 信息（ubva、len、token_id、seg flag、token）、进程实例唯一 ID。
2. **对端导入**：
   - 收到长度先校验。`len < sizeof(urma_rjetty_t)` 直接拒绝，并要防止整数溢出。
   - 调用 `urma_import_jetty(ctx, rjetty, &token)` 得到 `tjetty`。
   - 对每个远端段调用 `urma_import_seg(ctx, &seg, &token, 0, flag)`，其中 flag 常用配置为 `NON_CACHEABLE` + `URMA_SEG_NOMAP` + access。
3. **RC 模式**：调用 `urma_bind_jetty(jetty, tjetty)`。
   - 返回 `URMA_EEXIST` 视为成功。
   - 失败时要 `urma_unimport_jetty`。
   - 两端都 bind 成功后才能发双边消息。RM 模式跳过这一步。
4. **数据面探测**：建链后发一个很小的探测 WR（yuanrong 发 1 字节 WRITE），等完成后再把连接标记为可用。这样配置错误能在建链阶段就暴露，而不是拖到业务请求时才发现。
5. **重连判定**：用对端实例 ID（如 `uniqueInstanceId`）判断对端是否重启。
   - 对端重启后，旧的 tjetty 和 tseg 全部作废，需要重新握手。
   - 长时间不用的孤儿连接定期清理，参考 yuanrong `urma_orphan_conn_ttl_s` 和 `urma_orphan_sweep_*`。
6. 建链的各个阶段分别打点：import_jetty、bind、get/put_rjetty。建链延迟通常是大规模场景的瓶颈。

---

## 5. 内存注册（Segment）与安全

- **注册是重操作**（要经过 pin 内存和 UMMU 映射），绝不能放在每次 IO 的路径上。
  - 推荐做法：启动时把整块内存池或 arena 一次性注册。yuanrong 对应 `urma_register_whole_arena=true`，客户端会 mmap 256MB 传输池；UMQ 按 mempool 注册。
  - 如果必须按对象注册，要加注册缓存（`GetOrRegisterSegment`）。
  - 对端导入的段按 (对端, mempool_id) 缓存到 hash 表里，重复导入直接跳过。
- `urma_seg_cfg_t` 常用配置：
  ```c
  urma_reg_seg_flag_t flag = { .bs.token_policy = URMA_TOKEN_PLAIN_TEXT /* 或 URMA_TOKEN_NONE */,
                               .bs.cacheable = URMA_NON_CACHEABLE,
                               .bs.access = URMA_ACCESS_READ | URMA_ACCESS_WRITE | URMA_ACCESS_ATOMIC };
  urma_seg_cfg_t cfg = { .va = (uint64_t)(uintptr_t)addr, .len = size, .token_value = {.token = tok},
                         .flag = flag, .token_id = NULL, .iova = 0 };
  ```
  - access 按最小权限去给。只会被对端写入的段，不要开 ATOMIC。
- **token**：生产环境应开启 token_policy，并用随机数生成 token（参考 UMQ `umq_ub_token_generate`）。
  - jetty/jfr 和 segment 各自使用独立的 token。
  - token 只能通过可信的带外通道传递，**禁止打印到日志**。
  - import 时对端 rjetty 的 token_policy 必须和本端匹配。
- 一般用 RDMA WRITE 把数据写到对端预先申请好的缓冲，对端不需要拷贝。地址传递用 `seg_va + offset`，比如 yuanrong 的 `UrmaRemoteAddrPb`。

---

## 6. 数据面：提交 WR

- **单边 WRITE/READ**（大块数据）：
  ```c
  urma_sge_t src = { .addr = laddr, .len = n, .tseg = local_tseg };
  urma_sge_t dst = { .addr = raddr, .len = n, .tseg = remote_imported_tseg };
  urma_jfs_wr_t wr = { .opcode = URMA_OPC_WRITE /* or URMA_OPC_READ */,
                       .flag.bs.complete_enable = 1,
                       .tjetty = tjetty, .user_ctx = req_id,
                       .rw = { .src = { .sge = &src, .num_sge = 1 }, .dst = { .sge = &dst, .num_sge = 1 } } };
  urma_jfs_wr_t *bad = NULL;
  urma_status_t st = urma_post_jetty_send_wr(jetty, &wr, &bad);
  ```
- **分块**：单个 WR 的长度不能超过 `dev_cap.max_write_size`/`max_read_size`，大对象要切块后串成 WR 链（`wr.next`）一次提交。
  - 可以只在最后一块上开 `complete_enable`，以减少 CQE。前提是保序语义允许；不确定时每块都开。
  - 发送方要做流水：在途 chunk 数设上限，完成一块释放一块，参考 yuanrong `PipelineDataTransferHelper`。
- **聚合**：多个小对象写入对端同一个连续缓冲时，用多 SGE 的 gather write 合成一个 WR（`UrmaGatherWriteWithLane`）。
- **小消息**：用 send 或 send_imm，并开启 `inline_flag`（不超过 `max_inline_data`）。立即数（imm）可以携带控制类型或信用字段，参考 UMQ 的 `umq_ub_imm_t`。
- **user_ctx 编码**：写入请求 ID 或缓冲指针，完成时用它定位到对应的等待者。
  - yuanrong 用 40-bit reqId 加事件表。
  - UMQ 把指针直接放进 user_ctx，并用 `user_ctx==0` 标记不需要上报的内部 WR。
- **双边通信必须有流控**：接收端 RQE 用尽就会触发 RNR 重传，延迟会跳到秒级。
  - UMQ 的做法：基于信用的流控。每个 WR 消耗 1 个信用；信用用尽时按倍数重新申请；连接空闲时归还信用；每个连接至少保留一个最小信用数。对应 `UBSOCKET_INITIAL_CREDIT`/`MAX_CREDIT_PER_REQUEST`/`MIN_RESERVED_CREDIT`。
  - 接收端及时补充 RQE（`urma_post_jetty_recv_wr`/`urma_post_jfr_wr`）。补充时批量提交，不要一次一个。
- **提交失败处理**：`bad_wr` 指向的 WR 及其后面的所有 WR 都没有提交。
  - 这些 WR 对应的事件和在途计数必须回滚。
  - 提交失败不等于连接断开。`URMA_EAGAIN` 一般是队列满，应该退避重试或排队。
- **并发模型**：一个 jetty 同一时刻只让一个线程提交，比如每个 lane 独占一个 jetty，这样可以开启 lock_free。
  - 可以用 PostGate/PostPermit 这类原子门做保护，在 jetty 退休时阻止新的提交。

---

## 7. 完成处理：poll 与事件模式

### 7.1 忙轮询（延迟最低）
```c
urma_cr_t cr[BATCH];
int n = urma_poll_jfc(jfc, BATCH, cr);   /* <0 表示出错，0 表示没有完成记录 */
```
- 批量大小：yuanrong 用 16，UMQ 用 256。按单次完成的峰值来定。
- 没有完成记录时：短暂 `nanosleep(1us)` 后再试。注意 `usleep(0)` 受 timerslack 影响，实际平均约 50us。
  - 可以将线程的 timerslack 设小（`prctl(PR_SET_TIMERSLACK)`）。
  - 或者先自旋 N 次再休眠。
- poll 线程**绑核**，并隔离在与网卡 IODie 亲和的 NUMA 上。要监控两次 poll 之间的间隔。
  - yuanrong 的 `[URMA_ELAPSED_THREAD_SHED]` 日志就是为这个问题加的，门限由 `urma_log_threshold_us` 控制。

### 7.2 事件模式（CPU 占用低，适合连接多、流量稀疏的场景）
```
创建：jfce = urma_create_jfce(ctx)；jfc_cfg.jfce = jfce；urma_create_jfc；立即 urma_rearm_jfc(jfc, false)；
      jfce->fd 设为 O_NONBLOCK 后可以挂到 epoll（ubsocket 的做法）
循环：n = urma_wait_jfc(jfce, cnt, timeout, jfcs)
      → urma_ack_jfc(jfcs, nevents, n)            // 必须 ack，否则销毁 jfc 时会卡住
      → urma_poll_jfc 抽干
      → urma_rearm_jfc(jfc, solicited_only)
      → 再 poll 一次，关闭 rearm 前后的竞态窗口
```
- ack 可以批量做，但必须在删除 JFC 之前全部 ack 完。
- 每个带 jfce 的 JFC 都要单独 rearm。比如发送 JFC、接收 JFC、流控 JFC，漏掉任意一个就会丢事件。
  - bonding 的 balance 模式下也存在这个问题，umdk 已在内部 rearm 所有 p_jfc。
- 一个 jfce 可以被多个 JFC 共享，用这种方式减少 fd 数量。
- 要统计事件模式下 wait、ack、rearm 三个环节各自的耗时。

### 7.3 完成记录（CR）状态处理
- `URMA_CR_SUCCESS`：通知等待者，并释放 lane 或信用。
- `URMA_CR_WR_FLUSH_ERR_DONE`：表示该 jetty 的 flush 已经完成，可以安全删除。这是生命周期信号，不要当成业务错误上报。
- `URMA_CR_WR_SUSPEND_DONE`：传输层异常导致 jetty 挂起。
  - 无序模式下可以继续发送。
  - 有保序要求时，先 flush，再按顺序重发。
- 其他失败状态：
  - 远端访问或长度错误，属于参数或协议 bug，不要重试。
  - `ACK_TIMEOUT`/`RNR_RETRY_CNT_EXC`，属于链路问题或对端缓冲不足，走第 8 节的恢复流程。
- 进程内会有迟到的 CQE：lane 已经超时回池后才收到完成。必须能识别这种过期完成并丢弃，参考 yuanrong `IsStaleSendCompletion` 和 orphan WR 记账。
- 错误日志必须限速。单条日志里带上 eid、jetty_id、对端 eid 和对端 jetty_id、cr.status。

---

## 8. 异常事件、故障恢复与降级

- **异常事件使用独立线程**：循环调用 `urma_get_async_event(ctx, &ev)` → 按 `ev.event_type` 分类处理 → `urma_ack_async_event(&ev)`。
  - **对象上报过异常事件时，必须先 ack 再删除该对象。**
  - `JETTY_ERR`/`JFC_ERR`/`JFR_ERR`：按 jetty id 定位到对象，将其退休。
  - `JFR_LIMIT`/`JETTY_LIMIT`：补充接收队列，或者收紧流控。
  - `PORT_DOWN`/`PORT_ACTIVE`：更新端口健康状态，用于控制准入和主备切换。
  - `DEV_FATAL`/`EID_CHANGE`：重建 context，或者整体降级。
- **故障 jetty 退休流程**（两个项目一致）：
  ```
  关闭准入（不再派发）
   → urma_modify_jetty(jetty, {.mask=JETTY_STATE, .state=URMA_JETTY_STATE_ERROR})（JFR 同样设为 URMA_JFR_STATE_ERROR）
   → poll 直到收到 URMA_CR_WR_FLUSH_ERR_DONE（或用 urma_flush_jetty 取回未完成的 WR）
   → 把对应请求标记为失败并通知等待者
   → unbind（仅 RC）→ unimport tjetty → delete jetty
   → 后台补池
  ```
  - 删除失败或一直无法收敛的句柄不要强删，先隔离起来（fail-closed），留到进程退出时再处理。
- **降级到 TCP**：UB 不可用时必须有功能完全等价的 TCP 路径。
  - ubsocket：`UBSOCKET_DEGRADE_ENABLE`，另有主备链路 `UBSOCKET_BACKUP_LINK_ENABLE`。
  - yuanrong：单次写入失败时，这笔数据改用 payload 回传（有限流）。按窗口统计成功率，低于阈值则切换 worker（`urma_failover_success_rate_ratio` 和 `urma_failover_min_sample_count`）。
  - 可选开启连接健康探测：`UBSOCKET_PROBE_*`，或 yuanrong 的 `ProbeProviderUbRecovery`。
- **超时处理**：等待完成超时后，只是不再等这个结果，并不意味着 WR 已经结束。
  - 超时 WR 记入 orphan 账。
  - lane 强制回池。
  - orphan 数超过阈值时，将该 jetty 退休。
  - **在收到完成或 flush 之前，不能复用这块目标内存。**

---

## 9. 销毁顺序
```
停止准入 → 等待在途 WR 排空（或按第 8 节流程将 jetty 置为 ERROR 并 flush）
→ [RC] urma_unbind_jetty → urma_unimport_jetty / urma_unimport_seg
→ urma_delete_jetty → urma_delete_jfr → urma_delete_jfs
→ 确认事件已全部 ack → urma_delete_jfc → urma_delete_jfce
→ urma_unregister_seg → urma_delete_context → urma_uninit
```
- 用引用计数或智能指针表达对象之间的依赖关系。比如 jetty 持有 jfs/jfr，而 jfs/jfr 持有 jfc，保证析构顺序不会出错。
- 各个 delete 失败只打告警，销毁继续往下执行。
- 批量销毁可以用 `urma_delete_*_batch`。

---

## 10. 调优清单

按以下顺序排查。每一步都先用 `urma_perftest` 建立硬件基线，再拿业务结果和基线对比。测试项包括 `write_lat`/`write_bw`/`read_*`/`send_*`/`atomic_*`，以及全长度扫描 `all`。

1. **拓扑与设备**
   - 用 `urma_admin show --whole` 查看端口状态、MTU、各项 `max_*`。
   - 用 `urma_admin show topo` 查看 bonding EID 与 primary/port EID 之间的逻辑连通关系。注意这只是逻辑关系，物理上是否真的连通还要另外核实。
   - 确认使用的是 bonding 设备，并且聚合模式和业务需求匹配：带宽型业务用 Balance，高可靠消息用 ActiveBackup。
2. **传输模式**
   - RM+CTP 和 RC+RTP 中选一个（见 3.3）。
   - 单包上限：CTP 为 4k，RTP 为 64k；内存分片和分块大小要和它对齐。
3. **队列深度**
   - TX/RX 深度要覆盖带宽时延积需要的在途量。
   - JFC 深度要 ≥ 挂在它上的所有在途 WR 之和。
   - 深度过大会占内存和硬件上下文，并没有收益。
4. **小消息延迟**
   - 开启 inline。
   - 使用忙轮询。
   - poll 线程绑核并做 NUMA 亲和。
   - 业务线程和 IODie 亲和。
   - 队列单线程使用时开 lock_free。
   - 减少 CQE：批量的 WR 只在尾部产生完成。
   - 不要在数据面加锁、分配内存或打印日志。
5. **大块带宽**
   - 用单边 WRITE/READ 做零拷贝传输。
   - 大对象按 `max_write_size` 分块，流水发送，并限制在途量。
   - 多个 lane 或多个 jetty 并发发送，配合 Balance 模式和源芯片轮转。
   - 小对象用 gather 合并发送。
   - 内存整块注册。
6. **大规模连接**
   - 共享 JFR 和接收内存池。
   - jetty 使用池化。
   - 事件模式下让多个 JFC 共享 jfce。
   - 调大 `ulimit -n`。
   - 建链放到异步线程池，参考 `UBSOCKET_ASYNC_ACCEPT_*`。
   - 流控初始信用不要给太多，避免大量空闲连接占着 RQE。
7. **内存池容量**
   - 初始大小要足够，否则初始化会失败。
   - 允许弹性扩容，并设置上限。
   - ubsocket 的估算公式：`((RX_DEPTH*4 + BUF_POOL_DEPTH) * (分片+128)) / 1MB`，开启 tiny pool 时再加上 tiny pool 的部分。
8. **系统层面**
   - 留意调度抖动、timerslack、中断亲和性。
   - 容器内要处理 EID 与 net namespace：`urma_admin add_eid --ns`。

---

## 11. 观测与问题定位

- **调用级打点**：给每个 URMA 调用记录耗时。
  - UMQ 用 `umq_perf_record_write`。
  - yuanrong 用 `[URMA_ELAPSED_POLL_JFC]`/`[URMA_ELAPSED_THREAD_SHED]` 日志，超过阈值才打印。
  - 单次 post、poll、wait 耗时异常时，先判断问题在 URMA 还是在 OS 调度。
- **链路级 trace**：ubsocket 提供 `UBSOCKET_SPLIT_TRACE_*`、`UBSOCKET_PROF_*`、`UBSOCKET_MONITOR_*`。yuanrong 提供 `enable_urma_perf`、`urma_perf_interval_ms` 和 trace 分析脚本。
- **URMA 自带工具**：
  - `urma_admin show_stats`/`show_res` 查看资源与统计。
  - `urma_ping` 检查连通性。
  - `urma_perftest` 做基线测试。
  - `urma_start_perf`/`urma_get_perf_info`/`urma_stop_perf` 做进程内性能采样。
- **常见现象与排查方向**

| 现象 | 优先排查 |
|---|---|
| `urma_get_device_by_name` 或 `urma_create_context` 失败 | 驱动未加载（ubcore/uburma/udma/ubagg）；`/usr/lib64/urma` 和 `/dev/ummu/tid` 权限；设备已被容器占用；eid_index 越界 |
| import_jetty 成功，但发送一直失败或超时 | 两端 multi_path 或传输模式不一致；RC 只有一端做了 bind；token 或 token_policy 不匹配；rjetty 只拷贝了半个（漏了 bonding 扩展） |
| 偶发秒级延迟 | RNR：接收端 RQE 补充不及时，或没有做流控；poll 线程被调度走；事件模式漏了 rearm |
| 事件模式下连接“卡死” | 某个 JFC 没有 rearm；wait 之后没有 ack；rearm 之后没有再 poll 一次 |
| 大块带宽远低于 perftest | 只用了单个 jetty 串行阻塞发送；在每次 IO 上注册内存；分块过小；用的是 ActiveBackup 而不是 Balance；跨 NUMA |
| 销毁 JFC 或 jetty 时卡住或失败 | 异常事件或 JFC 事件没有 ack；还有在途 WR 没有 flush；销毁顺序不对 |
| 进程内存持续上涨 | 导入的段或 tjetty 没有缓存，或者没有释放；orphan 连接没有清理；`get_rjetty` 后没有 `put_rjetty` |

---

## 12. 接入自检清单
1. 有编译开关和运行开关；缺少 liburma 或没有设备时能正常降级。
2. URMA 日志已导入业务日志，级别一致。
3. 设备名和 EID 可配置，并支持回退到其他候选；bonding 模式显式设置。
4. 所有 cfg 和 WR 都零初始化；队列深度、SGE、inline、分块大小都由 `dev_cap` 约束。
5. 建链时交换的是完整的 rjetty（按 len 拷贝），并做长度校验；RC 两端都做了 bind；建链后有探测。
6. token 随机生成，不写日志；段的 access 按最小权限配置。
7. 内存整块注册并缓存；导入的段和 tjetty 有缓存，并且有回收机制。
8. 双边通信有流控，RQE 批量补充。
9. poll 线程绑核；事件模式下 wait、ack、poll、rearm、再 poll 的顺序正确。
10. CR 的各种状态都有分支处理，迟到的 CQE 可以识别；错误日志限速。
11. 异常事件有独立线程，先 ack 再删除；故障 jetty 按 ERROR→flush→删除→补池的流程退休。
12. 销毁顺序正确，退出时没有泄漏。
13. 有 mock 或注入点，用 UT 覆盖建链失败、CQE 失败、异常事件、超时、降级这些路径。
14. 已用 `urma_perftest` 测出基线，并与业务实测做过对比。

---

## 13. 参考实现索引

| 主题 | ubs-comm | yuanrong-datasystem |
|---|---|---|
| 符号加载 | `src/ubsocket/csrc/under_api/urma/dl_urma_api.*`、`src/hcom/umq/src/umq_ub/core/private/umq_symbol_private.*` | `src/datasystem/common/rdma/urma_dlopen_util.*` |
| 初始化与日志 | `src/ubsocket/csrc/core/urma/urma_backend.cpp` | `urma_manager.cpp` `Init` |
| 设备、context 与 bonding 模式 | `umq_ub.c`（设置 ACTIVE_BACKUP、DISABLE_MSN、QUERY_PORT）、`urma_wrapper.cpp` `CreateContext` | `urma_manager.cpp` `GetUrmaDeviceName`、`urma_resource.cpp`（切到 BALANCE） |
| JFC/JFR/Jetty 配置 | `urma_wrapper.cpp` `Create*Cfg`、`umq_ub.c` `umq_create_jetty` | `urma_resource.cpp`（jfc/jfr/jetty cfg、SendJettyPool） |
| 建链 | `umq_ub.c` `umq_ub_rjetty_get`/`umq_ub_connect_jetty`、`urma_wrapper.cpp` `ImportRemoteJetty` | `urma_manager.cpp` `ProcessHandshakePeer`/`ImportRemoteJetty`、`docs/urma_call_flow.md` 第 3 节 |
| 内存注册 | `umq_ub.c` `urma_register_seg`/`import_mem` | `urma_manager.cpp` seg flag、`urma_resource.cpp` `RegisterSegment` |
| 数据面 | `umq_ub.c` post send/poll、`flow_control/umq_ub_flow_control.c` | `urma_manager.cpp` `UrmaWriteImpl`/`UrmaRead`/gather/NUMA |
| 完成与事件 | `umq_ub.c` wait/ack、`umq_ub_impl.c` rearm | `urma_manager.cpp` `PollJfcWait`/`CheckCompletionRecordStatus` |
| 异常与恢复 | `umq_ub_impl.c` async event、`umq_pro_ub.c` `umq_ub_flush_sqe`、`umq_modify_ubq_to_err` | `urma_async_event_handler.cpp`、`urma_resource.cpp` `RetireJetty`/`RefillLoop` |
| 参数说明 | `doc/ubsocket/UBSOCKET-USER-GUIDE.md` 第 3.2 节 | `common/flags/common_flag_define.cpp`、`docs/source_zh_cn/appendix/client_env_guide.md` |
| 测试与 mock | `src/hcom/umq/stub/urma`、`test/hcom/unit_test/transport/ub` | `tests/support/datasystem/common/urma_mock`、`tests/ut/common/urma_mock` |
