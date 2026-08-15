"""The model keyword catalogue — resolution, validation, and the override table.

The rules that matter here are the ones the turn path depends on: resolution never
raises, a literal id always wins, and an override is a *row* rather than a rewritten
table, so a better built-in default still reaches an install that never touched that
keyword.
"""

import pytest

from app import models
from app.config import Settings
from app.memory.store import MemoryStore


def _settings(**over):
    base = dict(memory_db_path=":memory:", llm_tier="balanced")
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store(monkeypatch):
    """A throwaway store, wired in as the one `app.models` writes through."""
    store = MemoryStore(":memory:")
    import app.memory as memory_pkg

    monkeypatch.setattr(memory_pkg, "get_store", lambda: store)
    models.invalidate_cache()
    yield store
    models.invalidate_cache()
    store.close()


# --- resolution ---


def test_every_builtin_keyword_resolves_to_a_model_id(store):
    for keyword in models.BUILTIN_MODELS:
        assert "/" in models.resolve(keyword, _settings())


def test_a_literal_model_id_passes_straight_through(store):
    assert models.resolve("vendor/thing-2", _settings()) == "vendor/thing-2"


def test_nothing_means_the_install_default(store):
    assert models.resolve(None, _settings(llm_tier="strong")) == (
        models.BUILTIN_MODELS["strong"]
    )


def test_an_unknown_keyword_falls_back_instead_of_raising(store):
    """This runs immediately before the model call. A typo in `.env` must cost one
    reply from the wrong model, never every reply."""
    assert models.resolve("nope", _settings(llm_tier="cheap")) == (
        models.BUILTIN_MODELS["cheap"]
    )


def test_a_broken_default_still_resolves_to_something(store):
    """Both the keyword *and* the fallback keyword can be wrong at once."""
    assert models.resolve("nope", _settings(llm_tier="also-nope")) == (
        models.BUILTIN_MODELS["balanced"]
    )


def test_the_runtimes_own_tier_names_keep_working(store):
    """`cheap`/`balanced`/`strong` predate this module and are still in .env files."""
    for tier in ("cheap", "balanced", "strong"):
        assert models.resolve(tier, _settings()) == models.BUILTIN_MODELS[tier]


# --- overrides ---


def test_an_override_changes_what_a_keyword_means(store):
    assert models.set_keyword("coding", "vendor/coder-9")
    assert models.resolve("coding", _settings()) == "vendor/coder-9"
    # And it is a row, so the store agrees with the cache.
    assert store.model_keywords()["coding"]["model"] == "vendor/coder-9"


def test_only_the_overridden_keyword_moves(store):
    models.set_keyword("coding", "vendor/coder-9")
    assert models.resolve("writing", _settings()) == models.BUILTIN_MODELS["writing"]


def test_resetting_returns_a_builtin_to_its_default(store):
    models.set_keyword("fast", "vendor/quick")
    assert models.reset_keyword("fast")
    assert models.resolve("fast", _settings()) == models.BUILTIN_MODELS["fast"]
    assert store.model_keywords() == {}


def test_a_keyword_can_be_invented(store):
    """The useful descriptions are personal; there is no reason to ship a closed set."""
    assert models.set_keyword("sql", "vendor/db-tuned")
    assert models.resolve("sql", _settings()) == "vendor/db-tuned"
    names = [k["name"] for k in models.options(_settings())["keywords"]]
    assert names[-1] == "sql"
    custom = models.options(_settings())["keywords"][-1]
    assert custom["custom"] is True
    assert custom["default_model"] is None


def test_overrides_survive_a_new_process(store):
    """The cache is an optimisation; the table is the truth."""
    models.set_keyword("strong", "vendor/big")
    models.invalidate_cache()
    assert models.resolve("strong", _settings()) == "vendor/big"


# --- validation ---


@pytest.mark.parametrize(
    "keyword", ["", "  ", "with space", "has/slash", "9lives", "x" * 33, None, 7]
)
def test_bad_keywords_are_refused(store, keyword):
    assert models.valid_keyword(keyword) is False
    assert models.set_keyword(keyword, "vendor/model") is False


def test_keywords_are_normalised_rather_than_rejected(store):
    """Case and stray whitespace are a client's formatting, not a different keyword —
    refusing "Coding" would make a picker's label and its value two separate bugs."""
    assert models.set_keyword(" Coding ", " vendor/coder-9 ")
    assert list(store.model_keywords()) == ["coding"]
    assert store.model_keywords()["coding"]["model"] == "vendor/coder-9"


@pytest.mark.parametrize("model", ["", "balanced", "no-slash", "vendor/with space", None, 7])
def test_bad_model_ids_are_refused(store, model):
    """Requiring the slash catches the commonest mistake — a keyword in the model
    field — here, rather than as a 400 from OpenRouter mid-turn."""
    assert models.valid_model(model) is False
    assert models.set_keyword("coding", model) is False


# --- the patch a client sends ---


def test_apply_map_sets_and_resets(store):
    assert models.apply_map({"coding": "vendor/coder-9"}) == ["coding"]
    assert models.resolve("coding", _settings()) == "vendor/coder-9"
    # `null` is "back to your default", exactly as in a set_voice patch.
    assert models.apply_map({"coding": None}) == ["coding"]
    assert models.resolve("coding", _settings()) == models.BUILTIN_MODELS["coding"]


def test_apply_map_drops_junk_and_keeps_the_rest(store):
    changed = models.apply_map(
        {"coding": "vendor/coder-9", "BAD NAME": "vendor/x", "writing": "nonsense"}
    )
    assert changed == ["coding"]
    assert models.resolve("writing", _settings()) == models.BUILTIN_MODELS["writing"]


def test_apply_map_ignores_a_non_object(store):
    assert models.apply_map("coding=vendor/x") == []


# --- what goes on the wire ---


def test_options_describes_every_keyword(store):
    options = models.options(_settings(llm_tier="fast"))
    assert options["default_keyword"] == "fast"
    by_name = {k["name"]: k for k in options["keywords"]}
    assert set(by_name) == set(models.BUILTIN_MODELS)
    # A picker needs the description to be worth having keywords at all.
    assert all(k["description"] for k in options["keywords"] if not k["custom"])
    assert by_name["coding"]["overridden"] is False


def test_options_marks_an_override_so_a_client_can_offer_a_reset(store):
    models.set_keyword("coding", "vendor/coder-9")
    by_name = {k["name"]: k for k in models.options(_settings())["keywords"]}
    assert by_name["coding"]["overridden"] is True
    assert by_name["coding"]["model"] == "vendor/coder-9"
    assert by_name["coding"]["default_model"] == models.BUILTIN_MODELS["coding"]


def test_options_names_the_other_model_roles(store):
    """Memory and maintenance have no connection to be chosen on, but a UI that
    showed only the brain would imply Amber makes one kind of model call."""
    roles = models.options(_settings(memory_tier="cheap"))["roles"]
    assert roles == {"brain": "balanced", "memory": "cheap", "maintenance": "balanced"}


def test_state_distinguishes_a_choice_from_a_default(store):
    settings = _settings(llm_tier="balanced")
    assert models.state(None, settings) == {
        "keyword": "balanced",
        "model": models.BUILTIN_MODELS["balanced"],
        "default_keyword": "balanced",
        "chosen": False,
    }
    # Same resolved values, different state: only the first follows AMBER_LLM_TIER.
    assert models.state("balanced", settings)["chosen"] is True
