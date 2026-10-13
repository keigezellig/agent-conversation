import io
import json
import urllib.error

import pytest

from app import agents
from app.agents import (ANTHROPIC_VERSION, DEFAULT_EAGERNESS, EAGERNESS_SCALE, _complete,
                        _rate_eagerness, _read_languages, parse_args)

SYSTEM = "You are Mira, one of several AI agents."
HISTORY = [{"role": "user", "content": "Start a conversation."}, {"role": "assistant", "content": "Cars."}]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


def _answering(payload, monkeypatch):
    """Capture the one request the runner sends and answer it with the given payload."""
    sent = {}

    def urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["headers"] = {name.lower(): value for name, value in request.header_items()}
        sent["body"] = json.loads(request.data)
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(agents.urllib.request, "urlopen", urlopen)
    return sent


def _failing(code, detail, monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "boom", {}, io.BytesIO(detail.encode()))

    monkeypatch.setattr(agents.urllib.request, "urlopen", urlopen)


CLAUDE_REPLY = {"content": [{"type": "text", "text": " Cars should go. "}], "stop_reason": "end_turn"}
OPENAI_REPLY = {"choices": [{"message": {"content": " Cars should go. "}}]}


def test_the_claude_api_gets_the_system_prompt_beside_the_messages(monkeypatch):
    sent = _answering(CLAUDE_REPLY, monkeypatch)
    reply = _complete("claude", "https://api.anthropic.com/v1/", "claude-sonnet-5", "sk-ant-x",
                      SYSTEM, HISTORY, 1024)
    assert reply == "Cars should go."
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["body"] == {
        "model": "claude-sonnet-5",
        "max_tokens": 1024,
        "system": SYSTEM,
        "messages": HISTORY,
    }


def test_the_claude_api_is_called_with_a_key_and_a_version(monkeypatch):
    sent = _answering(CLAUDE_REPLY, monkeypatch)
    _complete("claude", "https://api.anthropic.com/v1", "claude-sonnet-5", "sk-ant-x", SYSTEM, HISTORY, 1024)
    assert sent["headers"]["x-api-key"] == "sk-ant-x"
    assert sent["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert "authorization" not in sent["headers"]


def test_a_claude_reply_split_over_text_blocks_is_joined(monkeypatch):
    _answering(
        {"content": [{"type": "text", "text": "Cars "}, {"type": "text", "text": "should go."}]},
        monkeypatch,
    )
    assert _complete("claude", "http://model/v1", "m", "k", SYSTEM, HISTORY, 1024) == "Cars should go."


def test_claude_blocks_that_are_not_text_are_left_out(monkeypatch):
    _answering(
        {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Cars should go."}]},
        monkeypatch,
    )
    assert _complete("claude", "http://model/v1", "m", "k", SYSTEM, HISTORY, 1024) == "Cars should go."


def test_the_claude_api_needs_a_key():
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _complete("claude", "http://model/v1", "m", "", SYSTEM, HISTORY, 1024)


def test_an_empty_claude_reply_is_an_error(monkeypatch):
    _answering({"content": []}, monkeypatch)
    with pytest.raises(RuntimeError, match="empty response"):
        _complete("claude", "http://model/v1", "m", "k", SYSTEM, HISTORY, 1024)


def test_an_unrecognisable_claude_reply_is_an_error(monkeypatch):
    _answering({"error": {"message": "overloaded"}}, monkeypatch)
    with pytest.raises(RuntimeError, match="invalid message"):
        _complete("claude", "http://model/v1", "m", "k", SYSTEM, HISTORY, 1024)


def test_a_claude_http_error_keeps_its_detail(monkeypatch):
    _failing(429, '{"error":{"message":"rate limited"}}', monkeypatch)
    with pytest.raises(RuntimeError, match="HTTP 429: .*rate limited"):
        _complete("claude", "http://model/v1", "m", "k", SYSTEM, HISTORY, 1024)


def test_the_openai_api_still_gets_the_system_prompt_as_a_message(monkeypatch):
    sent = _answering(OPENAI_REPLY, monkeypatch)
    reply = _complete("openai", "http://localhost:11434/v1", "llama3.2", "", SYSTEM, HISTORY, 1024)
    assert reply == "Cars should go."
    assert sent["url"] == "http://localhost:11434/v1/chat/completions"
    assert sent["body"] == {
        "model": "llama3.2",
        "messages": [{"role": "system", "content": SYSTEM}, *HISTORY],
    }
    assert "authorization" not in sent["headers"]


def test_an_openai_key_is_sent_as_a_bearer_token(monkeypatch):
    sent = _answering(OPENAI_REPLY, monkeypatch)
    _complete("openai", "http://model/v1", "m", "sk-x", SYSTEM, HISTORY, 1024)
    assert sent["headers"]["authorization"] == "Bearer sk-x"
    assert "x-api-key" not in sent["headers"]


def test_an_empty_openai_reply_is_an_error(monkeypatch):
    _answering({"choices": [{"message": {"content": "  "}}]}, monkeypatch)
    with pytest.raises(RuntimeError, match="empty response"):
        _complete("openai", "http://model/v1", "m", "k", SYSTEM, HISTORY, 1024)


PERSONAS = [("Mira", "curious and optimistic", None), ("Sol", "loud and never lets anyone finish", None)]


def _rate(answer, monkeypatch):
    sent = _answering({"choices": [{"message": {"content": answer}}]}, monkeypatch)
    rated = _rate_eagerness("openai", "http://model/v1", "m", "", PERSONAS, 1024)
    return rated, sent


def test_each_mood_is_rated_for_how_readily_it_interrupts(monkeypatch):
    rated, sent = _rate("Mira: 1\nSol: 4", monkeypatch)
    assert rated == {"Mira": 1, "Sol": 4}
    asked = sent["body"]["messages"][-1]["content"]
    assert "never lets anyone finish" in asked and f"0 (always waits their turn) to {EAGERNESS_SCALE}" in asked


def test_a_rating_is_read_out_of_a_wordier_answer_and_capped(monkeypatch):
    rated, _ = _rate("Here you go:\n- **Mira** - 2 (she listens)\nSol: 9/10, talks over everyone", monkeypatch)
    assert rated == {"Mira": 2, "Sol": EAGERNESS_SCALE}


def test_a_persona_the_rating_skips_gets_the_middle_rating(monkeypatch):
    rated, _ = _rate("Sol: 3\nI cannot rate the other one.", monkeypatch)
    assert rated == {"Mira": DEFAULT_EAGERNESS, "Sol": 3}


def test_a_failed_rating_falls_back_to_the_middle_without_ending_the_run(monkeypatch, capsys):
    _failing(429, '{"error":{"message":"rate limited"}}', monkeypatch)
    assert _rate_eagerness("openai", "http://model/v1", "m", "", PERSONAS, 1024) == {
        "Mira": DEFAULT_EAGERNESS,
        "Sol": DEFAULT_EAGERNESS,
    }
    assert "cannot rate how readily the agents interrupt" in capsys.readouterr().err


MIXED = [("Santa", "a talk show host who keeps neutral", None),
         ("Milorad", "a serbian nationalist who writes Serbian answers, arrogant", None)]


def _languages(answer, monkeypatch, personas=MIXED):
    _answering({"choices": [{"message": {"content": answer}}]}, monkeypatch)
    return _read_languages("openai", "http://model/v1", "m", "", personas, 1024)


def test_a_language_named_in_a_mood_is_read_and_the_silent_mood_is_left_alone(monkeypatch):
    assert _languages("Santa: none\nMilorad: Serbian", monkeypatch) == {"Santa": None, "Milorad": "Serbian"}


@pytest.mark.parametrize("answer", [
    # All three seen from a small model answering "NAME: LANGUAGE" a little too literally.
    "Santa: Santa: none\nMilorad: Milorad: Serbian",
    "Santa: none: none\nMilorad: Serbian: Serbian",
    "Santa: none\nMilorad: Mirorad: Serbian",
])
def test_a_label_echoed_into_the_answer_is_not_mistaken_for_the_language(answer, monkeypatch):
    assert _languages(answer, monkeypatch) == {"Santa": None, "Milorad": "Serbian"}


def test_a_shouted_or_lowercase_language_is_tidied_before_it_reaches_a_prompt(monkeypatch):
    assert _languages("Santa: NONE\nMilorad: SERBIAN", monkeypatch)["Milorad"] == "Serbian"
    assert _languages("Santa: none\nMilorad: serbian", monkeypatch)["Milorad"] == "Serbian"


def test_a_wordy_refusal_is_still_no_language(monkeypatch):
    assert _languages("Santa: none (nothing is said)\nMilorad: Serbian", monkeypatch)["Santa"] is None


def test_a_language_the_mood_never_names_is_thrown_away(monkeypatch):
    # Both of these came back from a small model reading the same two moods.
    assert _languages("Santa: English\nMilorad: Serbian", monkeypatch) == {"Santa": None, "Milorad": "Serbian"}
    assert _languages("Santa: none\nMilorad: Croatian", monkeypatch) == {"Santa": None, "Milorad": None}


def test_the_question_rules_out_inferring_a_language_from_nationality(monkeypatch):
    sent = _answering({"choices": [{"message": {"content": "Santa: none\nMilorad: Serbian"}}]}, monkeypatch)
    _read_languages("openai", "http://model/v1", "m", "", MIXED, 1024)
    asked = sent["body"]["messages"][-1]["content"]
    assert "is not such an instruction" in asked and "nationalist with nothing said about language" in asked


def test_a_two_word_language_survives_but_a_whole_sentence_does_not(monkeypatch):
    personas = [("Santa", "a talk show host who keeps neutral", None),
                ("Joao", "cheerful, writes Brazilian Portuguese answers", None)]
    answer = "Santa: none\nJoao: Brazilian Portuguese because the description says so"
    assert _languages(answer, monkeypatch, personas)["Joao"] == "Brazilian Portuguese"


@pytest.mark.parametrize("said", ["none", "None.", "n/a", "any", "**none**"])
def test_ways_of_saying_no_language_all_mean_none(said, monkeypatch):
    assert _languages(f"Santa: {said}\nMilorad: {said}", monkeypatch) == {"Santa": None, "Milorad": None}


def test_an_unreadable_answer_leaves_every_language_to_the_mood(monkeypatch):
    assert _languages("I am not sure what you mean.", monkeypatch) == {"Santa": None, "Milorad": None}


def test_a_failed_language_request_does_not_end_the_run(monkeypatch, capsys):
    _failing(500, "boom", monkeypatch)
    assert _read_languages("openai", "http://model/v1", "m", "", MIXED, 1024) == {
        "Santa": None,
        "Milorad": None,
    }
    assert "cannot read which language" in capsys.readouterr().err


def _args(monkeypatch, *argv, **environment):
    for name in ("LLM_API", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "ANTHROPIC_API_KEY", "LLM_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "sys.argv",
        ["agents", "--subject", "cars", "--agent", "Mira|curious", "--agent", "Sol|skeptical", *argv],
    )
    return parse_args()


def test_choosing_the_claude_api_points_at_it_with_a_claude_model(monkeypatch):
    args = _args(monkeypatch, "--llm-api", "claude")
    assert (args.llm_base_url, args.model) == ("https://api.anthropic.com/v1", "claude-sonnet-5")


def test_the_openai_api_stays_the_default(monkeypatch):
    args = _args(monkeypatch)
    assert (args.llm_api, args.llm_base_url, args.model) == ("openai", "http://localhost:11434/v1", "llama3.2")


def test_the_claude_api_takes_its_key_from_the_conventional_variable(monkeypatch):
    assert _args(monkeypatch, "--llm-api", "claude", ANTHROPIC_API_KEY="sk-ant-x").llm_api_key == "sk-ant-x"


def test_an_explicit_key_wins_over_the_conventional_variable(monkeypatch):
    args = _args(monkeypatch, "--llm-api", "claude", LLM_API_KEY="sk-chosen", ANTHROPIC_API_KEY="sk-ant-x")
    assert args.llm_api_key == "sk-chosen"


def test_a_chosen_model_and_endpoint_win_over_the_api_defaults(monkeypatch):
    args = _args(monkeypatch, "--llm-api", "claude", "--model", "claude-opus-5",
                 "--llm-base-url", "http://proxy/v1")
    assert (args.llm_base_url, args.model) == ("http://proxy/v1", "claude-opus-5")


def test_the_api_can_be_chosen_in_the_environment(monkeypatch):
    assert _args(monkeypatch, LLM_API="claude").llm_api == "claude"
