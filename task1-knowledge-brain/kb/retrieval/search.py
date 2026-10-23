"""Hybrid search: keyword (BM25) and meaning (vectors), merged by rank.

Why both: vectors find "cancel my plan" when the article says "downgrade membership";
keywords find exact setting names like "Enable Lead Matching" that vectors blur.
Reciprocal Rank Fusion merges the two lists without having to calibrate their scores.
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

import aiosqlite
import numpy as np

from kb.retrieval.embedder import Embedder, Vector
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


def fts_query(question: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms joined by OR."""
    terms = [t for t in re.findall(r"\w+", question.lower()) if t not in STOPWORDS]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))


class VectorIndex:
    """All chunk vectors in one in-memory matrix. ~8k x 384 floats = ~12 MB, and a
    brute-force dot product over it takes a few milliseconds, so no vector DB is needed."""

    def __init__(self) -> None:
        self.ids: np.ndarray = np.zeros(0, dtype=np.int64)
        self.matrix: Vector = np.zeros((0, 0), dtype=np.float32)
        self._version: tuple[int, int] | None = None

    async def refresh(self, db: aiosqlite.Connection) -> None:
        rows = list(
            await db.execute_fetchall("SELECT count(*) AS n, max(chunk_id) AS m FROM chunks")
        )
        version = (rows[0]["n"], rows[0]["m"] or 0)
        if version == self._version:
            return
        data = await db.execute_fetchall(
            "SELECT chunk_id, embedding FROM chunks WHERE embedding IS NOT NULL ORDER BY chunk_id"
        )
        ids = [r["chunk_id"] for r in data]
        vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in data]
        self.ids = np.array(ids, dtype=np.int64)
        self.matrix = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)
        self._version = version

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
    ) -> None:
        self.embedder = embedder
        # Optional: also search with the question rephrased in help-center vocabulary.
        self.rewriter = rewriter
        self.vectors = VectorIndex()
        # Keyword hits count less than meaning hits in the merge: the retrieval eval
        # showed plain OR-matching drags in noise for conversational questions.
        self.keyword_weight = keyword_weight
        # Release notes are still searchable ("what changed in March?") but rank lower.
        self.changelog_weight = changelog_weight
        # Cap passages per article so a few slots aren't spent on one article's chunks.
        self.max_per_article = max_per_article

    async def search(
        self, db: aiosqlite.Connection, question: str, k: int = 8, mode: Mode = "hybrid"
    ) -> list[SearchHit]:
        queries = [question]
        if self.rewriter is not None and mode == "hybrid":
            queries += [
                q for q in await self.rewriter.rewrite(question) if q.lower() != question.lower()
            ]
        vector_scores: dict[int, float] = {}
        if mode in ("hybrid", "vector"):
            await self.vectors.refresh(db)
        # Fuse keyword + vector within each query, then keep each passage's BEST query.
        # Summing across queries let passages that were mediocre for several queries beat
        # the one that was clearly best for one rewrite ("Staging Mode coming soon page").
        best: dict[int, float] = {}
        total: dict[int, float] = {}
        for q in queries:
            ranked: list[tuple[float, list[int]]] = []
            if mode in ("hybrid", "vector"):
                hits = self.vectors.top(self.embedder.embed_query(q), CANDIDATES)
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
        hits_by_id = await self._load(db, list(fused))
        for cid, loaded in hits_by_id.items():
            if loaded.source == "article" and is_changelog(loaded.title):
                fused[cid] *= self.changelog_weight
        out: list[SearchHit] = []
        per_article: dict[str, int] = {}
        for cid in sorted(fused, key=lambda c: fused[c], reverse=True):
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

    async def _load(self, db: aiosqlite.Connection, ids: list[int]) -> dict[int, SearchHit]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await db.execute_fetchall(
            f"""SELECT c.chunk_id, c.article_id, c.source, c.section, c.text, c.image_ids,
                       a.title, a.url
                FROM chunks c LEFT JOIN articles a ON a.article_id = c.article_id
                LEFT JOIN pages p ON p.article_id = c.article_id
                WHERE c.chunk_id IN ({marks})
                  -- Quarantined URLs keep their passages but are never cited.
                  AND (p.status IS NULL OR p.status != 'quarantined')""",
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
