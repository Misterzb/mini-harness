"""部署后在服务器上跑：验证网关 key / IP 白名单 / 模型名 / JSON 输出全部可用。

用法（在服务器项目目录下）：
    .venv/bin/python deploy/verify_gateway.py

退出码 0 = 全部通过；1 = 有失败项。逐个打印每步原因，便于定位。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.llm import resolve_gateway

# 与运行时同一套网关推断逻辑：只填 DASHSCOPE_API_KEY 时自检百炼地址，
# 不再出现"拿百炼 key 自检 MaaS 地址"的假失败。
BASE, MODEL, FALLBACK = resolve_gateway()
KEY = (os.getenv("MINI_HARNESS_API_KEY", "").strip()
       or os.getenv("DASHSCOPE_API_KEY", "").strip())


async def main() -> int:
    print("=" * 66)
    print("MiniHarness 网关自检")
    print("=" * 66)
    print(f"  Base URL : {BASE}")
    print(f"  Key      : {KEY[:8] + '...' + KEY[-4:] if KEY else '(空)'}")
    print(f"  主模型   : {MODEL}   备用: {FALLBACK}")
    if not KEY:
        print("\n[FAIL] 没有配置 API key —— 当前只能用 mock，不是真实模型。")
        return 1

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=KEY, base_url=BASE)

    ok = True
    for model in (MODEL, FALLBACK):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "只输出 JSON，不要任何解释文字。"},
                    {"role": "user", "content": '输出 {"fact":"gmv","intent":"query"}'},
                ],
                temperature=0.2,
                max_tokens=80,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
            parsed = json.loads(text)  # 验证 JSON 模式真的生效（作品依赖结构化输出）
            usage = resp.usage
            print(f"\n[OK] {model}: {text[:90]}")
            print(f"     usage: prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
            print(f"     JSON 解析成功，字段={list(parsed.keys())}")
        except Exception as e:
            ok = False
            print(f"\n[FAIL] {model}: {type(e).__name__}: {str(e)[:240]}")
            if "403" in str(e) or "IP" in str(e):
                print("     -> IP 白名单问题：把【服务器公网 IP】加入网关 key 的允许列表。")
            elif "401" in str(e) or "Invalid token" in str(e):
                print("     -> key 错误：核对控制台复制的完整 key。")
            elif "404" in str(e) or "model" in str(e).lower():
                print("     -> 模型名不存在：改成控制台模型清单里有的名字。")

    print("\n" + "=" * 66)
    if ok:
        print("全部通过：网关可用，可以跑真实评测（eval/run_eval.py）。")
        print("提示：真实评测 500 条约消耗数万 token，先确认额度。")
    else:
        print("有失败项，按上面提示处理后再跑。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
