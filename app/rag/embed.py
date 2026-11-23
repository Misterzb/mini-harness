"""确定性本地向量化：字符 n-gram 哈希投影到定长向量 + L2 归一化。

为什么不用外部 embedding API：
- 作品要能在无网络、无 Key 的面试现场开箱即跑；
- 评测要求可复现（同一问题每次得分一致），外部模型做不到；
- 这套哈希 embedding 是真实的稠密向量检索（余弦相似度），不是关键词 if/else，
  知识库 -1 换成正经 embedding 模型时上层接口不变。
"""
import hashlib
import math
import re

DIM = 256


def tokenize(text: str) -> list[str]:
    """中文按字 + unigram/bigram，英文/数字按词。"""
    text = re.sub(r"\s+", " ", (text or "").lower())
    tokens: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(list(seg))
        tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    tokens.extend(re.findall(r"[a-z0-9]+", text))
    return tokens


def embed(text: str, dim: int = DIM) -> list[float]:
    """字符 n-gram → 哈希桶计数 → L2 归一化。确定性，无随机、无网络。"""
    vec = [0.0] * dim
    for tok in tokenize(text):
        h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:4], "big")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
