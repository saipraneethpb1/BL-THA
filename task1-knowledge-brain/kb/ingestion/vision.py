"""Describe help-center screenshots with a vision model so their content is searchable.

- Each image URL is described once. The same screenshot reused in several articles,
  or an article re-crawled after an edit, reuses the stored description.
- The model sees the image plus the article text around it, but is told to describe
  only what is visible, and to leave out labels it cannot read.
- A failed image is marked and skipped, so one broken URL never stops the job.
"""

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import aiosqlite
import anthropic
import httpx
from anthropic.types import Base64ImageSourceParam, URLImageSourceParam
from pydantic import BaseModel, Field

from kb import claude
from kb.answer.llm import price
from kb.ingestion.sources import HostGuard, SitemapError, guarded_client

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
# What the Messages API accepts inline (Bedrock takes no image URLs, so we send the bytes).
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5_000_000


class ImageDescription(BaseModel):
    kind: Literal["screenshot", "diagram", "photo", "decorative", "other"]
    description: str = Field(description="Up to 60 words")
    # No hard limit in the schema: a model listing 14 labels instead of 12 used to make the
    # whole description fail validation (found by the vision eval). Extra labels are dropped.
    ui_labels: list[str] = Field(default_factory=list)
    legible: bool = True

    def as_text(self) -> str:
        """The text stored and inlined into chunks. Empty for decorative images."""
        if self.kind == "decorative":
            return ""
        text = self.description.strip()
        if self.ui_labels:
            text += " Visible labels: " + "; ".join(self.ui_labels[:12]) + "."
        if not self.legible:
            text += " (Parts of this image were not legible.)"
        return text


@dataclass(frozen=True)
class ImageJob:
    src: str
    title: str
    section: str | None
    context_before: str
    context_after: str


@dataclass
class DescribeResult:
    description: ImageDescription
    input_tokens: int
    output_tokens: int


class ImageDescriber(Protocol):
    model: str

    async def describe(self, job: ImageJob) -> DescribeResult: ...


class ClaudeImageDescriber:
    def __init__(self, model: str, prompt_version: str = "vision_v1") -> None:
        self.client = claude.client(max_retries=3, timeout=60.0)
        self.model = model
        self.system = (PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")
        self.guard = HostGuard()

    async def image_source(self, src: str) -> Base64ImageSourceParam | URLImageSourceParam:
        """Anthropic's API fetches the URL itself; Bedrock needs the image inline. The image
        URL comes from a crawled page, so the download is guarded like any other fetch."""
        if claude.provider() == "anthropic":
            return {"type": "url", "url": src}
        try:
            async with (
                guarded_client(self.guard, timeout=20.0) as http,
                http.stream("GET", src, follow_redirects=True) as resp,
            ):
                if resp.status_code != 200:
                    raise ValueError(f"image download answered HTTP {resp.status_code}")
                media_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if media_type not in IMAGE_TYPES:
                    raise ValueError(f"unsupported image type {media_type or 'unknown'}")
                data = b""
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError("image is larger than 5 MB")
        except (httpx.HTTPError, SitemapError) as exc:
            raise ValueError(f"image download failed: {exc}") from exc
        return {
            "type": "base64",
            "media_type": media_type,  # checked against IMAGE_TYPES
            "data": base64.b64encode(data).decode("ascii"),
        }

    async def describe(self, job: ImageJob) -> DescribeResult:
        context = (
            f"Article: {job.title}\nSection: {job.section or '-'}\n"
            f"Text before the image:\n{job.context_before or '-'}\n\n"
            f"Text after the image:\n{job.context_after or '-'}"
        )
        response = await self.client.messages.parse(
            model=claude.model_id(self.model),
            max_tokens=1000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": await self.image_source(job.src)},
                        {"type": "text", "text": context},
                    ],
                }
            ],
            output_config=claude.output_config(self.model, "low"),
            output_format=ImageDescription,
        )
        if response.parsed_output is None:
            raise ValueError(f"no description returned (stop_reason={response.stop_reason})")
        return DescribeResult(
            response.parsed_output, response.usage.input_tokens, response.usage.output_tokens
        )


@dataclass
class VisionReport:
    described: int = 0
    reused: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        price_in, price_out = price(self.model)
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000


async def describe_pending_images(
    db: aiosqlite.Connection,
    describer: ImageDescriber,
    *,
    limit: int | None = None,
    concurrency: int = 4,
    retry_failed: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> VisionReport:
    """`should_stop`: checked before each screenshot; one already being described finishes."""
    report = VisionReport(model=describer.model)

    # 1. Reuse: copy descriptions to rows whose image URL was already described elsewhere.
    cur = await db.execute(
        """UPDATE images SET
             description = (SELECT d.description FROM images d WHERE d.src = images.src
                            AND d.described_with NOT LIKE 'failed:%' LIMIT 1),
             described_with = (SELECT d.described_with FROM images d WHERE d.src = images.src
                               AND d.described_with NOT LIKE 'failed:%' LIMIT 1)
           WHERE described_with IS NULL AND EXISTS (
             SELECT 1 FROM images d WHERE d.src = images.src
             AND d.described_with IS NOT NULL AND d.described_with NOT LIKE 'failed:%')"""
    )
    report.reused = cur.rowcount
    await db.commit()

    # 2. Describe each remaining distinct URL once, with its first article's context.
    where = "described_with IS NULL"
    if retry_failed:
        where = "(described_with IS NULL OR described_with LIKE 'failed:%')"
    sql = f"""SELECT i.src, a.title, i.section, i.context_before, i.context_after
              FROM images i JOIN articles a ON a.article_id = i.article_id
              WHERE {where} GROUP BY i.src ORDER BY min(i.rowid)"""
    params: list[object] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    jobs = [
        ImageJob(
            r["src"], r["title"], r["section"], r["context_before"] or "", r["context_after"] or ""
        )
        for r in await db.execute_fetchall(sql, params)
    ]
    log.info("vision: %d images to describe, %d reused", len(jobs), report.reused)

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def run(job: ImageJob) -> None:
        async with sem:
            if should_stop is not None and should_stop():
                return  # left undescribed: picked up by the next refresh
            try:
                result = await describer.describe(job)
            except (anthropic.APIError, ValueError) as exc:
                async with lock:
                    report.failed += 1
                    await db.execute(
                        "UPDATE images SET described_with=? WHERE src=?",
                        (f"failed:{describer.model}:{type(exc).__name__}", job.src),
                    )
                    await db.commit()
                log.warning("vision failed for %s: %s", job.src, exc)
                return
        async with lock:
            report.described += 1
            report.input_tokens += result.input_tokens
            report.output_tokens += result.output_tokens
            await db.execute(
                "UPDATE images SET description=?, described_with=? WHERE src=?",
                (result.description.as_text(), describer.model, job.src),
            )
            await db.commit()

    await asyncio.gather(*(run(j) for j in jobs))
    return report


def main() -> None:
    import argparse

    from kb.config import settings
    from kb.db import connect, init_db

    parser = argparse.ArgumentParser(description="Describe help-center screenshots.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default=settings.vision_model)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    if reason := claude.missing():
        raise SystemExit(reason)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def go() -> VisionReport:
        await init_db(settings.db_path)
        async with connect(settings.db_path) as db:
            return await describe_pending_images(
                db,
                ClaudeImageDescriber(args.model),
                limit=args.limit,
                retry_failed=args.retry_failed,
            )

    report = asyncio.run(go())
    print(report, f"cost ${report.cost_usd:.4f}")


if __name__ == "__main__":
    main()
