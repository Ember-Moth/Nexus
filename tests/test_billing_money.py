from decimal import Decimal

import pytest
from pydantic import ValidationError

from nexus.modules.billing.money import decimal_value, reserve_amount, usage_amount
from nexus.modules.billing.schemas import DiscountCreate, Rate


@pytest.mark.parametrize(
    "value",
    [1, 0.1, True, "NaN", "sNaN", "Infinity", "-Infinity", "1e999", "1e1000000", "0.0000000000001"],
)
def test_money_rejects_lossy_or_unbounded_values(value):
    with pytest.raises(ValueError):
        decimal_value(value)


def test_fee_rounds_once_and_reservation_rounds_up():
    rates = {
        dimension: {"unit_price": "0.000000000001", "unit_quantity": 1000}
        for dimension in ("input_uncached_tokens", "output_tokens")
    }
    amount, lines = usage_amount(rates, {"input_uncached_tokens": 300, "output_tokens": 300})
    assert amount == Decimal("0.000000000001")
    assert all(Decimal(line["unrounded_amount"]) == Decimal("0.0000000000003") for line in lines)
    assert reserve_amount(rates, 1, 1, Decimal(1)) == Decimal("0.000000000001")
    assert usage_amount(rates, {"input_uncached_tokens": 1, "output_tokens": 1})[0] == 0


def test_reserve_uses_most_expensive_nonoverlapping_input_dimension():
    rates = {
        "input_uncached_tokens": {"unit_price": "2", "unit_quantity": 1000000},
        "input_cache_read_tokens": {"unit_price": "0.5", "unit_quantity": 1000000},
        "input_cache_write_1h_tokens": {"unit_price": "4", "unit_quantity": 1000000},
        "output_tokens": {"unit_price": "8", "unit_quantity": 1000000},
    }
    assert reserve_amount(rates, 1000, 100, Decimal(".8")) == Decimal(".00384")
    amount, _ = usage_amount(
        rates,
        {
            "input_uncached_tokens": 800,
            "input_cache_read_tokens": 200,
            "input_cache_write_1h_tokens": 0,
            "output_tokens": 100,
        },
        Decimal(".8"),
    )
    assert amount == Decimal(".002")
    with pytest.raises(ValueError):
        usage_amount(rates, {"input_uncached_tokens": 800, "output_tokens": 100})


@pytest.mark.parametrize("factor", ["0", "-1", "1.000001", "0.1234567"])
def test_discount_range_and_precision(factor):
    with pytest.raises(ValidationError):
        DiscountCreate(factor=factor)


@pytest.mark.parametrize("unit", [True, 1.0, "1000", 100])
def test_price_unit_is_an_explicit_integer(unit):
    with pytest.raises(ValidationError):
        Rate(unit_price="1", unit_quantity=unit)


def test_openapi_advertises_decimal_strings_and_no_gateway_management_routes(settings_factory):
    from nexus.api import create_app

    spec = create_app(settings_factory()).openapi()
    ref = spec["components"]["schemas"]["SaleCreate"]["properties"]["rates"][
        "additionalProperties"
    ]["$ref"]
    price = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]["unit_price"]
    while "$ref" in price:
        price = spec["components"]["schemas"][price["$ref"].rsplit("/", 1)[-1]]
    assert price["type"] == "string"
    assert not any(
        "/reserve" in path or "/settle" in path or "/attempt" in path for path in spec["paths"]
    )
