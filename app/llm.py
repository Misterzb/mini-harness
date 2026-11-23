"""LLM 统一出口：OpenAI 兼容网关（默认 MaaS 中转，可切任意兼容服务） + 本地 mock 降级。
所有 LLM 调用只走这里 → usage 自动埋点 → Budget / 成本可追溯到每一步。
"""

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from .semantic.normalize import FACT_ALIASES, PLATFORM_ALIASES, CITY_ALIASES

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# 两套网关配置都保留（完整模板见 .env.example）：
#   新 MaaS 中转：填 MINI_HARNESS_API_KEY → MaaS 地址 + kimi-k2.6 备用
#   旧阿里云百炼：只填 DASHSCOPE_API_KEY → 自动切百炼兼容地址 + qwen-turbo 备用
# 显式设置的 BASE_URL / MODEL / FALLBACK 始终优先，可手动覆盖任意兼容网关。
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MAAS_URL = "https://maas-api.simple-server.cn/v1"


def resolve_gateway(env=None) -> tuple[str, str, str]:
    """按填入的 key 推断网关默认配置，避免拿百炼 key 打到 MaaS 地址上。"""
    e = os.environ if env is None else env
    new_key = (e.get("MINI_HARNESS_API_KEY") or "").strip()
    old_key = (e.get("DASHSCOPE_API_KEY") or "").strip()
    legacy = bool(old_key) and not new_key  # 只用老变量名 = 百炼配置
    base = (e.get("MINI_HARNESS_BASE_URL") or "").strip() or (BAILIAN_URL if legacy else MAAS_URL)
    model = (e.get("MINI_HARNESS_MODEL") or "").strip() or "qwen4-max"
    fallback = ((e.get("MINI_HARNESS_MODEL_FALLBACK") or "").strip()
                or ("qwen-turbo" if legacy else "kimi-k2.6"))
    return base, model, fallback


BASE_URL, MODEL, FALLBACK = resolve_gateway()


class LLM:
    def __init__(self):
        # 优先通用变量名，兼容旧的 DASHSCOPE_API_KEY（老 .env 无需改动）
        self.api_key = (os.getenv("MINI_HARNESS_API_KEY", "").strip()
                        or os.getenv("DASHSCOPE_API_KEY", "").strip())
        self.client = None
        if self.api_key:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=BASE_URL)
        self.usage_log: list[dict] = []
        self.mode = "api" if self.client else "mock"

    async def chat(self, messages: list[dict], *, temperature: float = 0.2,
                   max_tokens: int = 800, task_type: str = "query") -> tuple[str, dict]:
        if self.client:
            from openai import APIStatusError
            try:
                resp = await self.client.chat.completions.create(
                    model=MODEL, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, response_format={"type": "json_object"})
            except APIStatusError as e:
                # 鉴权/配额/请求错误(4xx)不该换模型重试 —— 换个模型一样失败，还掩盖真因。
                # 只对服务端错误(5xx)/限流(429)降级到备用模型。
                if not (e.status_code == 429 or e.status_code >= 500):
                    raise
                resp = await self.client.chat.completions.create(
                    model=FALLBACK, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, response_format={"type": "json_object"})
            text = resp.choices[0].message.content or "{}"
            usage = {"prompt": resp.usage.prompt_tokens,
                     "completion": resp.usage.completion_tokens, "model": resp.model}
        else:
            text = self._mock(messages, task_type)
            usage = {"prompt": len(str(messages)) // 3, "completion": len(text) // 3, "model": "local-mock"}
        self.usage_log.append(usage)
        return text, usage

    @staticmethod
    def _mock(messages: list[dict], task_type: str) -> str:
        """确定性 mock：按用户最后一句话返回结构化计划。演示开箱即用。

        与 Loop 共用 normalize.FACT_ALIASES，并把"上个月"写成解析后的 time_range，
        避免 mock 与运行时的口径分叉。
        """
        from .semantic.normalize import resolve_time, load_manifest, DIMENSION_ALIASES
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = str(m.get("content"))
                break

        # 编造指标：只放行白名单别名里的名字，其余原样输出 → 由 Verifier 拦截
        if "净利润率" in user or "利润" in user:
            return json.dumps({"fact": "净利润率", "intent": "query",
                               "dimensions": [], "filters": []}, ensure_ascii=False)
        if ("退款" in user or "退货" in user or "退钱" in user
                or re.search(r"退\s*\d+(?:\.\d+)?\s*元", user)):
            m = re.search(r"(\d+(?:\.\d+)?)\s*元", user)
            amount = float(m.group(1)) if m else 100.0
            om = re.search(r"O\d{6}|A\d+", user)
            return json.dumps({"tool": "propose_refund", "order_id": om.group(0) if om else "",
                               "amount": amount, "reason": "用户申请退款"}, ensure_ascii=False)

        # 指标：用统一别名表识别（口径与 Loop 一致）
        fact = "gmv"
        for alias, canonical in FACT_ALIASES.items():
            if alias in user:
                fact = canonical
                break

        # 时间：相对词优先；其次绝对月份 YYYY-MM / YYYY年M月 / YYYY年M月（锚定年份）。
        # 与运行时 constrain_utterance_time 共用 resolve_time，口径不分叉。
        tr = None
        for token in ("上上个月", "上个月", "上月", "这个月", "本月"):
            if token in user:
                tr = token
                break
        if not tr:
            m = re.search(r"(20\d{2})\s*[-年]\s*(\d{1,2})\s*月?", user)
            if m:
                tr = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"

        # 维度过滤：平台 / 城市（城市别名扩到全量，与库里 8 个城市对齐）
        filters = []
        for zh, code in PLATFORM_ALIASES.items():
            if zh in user:
                filters.append({"field": "platform", "value": code})
                break
        if not filters:
            for city in CITY_ALIASES:
                if city in user:
                    filters.append({"field": "city", "value": city})
                    break

        # 拆分维度：按X / 分X / 各X / X维度。类别若配比值型指标，交给 compiler 拒绝
        # （比值不可下推商品粒度是声明过的口径，不在规划层偷偷抹掉）。
        dims = []
        for zh, canonical in DIMENSION_ALIASES.items():
            if any(pat in user for pat in (f"按{zh}", f"分{zh}", f"各{zh}", f"按{zh}分布")):
                if canonical not in dims:
                    dims.append(canonical)

        if "诊断" in user or "下降" in user or "为什么" in user or "原因" in user:
            plan = {"fact": fact, "intent": "diagnose", "dimensions": dims or ["platform"], "filters": filters}
        else:
            plan = {"fact": fact, "intent": "query", "dimensions": dims, "filters": filters}
        if tr:
            plan["time_range"] = tr
        return json.dumps(plan, ensure_ascii=False)
