"""pytest 公共 fixture：隔离库、mock LLM、不访问网络。"""
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def build_test_db():
    """每个测试会话重建一次账本，保证从 0 行开始，不依赖演示残留。"""
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_db.py")],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    yield


@pytest.fixture
def dbs():
    """每条测试用独立的 DuckDB 连接（测试进程单连接，不与外部服务冲突）。"""
    shop = duckdb.connect(str(ROOT / "data" / "shop.db"), read_only=True)
    led = duckdb.connect(str(ROOT / "data" / "ledger.db"))
    yield shop, led
    shop.close(); led.close()


@pytest.fixture
def mock_llm(monkeypatch):
    """强制 mock 模式，杜绝真实模型调用。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    from app.llm import LLM
    return LLM()
