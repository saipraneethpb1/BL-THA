from kb.answer.claims import check_claims


class FakeEntailment:
    def supports(self, premise: str, claim: str) -> bool:
        return claim in premise


def test_all_supported():
    assert check_claims("One. Two.", ["One. Two."], FakeEntailment()) == (["One.", "Two."], [])


def test_partial():
    assert check_claims("One. Two.", ["One."], FakeEntailment()) == (["One."], ["Two."])


def test_no_support_or_citation():
    assert check_claims("One.", ["Other."], FakeEntailment()) == ([], ["One."])
    assert check_claims("One.", [], FakeEntailment()) == ([], ["One."])
