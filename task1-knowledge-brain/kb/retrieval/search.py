"""Hybrid search: keyword (BM25) and meaning (vectors), merged by rank.

Why both: vectors find "cancel my plan" when the article says "downgrade membership";
keywords find exact setting names like "Enable Lead Matching" that vectors blur.
Reciprocal Rank Fusion merges the two lists without having to calibrate their scores.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Literal

import aiosqlite
import numpy as np

from kb.progress import Progress, report
from kb.retrieval.embedder import Embedder, Vector
from kb.retrieval.rerank import Reranker
from kb.retrieval.rewrite import QueryRewriter

Mode = Literal["hybrid", "vector", "keyword"]

RRF_K = 60  # standard constant; dampens the gap between rank 1 and rank 2
CANDIDATES = 30  # how deep to look in each list before fusing
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it my of on or the to "
    "what when where which who why will with you your".split()
)


@dataclass
class SearchHit:
    chunk_id: int
    article_id: str
    source: str
    title: str
    url: str | None
    section: str | None
    text: str
    image_ids: list[str]
    score: float
    rerank_score: float | None = None  # cross-encoder relevance, when a reranker ran


def fts_query(question: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms joined by OR."""
    terms = [t for t in re.findall(r"\w+", question.lower()) if t not in STOPWORDS]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


class VectorIndex:
    """All chunk vectors in one in-memory matrix. ~8k x 384 floats = ~12 MB, and a
    brute-force dot product over it takes a few milliseconds, so no vector DB is needed."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self.ids: np.ndarray = np.zeros(0, dtype=np.int64)
        self.matrix: Vector = np.zeros((0, 0), dtype=np.float32)
        self._version: tuple[int, int] | None = None

    async def refresh(self, db: aiosqlite.Connection) -> None:
        models = await db.execute_fetchall(
            "SELECT DISTINCT embedding_model FROM chunks WHERE is_active=1 AND source='article'"
        )
        if len(models) > 1:
            raise RuntimeError(
                "Mixed active article embedding models; reindex before serving queries"
            )
        data = await db.execute_fetchall(
            "SELECT chunk_id, embedding FROM chunks WHERE embedding IS NOT NULL AND is_active=1 "
            "AND embedding_model=? AND embedding_dim=? ORDER BY chunk_id",
            (self.embedder.model_name, self.embedder.dim),
        )
        ids = [r["chunk_id"] for r in data]
        vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in data]
        self.ids = np.array(ids, dtype=np.int64)
        self.matrix = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)

    def top(self, query_vec: Vector, k: int) -> list[tuple[int, float]]:
        if self.matrix.size == 0:
            return []
        scores = self.matrix @ query_vec
        k = min(k, len(scores))
        best = np.argpartition(-scores, k - 1)[:k]
        best = best[np.argsort(-scores[best])]
        return [(int(self.ids[i]), float(scores[i])) for i in best]


CHANGELOG_TITLE = re.compile(r"^\s*changelog\b|software updates?\s*$", re.IGNORECASE)


def is_changelog(title: str) -> bool:
    """632 of 1,537 articles are release notes. They mention every feature in passing,
    so they crowd out the article that actually explains it."""
    return bool(CHANGELOG_TITLE.search(title))


class Searcher:
    def __init__(
        self,
        embedder: Embedder,
        keyword_weight: float = 0.3,
        # Tuned on the 42-question retrieval eval: x0.5 cut changelog passages in the
        # model's context from 6% to 2% with no recall loss; x0.25 broke questions that
        # really are about release notes.
        changelog_weight: float = 0.5,
        max_per_article: int | None = 2,
        rewriter: QueryRewriter | None = None,
        reranker: Reranker | None = None,
        rerank_depth: int = 20,
        rerank_chars: int | None = None,
        rewrite_skip_above: float | None = None,
    ) -> None:
        self.embedder = embedder
        # Optional: also search with the question rephrased in help-center vocabulary.
        self.rewriter = rewriter
        self.vectors = VectorIndex(embedder)
        # Keyword hits count less than meaning hits in the merge: the retrieval eval
        # showed plain OR-matching drags in noise for conversational questions.
        self.keyword_weight = keyword_weight
        # Release notes are still searchable ("what changed in March?") but rank lower.
        self.changelog_weight = changelog_weight
        # Cap passages per article so a few slots aren't spent on one article's chunks.
        self.max_per_article = max_per_article
        # Optional second stage: a cross-encoder re-orders the best `rerank_depth` passages.
        self.reranker = reranker
        self.rerank_depth = rerank_depth
        # Score only the start of each passage (title, section, first paragraphs): the
        # cross-encoder's cost grows with length, and the start carries most of the signal.
        self.rerank_chars = rerank_chars
        # Skip rule in front of the rewriter: when the question as asked already finds a
        # passage this similar, it is in help-center wording and a rewrite adds only cost
        # and latency. None = always rewrite. Counted in `rewrites` for the eval.
        self.rewrite_skip_above = rewrite_skip_above
        self.rewrites = {"used": 0, "skipped": 0}

    async def search(
        self,
        db: aiosqlite.Connection,
        question: str,
        k: int = 8,
        mode: Mode = "hybrid",
        progress: Progress | None = None,
        archived: bool = False,
    ) -> list[SearchHit]:
        """`archived=True` searches only the Archived KB (pages taken out of normal answers)."""
        queries = [question]
        vector_scores: dict[int, float] = {}
        query_vecs: dict[str, Vector] = {}
        if mode in ("hybrid", "vector"):
            await self.vectors.refresh(db)
            query_vecs[question] = self.embedder.embed_query(question)
        if self.rewriter is not None and mode == "hybrid":
            first = self.vectors.top(query_vecs[question], 1)
            if (
                self.rewrite_skip_above is not None
                and first
                and first[0][1] >= self.rewrite_skip_above
            ):
                self.rewrites["skipped"] += 1
                await report(progress, "check", "The question already matches the help center")
            else:
                self.rewrites["used"] += 1
                await report(progress, "think", "Rephrasing the question in help-center terms")
                queries += [
                    q
                    for q in await self.rewriter.rewrite(question)
                    if q.lower() != question.lower()
                ]
        # Fuse keyword + vector within each query, then keep each passage's BEST query.
        # Summing across queries let passages that were mediocre for several queries beat
        # the one that was clearly best for one rewrite ("Staging Mode coming soon page").
        best: dict[int, float] = {}
        total: dict[int, float] = {}
        for q in queries:
            ranked: list[tuple[float, list[int]]] = []
            if mode in ("hybrid", "vector"):
                qvec = query_vecs.get(q)
                hits = self.vectors.top(
                    qvec if qvec is not None else self.embedder.embed_query(q), CANDIDATES
                )
                for cid, score in hits:  # keep each passage's best similarity for the gate
                    vector_scores[cid] = max(score, vector_scores.get(cid, 0.0))
                ranked.append((1.0, [cid for cid, _ in hits]))
            if mode in ("hybrid", "keyword"):
                weight = self.keyword_weight if mode == "hybrid" else 1.0
                ranked.append((weight, await self._keyword(db, q)))
            per_query: dict[int, float] = {}
            for weight, ranking in ranked:
                for rank, cid in enumerate(ranking):
                    per_query[cid] = per_query.get(cid, 0.0) + weight / (RRF_K + rank + 1)
            for cid, s in per_query.items():
                best[cid] = max(s, best.get(cid, 0.0))
                total[cid] = total.get(cid, 0.0) + s
        # Best query decides; agreement across queries only breaks near-ties.
        fused = {cid: best[cid] + 0.1 * total[cid] for cid in best}
        hits_by_id = await self._load(db, list(fused), archived)
        for cid, loaded in hits_by_id.items():
            if loaded.source == "article" and is_changelog(loaded.title):
                fused[cid] *= self.changelog_weight
        order = [c for c in sorted(fused, key=lambda c: fused[c], reverse=True) if c in hits_by_id]
        if self.reranker is not None and order:
            head = order[: self.rerank_depth]
            await report(progress, "think", f"Re-ranking the best {len(head)} passages")
            scores = await asyncio.to_thread(
                self.reranker.score,
                question,
                [hits_by_id[c].text[: self.rerank_chars] for c in head],
            )
            rerank = dict(zip(head, scores, strict=True))
            for cid, s in rerank.items():
                hits_by_id[cid].rerank_score = s
            order = sorted(head, key=lambda c: rerank[c], reverse=True) + order[len(head) :]
        out: list[SearchHit] = []
        per_article: dict[str, int] = {}
        for cid in order:
            hit = hits_by_id.get(cid)
            if hit is None:
                continue
            if self.max_per_article and per_article.get(hit.article_id, 0) >= self.max_per_article:
                continue
            per_article[hit.article_id] = per_article.get(hit.article_id, 0) + 1
            # Report cosine similarity when we have it: it is interpretable (0..1),
            # unlike an RRF score, and the answer step uses it to judge evidence strength.
            hit.score = vector_scores.get(cid, 0.0) if mode != "keyword" else fused[cid]
            out.append(hit)
            if len(out) == k:
                break
        kinds = {"hybrid": "meaning + keywords", "vector": "meaning", "keyword": "keywords"}[mode]
        await report(
            progress,
            "search",
            f"Ran {len(queries)} search{'es' if len(queries) > 1 else ''} ({kinds})",
            " · ".join(f"“{q}”" for q in queries),
        )
        titles = list(dict.fromkeys(h.title for h in out))
        await report(
            progress,
            "doc",
            f"Found {len(out)} passage{'s' if len(out) != 1 else ''} in {len(titles)} article{'s' if len(titles) != 1 else ''}",
            " · ".join(titles[:5]) + (f" · +{len(titles) - 5} more" if len(titles) > 5 else ""),
        )
        return out

    async def _keyword(self, db: aiosqlite.Connection, question: str) -> list[int]:
        query = fts_query(question)
        if query is None:
            return []
        rows = await db.execute_fetchall(
            """SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?
               ORDER BY bm25(chunks_fts) LIMIT ?""",
            (query, CANDIDATES),
        )
        return [r["rowid"] for r in rows]

    async def _load(
        self, db: aiosqlite.Connection, ids: list[int], archived: bool = False
    ) -> dict[int, SearchHit]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await db.execute_fetchall(
            f"""SELECT c.chunk_id, c.article_id, c.source, c.section, c.text, c.image_ids,
                       a.title, a.url
                FROM chunks c LEFT JOIN articles a ON a.article_id = c.article_id
                LEFT JOIN pages p ON p.article_id = c.article_id
                WHERE c.chunk_id IN ({marks}) AND c.is_active = 1
                  -- Archived pages (status 'quarantined') keep their passages for the
                  -- Archived KB but are never cited; a duplicate is searched as its original.
                  AND {
                "p.status = 'quarantined'"
                if archived
                else "(p.status IS NULL OR p.status NOT IN ('quarantined', 'duplicate'))"
            }""",
            ids,
        )
        return {
            r["chunk_id"]: SearchHit(
                chunk_id=r["chunk_id"],
                article_id=r["article_id"],
                source=r["source"],
                title=r["title"]
                or (f"Expert answer: {r['section']}" if r["source"] == "expert" else ""),
                url=r["url"],
                section=r["section"],
                text=r["text"],
                image_ids=json.loads(r["image_ids"]),
                score=0.0,
            )
            for r in rows
        }
