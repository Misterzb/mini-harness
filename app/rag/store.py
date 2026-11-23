"""轻量向量检索库：确定性 embedding + 余弦相似度 + 出处追溯。

设计边界（诚实）：这是"检索增强"的最小可用实现——用字符 n-gram 哈希向量
做余弦检索，答案只拼接命中文档原文并标出处，不调用 LLM 自由生成。
它演示的是 RAG 的检索/融合/引证/阈值拒答骨架，不是生产级向量库
（没有 HNSW、没有重排模型、没有 chunk 切分流水线）。接口预留：
换 embedding/向量库时只改 embed() 与 search()，上层工具不动。
"""
import json
from dataclasses import dataclass
from pathlib import Path

from .embed import embed, cosine

KB_PATH = Path(__file__).with_name("knowledge.json")


@dataclass
class Hit:
    id: str
    title: str
    text: str
    tags: list[str]
    score: float

    def cite(self) -> str:
        return f"[{self.id} {self.title}]"


class KnowledgeStore:
    def __init__(self, path: Path = KB_PATH):
        self.docs = json.loads(path.read_text(encoding="utf-8"))
        # 字段分开建向量：标题/标签是精确专名，正文是宽泛描述。
        # 评分时标题+标签权重更高，避免正文里高频通用词（如"口径"）压过专名（如"GMV"）。
        self._vectors = [
            (doc,
             embed(doc["title"] + " " + " ".join(doc["tags"])),   # 精确字段
             embed(doc["text"]))                                   # 正文
            for doc in self.docs
        ]

    def search(self, query: str, top_k: int = 3, min_score: float = 0.28) -> list[Hit]:
        """余弦检索 top_k；低于阈值的片段丢弃（没检索到依据就不许答）。

        余弦是主信号；标题/标签里的精确词命中做小幅加权（确定性重排），
        解决"GMV 是什么口径"里'口径'通用词压过'GMV'专名的问题。
        """
        from .embed import tokenize
        qv = embed(query)
        # 只用多字词（gmv/退款/品类…）做精确命中，单字（口/径/是）区分力太低会误导
        qwords = {t for t in tokenize(query) if len(t) >= 2}
        reranked = []
        for doc, precise_vec, body_vec in self._vectors:
            # 标题/标签 0.65 + 正文 0.35：专名优先，正文兜底
            s = 0.65 * cosine(qv, precise_vec) + 0.35 * cosine(qv, body_vec)
            title_tags = {t for t in tokenize(doc["title"] + " " + " ".join(doc["tags"]))
                          if len(t) >= 2}
            overlap = len(qwords & title_tags)
            s += 0.10 * overlap          # 专名命中加分，幅度小于余弦主信号
            if s < min_score:            # 全部分算完再卡阈值：专名分要算进去
                continue
            reranked.append((s, doc))
        reranked.sort(key=lambda x: -x[0])
        return [Hit(id=d["id"], title=d["title"], text=d["text"],
                    tags=d["tags"], score=round(s, 4)) for s, d in reranked[:top_k]]


_STORE: KnowledgeStore | None = None


def get_store() -> KnowledgeStore:
    global _STORE
    if _STORE is None:
        _STORE = KnowledgeStore()
    return _STORE
