import os
import json
from e2b import Template, default_build_logger, wait_for_port
from e2b  import Sandbox
# 设置E2B环境变量，IP修改为本地IP
os.environ["E2B_API_URL"] = "http://6.176.76.128:3000"
os.environ["E2B_HTTP_SSL"] = "false"
config_path = "/root/.e2b/config.json"
if __name__ == '__main__':
    print(f"读取配置文件路径：{config_path}")

    # 初始化变量，避免异常时变量未定义
    access_token = None
    team_api_key = None
        # 1. 打开并读取文件内容
    with open(config_path, "r", encoding="utf-8") as f:
        # 2. 解析JSON内容为Python字典
        data = json.load(f)
    # 3. 提取目标字段（使用get方法避免键不存在报错）
    access_token = data.get("accessToken")
    team_api_key = data.get("teamApiKey")
    # 4. 输出结果
    print("提取结果：")
    print(f"accessToken: {access_token}")
    print(f"teamApiKey: {team_api_key}")
    # 验证字段是否存在
    if not access_token or not team_api_key:
        print("警告：文件中未找到accessToken或teamApiKey字段！")
        # 字段缺失时直接退出，避免后续执行失败
        exit(1)

    # 设置E2B相关环境变量
    os.environ["E2B_ACCESS_TOKEN"] = access_token
    os.environ["E2B_API_KEY"] = team_api_key

    # 构建E2B模板
    print("开始构建E2B模板...")
    Template.build(
        #Template().from_dockerfile('FROM harbor:443/e2b-orchestration/openclaw-openviking:custom') # Nginx 反向代理了443端口
        #.set_start_cmd("sudo websocat -b --exit-on-eof ws-l:0.0.0.0:8081 tcp:127.0.0.1:22", wait_for_port(8081)), # websocat在沙箱内8081端口启动
        #Template().from_dockerfile('FROM harbor:443/e2b-orchestration/openclaw-openviking:custom'),
        #Template().from_dockerfile('FROM harbor:443/e2b-orchestration/ubuntu:22.04-nfs-ready'),

        #Template().from_image('10.50.156.192:2900/e2b-orchestration/swesmith.arm64.ls1intum_1776_hephaestus-884:1.0'),
        Template().from_image('6.176.76.128:2900/e2b-orchestration/ubuntu:22.04-custom'),
        #Template().from_dockerfile('/mnt/sdb/wbf/lslintum_dockerfile'),
        alias="wbf_test", # 模板别名
        cpu_count=2,
        memory_mb=2048,
        on_build_logs=default_build_logger(),
        skip_cache=True
    )
    print("模板构建完成！")
    # 沙箱名称与模板名一致
    #sbx = Sandbox.create("openclaw")
    #print("沙箱启动完成！")
    #print("沙箱ID: ", sbx.sandbox_id) #输出沙箱id
    #print(sbx.commands.run("whoami"))  # guest
    ##print(sbx.commands.run("ss -tlnp | grep :22", user="root"))
