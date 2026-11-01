"""检索：query 向量化后到 Chroma 取 top-k 片段。"""
from app.config import settings
from app.services import vectorstore
from app.services.embeddings import embed_one


def retrieve(kb_id: str, query: str, top_k: int | None = None, doc_id: str | None = None) -> list[dict]:
    k = top_k or settings.top_k
    vector = embed_one(query)
    return vectorstore.search(kb_id, vector, top_k=k, doc_id=doc_id)
