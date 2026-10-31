"""The free local stack: Ollama answerer and rewriter (mocked HTTP), and the rewrite skip rule."""

import json
from pathlib import Path

import httpx
import respx

from kb.answer.llm import PassageInput
from kb.answer.local import OLLAMA_URL, OllamaAnswerer, OllamaQueryRewriter
from kb.db import connect, init_db
from kb.retrieval.indexer import sync_index
from kb.retrieval.search import Searcher
from tests.fakes import FakeEmbedder
from tests.test_retrieval import ARTICLES, add_article

PASSAGES = [PassageInput(1, "Refund a member payment", None, "Go to Finance >> Invoices.")]


def reply(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"message": {"content": content}, "prompt_eval_count": 50, "eval_count": 10}
    )


@respx.mock
async def test_local_answer_is_validated_and_retried() -> None:
    good = json.dumps({"status": "answered", "answer": "Use Finance.", "cited_passages": [1]})
    route = respx.post(f"{OLLAMA_URL}/api/chat").mock(
        side_effect=[reply("not json at all"), reply(good)]
    )
    result = await OllamaAnswerer("llama3.2").answer("How do I refund?", PASSAGES)
    assert result.draft.status == "answered" and result.draft.cited_passages == [1]
    assert result.model == "ollama:llama3.2" and result.cost_usd == 0.0
    assert (result.input_tokens, result.output_tokens) == (100, 20)  # both attempts counted
    sent = json.loads(route.calls[1].request.content)
    assert sent["format"]["title"] == "AnswerDraft"  # constrained to the schema
    assert "did not match the schema" in sent["messages"][-1]["content"]


@respx.mock
async def test_local_answer_that_never_validates_is_a_refusal() -> None:
    respx.post(f"{OLLAMA_URL}/api/chat").mock(return_value=reply('{"status": "maybe"}'))
    result = await OllamaAnswerer("llama3.2").answer("How do I refund?", PASSAGES)
    assert result.draft.status == "insufficient_evidence" and result.draft.cited_passages == []


@respx.mock
async def test_local_rewriter_falls_back_to_the_question_when_ollama_is_down() -> None:
    respx.post(f"{OLLAMA_URL}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    assert await OllamaQueryRewriter("llama3.2").rewrite("hide my site") == []


class CountingRewriter:
    def __init__(self) -> None:
        self.calls = 0

    async def rewrite(self, question: str) -> list[str]:
        self.calls += 1
        return ["Enable Lead Matching"]


async def test_rewrite_is_skipped_when_the_question_already_matches(tmp_path: Path) -> None:
    await init_db(tmp_path / "kb.db")
    async with connect(tmp_path / "kb.db") as db:
        for aid, (title, body) in ARTICLES.items():
            await add_article(db, aid, title, body)
        emb = FakeEmbedder()
        await sync_index(db, emb)
        rw = CountingRewriter()
        searcher = Searcher(emb, rewriter=rw, rewrite_skip_above=0.5)
        await searcher.search(db, "Refund a member payment Finance Invoices Refund")
        assert rw.calls == 0 and searcher.rewrites == {"used": 0, "skipped": 1}
        await searcher.search(db, "zebra quantum")  # matches nothing: rewrite
        assert rw.calls == 1 and searcher.rewrites["used"] == 1
        searcher.rewrite_skip_above = None  # default: always rewrite
        await searcher.search(db, "Refund a member payment Finance Invoices Refund")
        assert rw.calls == 2
