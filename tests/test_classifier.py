"""Interpreting the token distribution, and the shape of the Ollama call.

Model *quality* is not asserted here - that is the eval set's job in Phase 2.
These tests answer a different question: given a response, does the parser
produce the right numbers, and does it fail in the right way when it cannot.
"""

import json
import math
import urllib.error
from unittest.mock import patch

import pytest

from app import classifier
from app.categories import Category, letter_map
from app.classifier import NoCategoryLetter, OllamaError, interpret


def entry(token, probability):
    """A top_logprobs entry, written as a probability for readability."""
    return {"token": token, "logprob": math.log(probability)}


def even_spread():
    """All six letters, equal mass."""
    return [entry(letter, 1 / 6) for letter in "ABCDEF"]


def test_full_distribution_normalises_to_one():
    result = interpret(even_spread())
    assert sum(result.distribution.values()) == pytest.approx(1.0)
    assert set(result.distribution) == set(Category)
    assert result.missing_letters == ()


def test_argmax_and_confidence():
    result = interpret([entry("A", 0.6), entry("B", 0.2), entry("C", 0.2)])
    assert result.category is Category.TO_ACTION
    assert result.confidence == pytest.approx(0.6)


def test_letters_outside_the_top_20_score_zero_and_are_reported():
    """A truncated distribution is worth knowing about when reading back a
    surprising decision, so it goes to the log's `note` field."""
    result = interpret([entry("A", 0.7), entry("B", 0.3)])
    assert result.distribution[Category.PERSONAL] == 0.0
    assert result.missing_letters == ("C", "D", "E", "F")
    assert sum(result.distribution.values()) == pytest.approx(1.0)


def test_renormalisation_ignores_mass_on_non_category_tokens():
    """Half the mass sits on junk tokens; the six categories still sum to 1."""
    raw = [entry("A", 0.3), entry("B", 0.2), entry("the", 0.4), entry("\n", 0.1)]
    result = interpret(raw)
    assert result.distribution[Category.TO_ACTION] == pytest.approx(0.6)
    assert result.distribution[Category.RECEIPTS] == pytest.approx(0.4)
    assert sum(result.distribution.values()) == pytest.approx(1.0)


def test_whitespace_prefixed_tokens_are_the_same_answer():
    result = interpret([entry(" A", 0.7), entry("B", 0.3)])
    assert result.category is Category.TO_ACTION
    assert result.confidence == pytest.approx(0.7)


def test_lowercase_tokens_are_the_same_answer():
    result = interpret([entry("a", 0.7), entry("B", 0.3)])
    assert result.category is Category.TO_ACTION


def test_variants_of_one_letter_sum_rather_than_first_winning():
    """0.55 + 0.20 on "A" beats 0.25 on "B". Taking only the highest-ranked
    variant would report 0.69 for A instead of 0.75, and a split like this can
    push a genuinely confident answer under the threshold."""
    raw = [entry("A", 0.55), entry(" A", 0.20), entry("B", 0.25)]
    result = interpret(raw)
    assert result.distribution[Category.TO_ACTION] == pytest.approx(0.75)
    assert result.distribution[Category.RECEIPTS] == pytest.approx(0.25)


def test_retained_mass_reports_how_much_sat_on_letters():
    """Renormalising is conditional on the answer being a category letter, so
    confidence alone cannot tell a certain model from a barely-engaged one."""
    result = interpret([entry("A", 0.94), entry("B", 0.05), entry("Sorry", 0.01)])
    assert result.retained_mass == pytest.approx(0.99)


def test_confidence_hides_what_retained_mass_exposes():
    """The reason this field exists. Both responses report ~0.95 confidence;
    one had 99% of its mass on category letters and the other 20%."""
    compliant = interpret([entry("A", 0.94), entry("B", 0.05), entry("Sorry", 0.01)])
    mostly_prose = interpret(
        [entry("Message", 0.5), entry("The", 0.3), entry("A", 0.19), entry("B", 0.01)]
    )
    assert compliant.confidence == pytest.approx(mostly_prose.confidence, abs=0.01)
    assert compliant.retained_mass == pytest.approx(0.99)
    assert mostly_prose.retained_mass == pytest.approx(0.20)


def test_retained_mass_is_bounded_by_the_top_20_window():
    """It is the observable letter mass, not the true one - the top-20 is a
    truncated view of the vocabulary."""
    result = interpret([entry("A", 0.3), entry("B", 0.1)])
    assert result.retained_mass == pytest.approx(0.4)
    assert sum(result.distribution.values()) == pytest.approx(1.0)


def test_no_valid_letter_raises():
    """A failure, not a low-confidence result: it must apply no labels."""
    with pytest.raises(NoCategoryLetter):
        interpret([entry("the", 0.6), entry("Sorry", 0.4)])


def test_empty_top_logprobs_raises():
    with pytest.raises(NoCategoryLetter):
        interpret([])


def test_malformed_entries_are_skipped_not_fatal():
    result = interpret([{"logprob": -0.1}, {"token": None}, entry("A", 0.9)])
    assert result.category is Category.TO_ACTION


def test_a_permuted_mapping_reads_the_same_letters_differently():
    """Phase 2's permutation-stability check depends on this."""
    permuted = letter_map(tuple(reversed(classifier.DEFAULT_ORDER)))
    result = interpret([entry("A", 0.9), entry("B", 0.1)], mapping=permuted)
    assert result.category is Category.PERSONAL


# --- the prompt -----------------------------------------------------------

def test_system_prompt_lists_every_category_with_its_letter():
    prompt = classifier.build_system_prompt()
    for letter, category in letter_map().items():
        assert f"{letter} = {category.value}" in prompt


def test_user_message_truncates_the_body():
    message = classifier.build_user_message("a@b.com", "Subject", "x" * 5000, 100)
    assert message.count("x") == 100
    assert "From: a@b.com" in message
    assert "Subject: Subject" in message


# --- the Ollama call, mocked ----------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ollama_reply(top_logprobs, content="A"):
    return {
        "message": {"content": content},
        "logprobs": [{"top_logprobs": top_logprobs}],
    }


def call(payload_or_error):
    """Run classify() against a canned response, returning (result, request)."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        if isinstance(payload_or_error, Exception):
            raise payload_or_error
        return FakeResponse(payload_or_error)

    with patch("urllib.request.urlopen", fake_urlopen):
        result = classifier.classify(
            "billing@octopus.energy",
            "Your bill is ready",
            "payment due 15 October",
            model="llama3.1:8b",
            body_chars=1500,
        )
    return result, captured["request"]


def test_classify_sends_the_single_token_logprob_request():
    """The whole confidence mechanism depends on these four options."""
    _, request = call(ollama_reply([entry("A", 0.9), entry("B", 0.1)]))
    body = json.loads(request.data)
    assert body["logprobs"] is True
    assert body["top_logprobs"] == 20
    assert body["options"]["num_predict"] == 1
    assert body["options"]["temperature"] == 1.0
    assert body["stream"] is False


def test_classify_sends_the_built_prompt_and_the_email():
    _, request = call(ollama_reply([entry("A", 1.0)]))
    system, user = json.loads(request.data)["messages"]
    assert system["content"] == classifier.build_system_prompt()
    assert "billing@octopus.energy" in user["content"]
    assert "payment due 15 October" in user["content"]


def test_classify_returns_an_interpretation():
    result, _ = call(ollama_reply([entry("A", 0.9), entry("B", 0.1)]))
    assert result.category is Category.TO_ACTION
    assert result.confidence == pytest.approx(0.9)


def test_missing_logprobs_key_is_a_failure_not_a_guess():
    with pytest.raises(NoCategoryLetter):
        call({"message": {"content": "To Action"}})


def test_empty_logprobs_list_is_a_failure():
    with pytest.raises(NoCategoryLetter):
        call({"message": {"content": "A"}, "logprobs": []})


class MalformedResponse:
    """Ollama answered, but not with JSON - a truncated or proxied response."""

    def read(self):
        return b"<html>502 Bad Gateway</html>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_non_json_response_becomes_ollama_error():
    def fake_urlopen(request, timeout=None):
        return MalformedResponse()

    with (
        patch("urllib.request.urlopen", fake_urlopen),
        pytest.raises(OllamaError, match="invalid JSON"),
    ):
        classifier.classify("a@b.com", "s", "b", model="m", body_chars=100)


def test_http_error_status_becomes_ollama_error():
    """HTTPError is a URLError is an OSError - one net catches all three."""
    error = urllib.error.HTTPError(
        "http://localhost:11434", 500, "Internal Server Error", {}, None
    )
    with pytest.raises(OllamaError):
        call(error)


def test_unreachable_ollama_becomes_ollama_error():
    with pytest.raises(OllamaError):
        call(urllib.error.URLError("connection refused"))


def test_timeout_becomes_ollama_error():
    with pytest.raises(OllamaError):
        call(TimeoutError("timed out"))


def test_both_failure_modes_share_a_base_class():
    """The caller retries on either, so it should not have to name both."""
    assert issubclass(NoCategoryLetter, classifier.ClassifierError)
    assert issubclass(OllamaError, classifier.ClassifierError)
