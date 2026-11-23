"""关键词检索与混合融合的纯逻辑。

不碰 I/O 与 nonebot，便于用临时脚本单独验证。

中文不引入分词依赖：CJK 段按单字加二元组切分，英文数字按词切分。
召回靠二元组，精度靠 BM25 的 idf 与长度归一。

混合排序用加权 RRF（Reciprocal Rank Fusion）而不是分数线性相加：
向量距离与 BM25 分量纲不同，归一化后的线性相加会被单条极值带偏；
RRF 只看名次，权重语义干净，调用方给的 ``vector_weight`` 直接就是
“向量名次占多少话语权”。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

# BM25 词频饱和与长度归一参数，取常见默认值
BM25_K1 = 1.5
BM25_B = 0.75
# RRF 平滑常数。候选只取 top_k*overfetch（十几条量级），k 取 60 会把名次差
# 压得几乎没有，权重就翻不动顺序；20 保留头部区分度又不至于让单路垄断
RRF_K = 20
# 默认向量权重，关键词占其余部分
DEFAULT_VECTOR_WEIGHT = 0.6

_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def tokenize(text: str) -> list[str]:
    """切成检索用的词项。

    英文数字保留整词，CJK 段取二元组；只有单个汉字时才退成单字。
    单字全上会过度召回（“不”能命中所有带“不”的句子），二元组足以承担
    区分度。标点与空白不产生词项。
    """
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = _WORD_RE.findall(lowered)
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            # 单字保留：否则“猫”这类单词查询永远无召回
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def bm25_scores(query: str, documents: Sequence[str]) -> list[float]:
    """返回每条文档相对 ``query`` 的 BM25 分数，顺序与输入一致。

    空查询或空语料返回全零；单文档语料下 idf 退化为常数，分数仍可比。
    """
    if not documents:
        return []
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms:
        return [0.0] * len(documents)

    docs_tokens = [tokenize(doc or "") for doc in documents]
    total = len(docs_tokens)
    avg_len = sum(len(t) for t in docs_tokens) / total or 1.0
    doc_freq: Counter[str] = Counter()
    for tokens in docs_tokens:
        doc_freq.update(set(tokens))

    idf = {
        term: math.log(
            1.0 + (total - doc_freq.get(term, 0) + 0.5) / (doc_freq.get(term, 0) + 0.5)
        )
        for term in query_terms
    }

    scores: list[float] = []
    for tokens in docs_tokens:
        counts = Counter(tokens)
        length = len(tokens)
        score = 0.0
        for term in query_terms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            denom = freq + BM25_K1 * (1.0 - BM25_B + BM25_B * length / avg_len)
            score += idf[term] * freq * (BM25_K1 + 1.0) / denom
        scores.append(score)
    return scores


def normalize_scores(scores: Iterable[float]) -> list[float]:
    """按最大值压到 0..1；全零或负数按零处理，避免除零。"""
    values = [max(float(s), 0.0) for s in scores]
    peak = max(values, default=0.0)
    if peak <= 0.0:
        return [0.0] * len(values)
    return [s / peak for s in values]


def weighted_rrf(
    vector_ids: Sequence[str],
    keyword_ids: Sequence[str],
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    *,
    k: int = RRF_K,
) -> list[tuple[str, float]]:
    """合并两路名次，返回按融合分降序的 ``(id, score)``。

    只在一路出现的 id 在另一路贡献零分：它仍然可能排进前列，
    但天然低于两路同时命中的条目 —— 这正是混合检索想要的偏好。

    权重会夹到 0..1；``vector_weight=1`` 等价纯向量，``0`` 等价纯关键词。
    """
    weight = min(max(float(vector_weight), 0.0), 1.0)
    fused: dict[str, float] = {}
    for rank, doc_id in enumerate(vector_ids):
        fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + rank + 1)
    for rank, doc_id in enumerate(keyword_ids):
        fused[doc_id] = fused.get(doc_id, 0.0) + (1.0 - weight) / (k + rank + 1)
    return sorted(fused.items(), key=lambda item: item[1], reverse=True)


def rank_ids(scores: Sequence[float], ids: Sequence[str], top_k: int) -> list[str]:
    """按分数取前 ``top_k`` 个 id，同分时保持原顺序。"""
    order = sorted(range(len(ids)), key=lambda i: scores[i], reverse=True)
    return [ids[i] for i in order[: max(top_k, 0)]]


__all__ = [
    "BM25_B",
    "BM25_K1",
    "DEFAULT_VECTOR_WEIGHT",
    "RRF_K",
    "bm25_scores",
    "normalize_scores",
    "rank_ids",
    "tokenize",
    "weighted_rrf",
]
