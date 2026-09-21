#!/usr/bin/env python3
"""通过 PTY 以类似 SSH 的方式登录 E2B 沙箱，进入交互式 shell。
前提：模板别名已存在（可用 Template().from_image(IMAGE) 预先构建）。
本脚本不会在沙箱内安装任何东西。
用法：
    python3 sandbox_shell.py
    python3 sandbox_shell.py --template wbf_test --user root --cwd /root
退出：按 Ctrl-] 本地退出，或在远端 shell 里输入 exit。
"""
import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Optional
from e2b import AsyncSandbox
from e2b.sandbox.commands.command_handle import CommandExitException, PtySize
try:
    import termios
    import tty
except ImportError:  # 非 POSIX 平台
    termios = None
    tty = None
# 已存在的模板别名（由 from_image 预先构建）
TEMPLATE_ALIAS = "wbf_test"
# 仅作记录：构建该别名时使用的镜像
IMAGE = ""
DEFAULT_API_URL = "http://6.176.76.129:3000"
CONFIG_PATH = os.path.expanduser("/home/wbf/debug_config129.json")
# 本地退出快捷键 Ctrl-]
LOCAL_ESCAPE = b"\x1d"
SANDBOX_TIMEOUT = 3600
def load_config(path: str) -> None:
    """读取 ~/.e2b/config.json 并设置 E2B 相关环境变量。"""
    os.environ.setdefault("E2B_API_URL", DEFAULT_API_URL)
    os.environ.setdefault("E2B_HTTP_SSL", "false")
    os.environ.setdefault("E2B_SANDBOX_URL", "http://6.176.76.129:3002")

    if os.path.exists(path):
        print(f"读取配置文件：{path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        access_token = data.get("accessToken")
        team_api_key = data.get("teamApiKey")
        if access_token:
            os.environ["E2B_ACCESS_TOKEN"] = access_token
        if team_api_key:
            os.environ["E2B_API_KEY"] = team_api_key
    # 兼容被 Markdown 方括号包裹的 URL，例如 "[[http://10.50.156.192:3000](http://10.50.156.192:3000)]"
    url = os.environ.get("E2B_API_URL", "")
    if url.startswith("[") and url.endswith("]"):
        os.environ["E2B_API_URL"] = url[1:-1]
    if not os.environ.get("E2B_API_KEY"):
        raise SystemExit("未找到 E2B_API_KEY / teamApiKey，请检查配置文件或环境变量")
    print(f"E2B_API_URL = {os.environ['E2B_API_URL']}")
def get_terminal_size() -> PtySize:
    try:
        size = os.get_terminal_size(sys.stdin.fileno())
        return PtySize(rows=size.lines, cols=size.columns)
    except OSError:
        return PtySize(rows=24, cols=80)
def write_stdout(data: bytes) -> None:
    out = sys.stdout.buffer
    out.write(data)
    out.flush()
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="以交互式 shell 登录 E2B 沙箱（类似 SSH）"
    )
    parser.add_argument("--template", default=TEMPLATE_ALIAS, help="已存在的模板别名")
    parser.add_argument("--user", default=None, help="沙箱内登录用户，默认使用模板用户")
    parser.add_argument("--cwd", default=None, help="登录后的工作目录")
    parser.add_argument(
        "--timeout",
        type=int,
        default=SANDBOX_TIMEOUT,
        help="沙箱存活时间（秒）",
    )
    return parser.parse_args()
async def run_shell(
    template: str,
    user: Optional[str],
    cwd: Optional[str],
    timeout: int,
) -> None:
    print(f"正在创建沙箱（模板别名: {template}）...")
    sbx = await AsyncSandbox.create(template, timeout=timeout)
    print(f"沙箱已启动，ID: {sbx.sandbox_id}")
    print("提示：按 Ctrl-] 本地退出；在远端 shell 输入 exit 亦可退出。\n")
    stdin_fd = sys.stdin.fileno()
    is_tty = sys.stdin.isatty() and termios is not None
    if not is_tty:
        print("警告：当前 stdin 不是终端，仅能被动接收输出（Ctrl-] 仍可退出）。\n")
    size = get_terminal_size()
    # timeout=0 表示不限制 PTY 连接时长
    terminal = await sbx.pty.create(
        size,
        on_data=write_stdout,
        user=user,
        cwd=cwd,
        timeout=0,
    )
    loop = asyncio.get_running_loop()
    stdin_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
    quit_event = asyncio.Event()
    old_term_attrs = None
    def on_stdin_ready() -> None:
        try:
            data = os.read(stdin_fd, 4096)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(stdin_fd)
        stdin_queue.put_nowait(data)
    if is_tty:
        assert termios is not None and tty is not None
        loop.add_reader(stdin_fd, on_stdin_ready)
        old_term_attrs = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
        def on_resize() -> None:
            asyncio.ensure_future(sbx.pty.resize(terminal.pid, get_terminal_size()))
        if hasattr(signal, "SIGWINCH"):
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
    async def forward_input() -> None:
        while not quit_event.is_set():
            data = await stdin_queue.get()
            if not data:
                quit_event.set()
                return
            if LOCAL_ESCAPE in data:
                quit_event.set()
                return
            await sbx.pty.send_stdin(terminal.pid, data)
    waiter = asyncio.ensure_future(terminal.wait())
    input_task = asyncio.ensure_future(forward_input())
    try:
        done, _ = await asyncio.wait(
            {waiter, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    except asyncio.CancelledError:
        pass
    except CommandExitException as e:
        print(
            f"\n远端 shell 退出，exit_code={e.exit_code}",
            file=sys.stderr,
        )
    finally:
        quit_event.set()
        for task in (input_task, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(input_task, waiter, return_exceptions=True)
        if is_tty:
            assert termios is not None
            if hasattr(signal, "SIGWINCH"):
                loop.remove_signal_handler(signal.SIGWINCH)
            try:
                loop.remove_reader(stdin_fd)
            except Exception:
                pass
            if old_term_attrs is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term_attrs)
        try:
            await sbx.pty.kill(terminal.pid)
        except Exception:
            pass
        await sbx.kill()
        print("\n沙箱已关闭。")
def main() -> None:
    args = parse_args()
    load_config(CONFIG_PATH)
    try:
        asyncio.run(
            run_shell(args.template, args.user, args.cwd, args.timeout)
        )
    except KeyboardInterrupt:
        print("\n已中断。")
if __name__ == "__main__":
    main()
