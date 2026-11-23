"""一键安装 MCP 依赖到隔离目录 .mcp_lib2（避免污染全局环境 / 版本冲突）。

运行：python scripts/setup_mcp.py
说明：mcp 1.9.4 依赖旧版 pydantic 内部 API，故锁定 pydantic==2.10.6 一起装。
若 .mcp_lib2 已存在可跳过（脚本检测）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".mcp_lib2"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def main():
    if (TARGET / "mcp").exists():
        print(f"[setup_mcp] 已安装: {TARGET}")
        return
    print(f"[setup_mcp] 安装 mcp 1.9.4 + pydantic 2.10.6 → {TARGET}")
    cmd = [sys.executable, "-m", "pip", "install",
           "mcp==1.9.4", "pydantic==2.10.6",
           "--target", str(TARGET), "--no-cache-dir", "-i", MIRROR]
    subprocess.run(cmd, check=True)
    print("[setup_mcp] 完成。运行 mcp_server/test_client.py 验证。")


if __name__ == "__main__":
    main()
