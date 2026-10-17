"""Client for the Knowledge Brain (Task 1) HTTP API. Task 3 never imports Task 1 code."""

import asyncio
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError


class KBSource(BaseModel):
    title: str
    url: str | None
    source_type: Literal["article", "expert"]
    section: str | None


class KBAnswer(BaseModel):
    status: Literal["answered", "partial", "insufficient_evidence"]
    answer: str
    sources: list[KBSource]
    needs_human: bool


class KnowledgeUnavailableError(RuntimeError):
    pass


class KnowledgeClient(Protocol):
    async def query(self, question: str) -> KBAnswer: ...


class HttpKnowledgeClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout_s: float = 60.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self.client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout_s, transport=transport
        )
        self.retries = retries

    async def query(self, question: str) -> KBAnswer:
        last = "unknown error"
        for attempt in range(self.retries + 1):
            try:
                resp = await self.client.post(
                    "/query", json={"question": question, "caller": "scope-builder"}
                )
                if resp.status_code == 200:
                    return KBAnswer.model_validate(resp.json())
                last = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # bad key or bad input will not improve with retries
            except httpx.TransportError as exc:
                last = repr(exc)
            except ValidationError as exc:
                raise KnowledgeUnavailableError(f"unexpected response shape: {exc}") from exc
            if attempt < self.retries:
                await asyncio.sleep(0.5 * 2**attempt)
        raise KnowledgeUnavailableError(f"Knowledge Brain unavailable: {last}")

    async def aclose(self) -> None:
        await self.client.aclose()
