"""Keep unit tests independent of network downloads and production NLI weights."""

import pytest

from kb.answer import service


class FixtureEntailment:
    def supports(self, premise: str, claim: str) -> bool:
        return claim == "Go to Finance." and "finance" in premise.lower()


@pytest.fixture(autouse=True)
def offline_entailment(monkeypatch):
    monkeypatch.setattr(service, "local_entailment", FixtureEntailment())
