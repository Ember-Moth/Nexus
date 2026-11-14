import pytest

from nexus.modules.billing.usage import normalize


def test_token_subsets_are_counted_once():
    _, counts = normalize(
        {
            "usage_format": "fixture.tokens.v1",
            "raw_usage": {
                "input_total_tokens": 1000,
                "cached_input_tokens": 200,
                "cache_write_5m_tokens": 100,
                "output_tokens": 100,
                "reasoning_tokens": 20,
            },
        },
        {
            "input_uncached_tokens",
            "input_cache_read_tokens",
            "input_cache_write_5m_tokens",
            "output_tokens",
        },
    )
    assert counts == {
        "input_uncached_tokens": 700,
        "input_cache_read_tokens": 200,
        "input_cache_write_5m_tokens": 100,
        "output_tokens": 100,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"cached_input_tokens": None},
        {"cached_input_tokens": 3},
        {"reasoning_tokens": 2},
        {"output_tokens": False},
    ],
)
def test_ambiguous_usage_requires_review(change):
    with pytest.raises(ValueError):
        normalize(
            {
                "usage_format": "fixture.tokens.v1",
                "raw_usage": {
                    "input_total_tokens": 2,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    **change,
                },
            },
            {"input_uncached_tokens", "output_tokens"},
        )
