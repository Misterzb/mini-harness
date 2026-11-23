"""RAG 知识检索包：确定性向量检索 + 引证式回答。"""
from .store import get_store, KnowledgeStore, Hit
from .embed import embed, cosine

__all__ = ["get_store", "KnowledgeStore", "Hit", "embed", "cosine"]
