### 客户环境账号信息
ext.lihuafei1
uKonw3in8gxs_1

ext.xiajingze1
Huawei12#$

#### 登录堡垒机
ssh ext.xiajingze1@bastion.jd.com

Podip:
6.176.76.128
6.176.76.129
root
OGIzYzgyMjExM2I5MTUyNjljMWEyN2Uz

#### JD内部go源
http://artifactory.jd.com/artifactory/api/go/go_virtual

#### 容器镜像：
other-jd-openeuler24.03lts:

#### 镜像制作流程

### redis/mysql
#### redis测试命令：
redis-cli -h ap2.jd.local -p 5360 -a 'jim://3045132206070481179/6650' ping

#### Mysql创建表命令：
export DB_HOST=mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com
export DB_NAME=agentic_bidding 
export DB_USER=ads_model_agent
export DB_PASSWORD='NzdlZmMxZGE3MGY0YTkwZTc4ZDA5Zjdm'
export MIGRATOR_BIN=/home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/migrator
export MIGRATIONS_DIR=/home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/migrations
./run-migrations.sh up

#### Mysql确认库已创建命令：
mysql -h mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com -P 3306 -u ads_model_agent -p --table -e "SELECT VERSION() AS ver, CURRENT_USER() AS matched_account, USER() AS connected_as; SHOW GRANTS; SHOW DATABASES;"

#### seed-db生成凭证命令（注意邮箱 admin@e2b.dev 会影响teamsid生成）：
export POSTGRES_CONNECTION_STRING='ads_model_agent:NzdlZmMxZGE3MGY0YTkwZTc4ZDA5Zjdm@tcp(mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com:3306)/agentic_bidding'
chmod +x ./seed-db
printf '%s\n' 'admin@e2b.dev' | ./seed-db | tee user.txt

#### 生成的凭证信息：
Please enter the following values:
Email:
Seeding database with:
  Email: admin@e2b.dev
  Team ID: 81e81bf9-151f-475c-b684-f4d84ab212e6
  Access Token: sk_e2b_604b598a2270812e30b1b17ae443ee25db5e4022
  Team API Key: e2b_03ec1657fe0514e9a457200acf1cdcbde0bd36eb

Database seeded.

#### 在旧容器内导出postgresql模板数据：
./template-migrate.py export --template-id r0zjp61psosxt27uo221 --out tpl.json --pg-host 6.176.76.128 --pg-db mydatabase --pg-user postgres

#### 在新容器内导入mysql模板数据：
export MYSQL_PWD=NzdlZmMxZGE3MGY0YTkwZTc4ZDA5Zjdm
./template-migrate.py import --in tpl.json --team-id 81e81bf9-151f-475c-b684-f4d84ab212e6 --host mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com --db agentic_bidding --user ads_model_agent

#### 校验导入的模板数据：
python3 template-migrate.py verify \
    --in tpl.json \
    --team-id 81e81bf9-151f-475c-b684-f4d84ab212e6 \
    --host mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com --db agentic_bidding --user ads_model_agent


### 拉取服务
#### 1.编译：
/home/KASandbox/KASandbox-node-discovery-dev
./build.sh
./build.sh -f

#### 2.copy二进制文件
沙箱pod依赖的组件，需要创建放置目录：
mkdir -p /orchestrator/sandbox /orchestrator/template \
         /orchestrator/build /orchestrator/build-templates
mkdir -p /fc-vm /fc-kernels /fc-versions/v1.13.1 /fc-envd
mkdir -p /mnt/snapshot-cache /tmp/templates
cp /home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/api /usr/bin/api
cp /home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/orchestrator /usr/bin/orchestrator
cp /home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/client-proxy /usr/bin/client-proxy
cp -r /home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/vmlinux-* /fc-kernels/
cp /home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/firecracker /fc-versions/v1.13.1/firecracker
cp /home/KASandbox/KASandbox-node-discovery-dev/bin/arm64/envd /fc-envd/envd

#### 3.起好mock的agent-proxy
cd /home/KASandbox/KASandbox-node-discovery-dev/tests/discovery-stub
GOWORK=off go build -o discovery-stub .
vim config.example.json
{
  "instances": [
    {"id": "wbff1537-5908-1ac0-e251-516cff24e4ee", "ip": "6.176.76.129", "port": 5008}
  ]
}
GOWORK=off go run . -config config.example.json -addr :15080

#### 4.手动拉起所有服务
cd /root/rpmbuild/BUILD/e2b-infra-2026.09/no-nomad
修改start-api.sh
export ORCHESTRATOR_TYPE="custom" # ← mock 生效关键
export CUSTOM_URL="http://127.0.0.1:15080/instances"
./start-all.sh


e2b里redis代码修改（注释的是源代码，packages/shared/pkg/factories/redis.go）：
 77         case config.RedisURL != "":
 78                 // redisClient = redis.NewClient(&redis.Options{
 79                 //      Addr:         config.RedisURL,
 80                 //      MinIdleConns: 1,
 81                 // })
 82                 opts, err := redis.ParseURL(config.RedisURL)
 83                 if err != nil {
 84                         opts = &redis.Options{Addr: config.RedisURL}
 85                 }
 86                 opts.MinIdleConns = 1
 87                 redisClient = redis.NewClient(opts)

#### 直接通过restful接口查询命令
curl --request GET --url 'http://6.176.76.129:3000/v2/sandboxes?order=desc&limit=100' --header 'X-API-Key: e2b_03ec1657fe0514e9a457200acf1cdcbde0bd36eb' | jq

curl --request DELETE --url 'http://6.176.76.129:3000/sandboxes/iajzavk1i03ifzmuukouv' --header 'X-API-Key: e2b_03ec1657fe0514e9a457200acf1cdcbde0bd36eb' | jq

### 验证命令
创建沙箱并在沙箱执行命令
cd /home/wbf
python3 sandbox_shell.py

**需要先在沙箱内修改DNS**


在沙箱内调的是内网部署的一个 llm 服务：

curl -sS http://vobagent.llmserver.jd.local/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "Qwen3-14B","messages": [{"role":"user","content":"用一句话介绍京东"}],"max_tokens": 64,"temperature": 0.7,"top_p": 0.8,"chat_template_kwargs": {"enable_thinking": false}}'

预期 response：

{
  "id": "chatcmpl-904129d1c9bfec28",
  "object": "chat.completion",
  "created": 1789042501,
  "model": "Qwen3-14B",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "京东是一家以电子商务为主，业务涵盖零售、物流、金融、科技等领域的中国大型综合型互联网企业。",
        "refusal": null,
        "annotations": null,
        "audio": null,
        "function_call": null,
        "tool_calls": [],
        "reasoning": null,
        "reasoning_content": null
      },
      "logprobs": null,
      "finish_reason": "stop",
      "stop_reason": null,
      "token_ids": null
    }
  ],
  "service_tier": null,
  "system_fingerprint": null,
  "usage": {
    "prompt_tokens": 16,
    "total_tokens": 41,
    "completion_tokens": 25,
    "prompt_tokens_details": null
  },
  "prompt_logprobs": null,
  "prompt_token_ids": null,
  "kv_transfer_params": null
}

### 创建沙箱脚本
cd /home/wbf
python3 build_e2b.py


### 启动脚本

#### env.sh
[root@harnessruntime-25-00004 no-nomad]# cat env.sh
#!/usr/bin/env bash
# 共享静态配置（无 Nomad/Consul）。复用老容器 redis/postgres/harbor；无 MinIO/logs/otel/clickhouse。
#OLD_HOST_IP="<老容器IP>"          # 老容器（已部署 E2B）的 IP
REDIS_URL="redis://:jim%3A%2F%2F3045132206070481179%2F6650@ap2.jd.local:5360"     # 复用老容器 Redis（Go 当 host:port 解析）
REDIS_CLUSTER_URL=""
REDIS_TLS_CA_BASE64=""
DOMAIN_NAME=localhost
STORAGE_PROVIDER=Local            # 本地磁盘存储（无 MinIO）
LOCAL_TEMPLATE_STORAGE_BASE_PATH="/tmp/templates"   # 模板根文件系统目录（默认值，从老容器拷来）
ARTIFACTS_REGISTRY_PROVIDER=Local
MAX_STARTING_INSTANCES_PER_NODE=30
GIN_MODE=release
SHARED_CHUNK_CACHE_PATH="/mnt/snapshot-cache"
E2B_FC_NETNS_EXEC_HELPER=disabled

#### start-api.sh
cat start-api.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh
export ENVIRONMENT=dev              # 照抄 api.hcl（NOT local）
export NODE_ID="wbff1537-5908-1ac0-e251-516cff24e4ee"    # 原 node.unique.id（uuid），任意唯一串即可
export ORCHESTRATOR_TYPE="custom" # ← mock 生效关键
export CUSTOM_URL="http://127.0.0.1:15080/instances"
export ORCHESTRATOR_PORT=5008
export API_GRPC_PORT=5009
export ADMIN_TOKEN="dev_admin"
export SANDBOX_ACCESS_TOKEN_HASH_SEED="dadbb9c5-7c79-7bb1-f58c-e0fa61d49c57"
export POSTGRES_CONNECTION_STRING="ads_model_agent:NzdlZmMxZGE3MGY0YTkwZTc4ZDA5Zjdm@tcp(mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com:3306)/agentic_bidding"
export AUTH_DB_CONNECTION_STRING="ads_model_agent:NzdlZmMxZGE3MGY0YTkwZTc4ZDA5Zjdm@tcp(mysql-cn-north-1-0d172e202dfb4050.rds.jdcloud.com:3306)/agentic_bidding"
export SUPABASE_JWT_SECRETS=""
export LOKI_URL=127.0.0.1:3100
export REDIS_URL REDIS_CLUSTER_URL REDIS_TLS_CA_BASE64
export SANDBOX_STORAGE_BACKEND=redis
export TEMPLATE_BUCKET_NAME=skip
# 注意：不设 Nomad token/address（static 模式不需要）
exec /usr/bin/api --port 3000

#### start-edge.sh
[root@harnessruntime-25-00004 no-nomad]# cat start-edge.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh
export ENVIRONMENT=local
export NODE_ID="wbff1537-5908-1ac0-e251-516cff24e4ee"   # 原 node.unique.id
export NODE_IP="127.0.0.1"          # 原 attr.unique.network.ip-address
export HEALTH_PORT=3003             # 原 health 端口
export PROXY_PORT=3002              # 原 proxy 端口
export REDIS_URL REDIS_CLUSTER_URL REDIS_TLS_CA_BASE64
export API_GRPC_ADDRESS=localhost:5009
exec /usr/bin/client-proxy

#### start-orchestrator.sh
[root@harnessruntime-25-00004 no-nomad]# cat start-orchestrator.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh
export ENVIRONMENT=local          # local/dev 均可（env.IsDevelopment() 二者皆 true → StorageLocal 绕过 Consul KV）；选 local 对齐 IsLocal() 语义
export NODE_ID="wbff1537-5908-1ac0-e251-516cff24e4ee"      # 原 node.unique.name
export GRPC_PORT=5008
export PROXY_PORT=5007
export REDIS_URL REDIS_CLUSTER_URL REDIS_TLS_CA_BASE64
export ORCHESTRATOR_SERVICES="orchestrator"
export STORAGE_PROVIDER ARTIFACTS_REGISTRY_PROVIDER
export MAX_STARTING_INSTANCES_PER_NODE GIN_MODE
export DOMAIN_NAME SHARED_CHUNK_CACHE_PATH
# 注意：不设 Consul 令牌（ENVIRONMENT=local 下 StorageLocal 不走 Consul KV）
exec /usr/bin/orchestrator

#### start-all.sh
[root@harnessruntime-25-00004 no-nomad]# cat start-all.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p /var/log/e2b-no-nomad
./start-dnsmasq.sh
nohup ./start-orchestrator.sh > /var/log/e2b-no-nomad/orchestrator.log 2>&1 &
# 等 orchestrator 就绪（:5008）
for _ in $(seq 1 30); do curl -sf 127.0.0.1:5008/health >/dev/null 2>&1 && break; sleep 1; done
# template-manager 本轮 deferred，暂不启动
nohup ./start-edge.sh > /var/log/e2b-no-nomad/edge.log 2>&1 &
nohup ./start-api.sh > /var/log/e2b-no-nomad/api.log 2>&1 &
echo "started; logs in /var/log/e2b-no-nomad/"

#### stop-all.sh
[root@harnessruntime-25-00004 no-nomad]# cat stop-all.sh
#!/usr/bin/env bash
set -euo pipefail
pkill -f '/usr/bin/(orchestrator|template-manager|api|client-proxy)' || true


#### start-template-manager.sh
[root@harnessruntime-25-00004 no-nomad]# cat start-template-manager.sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"; source ./env.sh
export ENVIRONMENT=local
export NODE_ID="$(hostname)"
export ORCHESTRATOR_SERVICES="template-manager"
export API_SECRET="<旧容器 EDGE_API_SECRET>"
export GCP_DOCKER_REPOSITORY_NAME="<旧容器 HARBOR_HOST>"
export GRPC_PORT=5010             # 端口用 5010 而非 5008：host 网络下与 orchestrator(5008) 冲突
export STORAGE_PROVIDER ARTIFACTS_REGISTRY_PROVIDER
export MAX_STARTING_INSTANCES_PER_NODE GIN_MODE
export DOMAIN_NAME SHARED_CHUNK_CACHE_PATH
# 注意：不设 Consul 令牌；本轮 deferred，start-all.sh 默认不启动
exec /usr/bin/template-manager --port 5010
