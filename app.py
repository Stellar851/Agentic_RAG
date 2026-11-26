# -*- coding: utf-8 -*-
"""
================================================================================
RAG Project 微服务集群统一总控编排启动入口 (app.py)

【模块定位】
解决用户“单命令拉起全工程”的核心诉求：
仅需在项目根目录下执行 `python app.py`，即可一键并发唤起并协同管理三大解耦微服务：
  1. [Port 8001] 数据中台 (Data Platform)
  2. [Port 8002] 评测中台 (Evaluation Platform)
  3. [Port 8000] 业务智能体 (Agent Core Service)

【核心特性】
1. 统一日志聚合：在同一个终端窗口中输出三大服务的实时日志，自动附带各服务专属彩色前缀；
2. 优雅生命周期管理：捕获 Ctrl+C，一键联动关闭全部子进程，杜绝端口残留与孤儿进程；
3. 自动化健康就绪检查：启动完成后在控制台输出直观的访问面板；
4. 智能环境感知：自动检测并优先激活当前工程的 .venv 虚拟环境。
================================================================================
"""

import os
import sys
import time
import signal
import subprocess
import threading
import urllib.request
import json
import functools

# 强制开启全局标准输出无缓冲即时刷屏
os.environ["PYTHONUNBUFFERED"] = "1"
print = functools.partial(print, flush=True)

# 规范 Windows 终端 UTF-8 输出
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 智能探测 Python 解释器路径 (优先使用本地 .venv)
venv_python = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
if sys.platform != "win32":
    venv_python = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")

PYTHON_EXEC = venv_python if os.path.exists(venv_python) else sys.executable

# ANSI 终端颜色高亮
COLOR_RESET = "\033[0m"
COLOR_CYAN = "\033[36m"
COLOR_GREEN = "\033[32m"
COLOR_YELLOW = "\033[33m"
COLOR_MAGENTA = "\033[35m"
COLOR_BOLD = "\033[1m"

# 服务清单定义
SERVICES_CONFIG = [
    {
        "id": "data",
        "name": "Data-8001",
        "title": "数据中台 (Data Platform)",
        "port": 8001,
        "cwd": os.path.join(PROJECT_ROOT, "services", "data_platform"),
        "cmd": [PYTHON_EXEC, "-u", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"],
        "color": COLOR_GREEN,
        "health_url": "http://127.0.0.1:8001/api/v1/data/health",
        "web_url": "http://127.0.0.1:8001/"
    },
    {
        "id": "eval",
        "name": "Eval-8002",
        "title": "评测中台 (Evaluation Platform)",
        "port": 8002,
        "cwd": os.path.join(PROJECT_ROOT, "services", "eval_platform"),
        "cmd": [PYTHON_EXEC, "-u", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"],
        "color": COLOR_YELLOW,
        "health_url": "http://127.0.0.1:8002/api/eval/health",
        "web_url": "http://127.0.0.1:8002/"
    },
    {
        "id": "agent",
        "name": "Agent-8000",
        "title": "业务智能体 (Agent Core)",
        "port": 8000,
        "cwd": PROJECT_ROOT,
        "cmd": [PYTHON_EXEC, "-u", "-m", "uvicorn", "Agentic_RAG.app:app", "--host", "0.0.0.0", "--port", "8000"],
        "color": COLOR_CYAN,
        "health_url": "http://127.0.0.1:8000/api/sessions",
        "web_url": "http://127.0.0.1:8000/"
    }
]

processes = []
is_shutting_down = False


def log_stream_reader(pipe, prefix, color):
    """实时读取子进程标准输出并聚合至当前控制台"""
    try:
        for line in iter(pipe.readline, ''):
            if not line:
                break
            clean_line = line.rstrip('\r\n')
            if clean_line:
                print(f"{color}[{prefix}]{COLOR_RESET} {clean_line}")
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def print_banner():
    banner = f"""{COLOR_BOLD}{COLOR_CYAN}
================================================================================
🚀 [Orchestrator] 企业级解耦集群协同总控 (One-Command All Services)
📍 正在以单命令模式并发调度三大微服务集群:
   • [Port 8001] 数据中台:   知识库上传、24篇入库管理、切片穿透查看、双层漏斗精排
   • [Port 8002] 评测中台:   五阶段工业级基准评测流水线、自动化打分看板
   • [Port 8000] 业务智能体: 纯业务对话、ReAct 多跳工具调度、跨会话历史记忆
================================================================================{COLOR_RESET}
"""
    print(banner)


def check_ports_available():
    """检测 8000, 8001, 8002 端口是否可用"""
    import socket
    occupied = []
    for svc in SERVICES_CONFIG:
        port = svc["port"]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                occupied.append(port)
    return occupied


def shutdown_all_processes(signum=None, frame=None):
    """安全且优雅地关闭全部微服务子进程"""
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True

    print(f"\n{COLOR_BOLD}{COLOR_MAGENTA}🛑 正在关闭三大微服务集群...{COLOR_RESET}")

    for item in processes:
        proc = item["proc"]
        name = item["name"]
        try:
            if proc.poll() is None:
                # 优先优雅终止
                proc.terminate()
        except Exception:
            pass

    # 等待 2 秒超时后强制销毁
    time.sleep(1)
    for item in processes:
        proc = item["proc"]
        name = item["name"]
        try:
            if proc.poll() is None:
                proc.kill()
                print(f"   已安全清理子服务: {name}")
            else:
                print(f"   子服务已正常退出: {name}")
        except Exception:
            pass

    print(f"{COLOR_BOLD}{COLOR_GREEN}✅ 所有微服务已彻底关闭，端口已安全释放！感谢使用。{COLOR_RESET}\n")
    sys.exit(0)


def probe_health():
    """在后台静默轮询各服务探针并在就绪后打印引导面板"""
    time.sleep(3)
    ready_count = 0
    max_wait = 15
    start_t = time.time()

    while (time.time() - start_t) < max_wait and ready_count < len(SERVICES_CONFIG):
        for svc in SERVICES_CONFIG:
            if svc.get("_ready"):
                continue
            try:
                req = urllib.request.Request(svc["health_url"], headers={"User-Agent": "Orchestrator"})
                with urllib.request.urlopen(req, timeout=1) as res:
                    if res.status in (200, 204):
                        svc["_ready"] = True
                        ready_count += 1
            except Exception:
                pass
        time.sleep(0.8)

    # 打印最终就绪面板（区分全部就绪与部分未就绪）
    unready = [svc for svc in SERVICES_CONFIG if not svc.get("_ready")]
    print(f"\n{COLOR_BOLD}{COLOR_GREEN}================================================================================")
    if not unready:
        print("🎉 三大微服务集群协同初始化就绪！访问入口如下：")
    else:
        unready_desc = "、".join(f"{s['name']}(Port {s['port']})" for s in unready)
        print(f"⚠️ 微服务集群部分就绪（超时未响应: {unready_desc}），已就绪访问入口如下：")
    print("================================================================================")
    print(f" 🤖 业务智能体纯对话界面: {COLOR_CYAN}{SERVICES_CONFIG[2]['web_url']}{COLOR_GREEN}")
    print(f" 📚 数据中台专属管理大屏: {COLOR_YELLOW}{SERVICES_CONFIG[0]['web_url']}{COLOR_GREEN}")
    print(f" 🧪 工业级评测中枢看板:   {COLOR_MAGENTA}{SERVICES_CONFIG[1]['web_url']}{COLOR_GREEN}")
    print("--------------------------------------------------------------------------------")
    print(f" 💡 提示：在当前终端按 {COLOR_YELLOW}Ctrl + C{COLOR_GREEN} 即可一键安全停止所有微服务！")
    print(f"================================================================================{COLOR_RESET}\n")


def main():
    print_banner()

    # 检查端口占用
    occupied = check_ports_available()
    if occupied:
        print(f"{COLOR_YELLOW}⚠️  检测到以下端口已被占用: {occupied}。建议先关闭占用进程或稍等数秒释放。{COLOR_RESET}")

    # 注册退出信号处理
    signal.signal(signal.SIGINT, shutdown_all_processes)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown_all_processes)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_all_processes)

    # 并发拉起三大微服务
    for svc in SERVICES_CONFIG:
        print(f"🚀 正在拉起子服务: {svc['title']} (Port {svc['port']})...")
        try:
            proc = subprocess.Popen(
                svc["cmd"],
                cwd=svc["cwd"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )
            processes.append({"name": svc["name"], "proc": proc})

            # 启动独立线程聚合子进程的 stdout 日志
            t = threading.Thread(
                target=log_stream_reader,
                args=(proc.stdout, svc["name"], svc["color"]),
                daemon=True
            )
            t.start()
        except Exception as e:
            print(f"❌ 启动 {svc['name']} 失败: {e}")

    # 启动健康检查监控线程
    probe_thread = threading.Thread(target=probe_health, daemon=True)
    probe_thread.start()

    # 主线程维持常驻监听
    reported_exits = set()
    try:
        while True:
            time.sleep(1)
            # 检查是否有核心子进程异常退出（每个子进程仅告警一次，防止刷屏）
            for item in processes:
                code = item["proc"].poll()
                if code is not None and not is_shutting_down and item["name"] not in reported_exits:
                    reported_exits.add(item["name"])
                    print(f"\n⚠️ 子服务 [{item['name']}] 意外退出 (Exit Code: {code})")
    except KeyboardInterrupt:
        shutdown_all_processes()


if __name__ == "__main__":
    main()
