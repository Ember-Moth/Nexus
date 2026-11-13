"""Exact monetary arithmetic shared by pricing, holds and ledger operations."""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

SCALE = Decimal("0.000000000001")
MAX_AMOUNT = Decimal("9999999999999999.999999999999")
DIMENSIONS = frozenset(
    {
        "input_uncached_tokens",
        "input_cache_read_tokens",
        "input_cache_write_5m_tokens",
        "input_cache_write_1h_tokens",
        "output_tokens",
    }
)


def decimal_value(value, places=12):
    if not isinstance(value, (str, Decimal)):
        raise ValueError("decimal values must be supplied as strings")
    if isinstance(value, str) and len(value) > 128:
        raise ValueError("decimal string is too long")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if (
        not result.is_finite()
        or result.as_tuple().exponent < -places
        or result.copy_abs() > MAX_AMOUNT
    ):
        raise ValueError("decimal is non-finite or outside supported precision")
    return result


def money_string(value: Decimal) -> str:
    return format(value, ".12f")


Money = Annotated[
    Decimal,
    BeforeValidator(decimal_value, json_schema_input_type=str),
    PlainSerializer(money_string, return_type=str),
]
Factor = Annotated[
    Decimal,
    BeforeValidator(lambda value: decimal_value(value, 6), json_schema_input_type=str),
    PlainSerializer(lambda value: format(value, ".6f"), return_type=str),
]


def rounded(value: Decimal, *, ceiling=False) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        result = value.quantize(SCALE, rounding=ROUND_CEILING if ceiling else ROUND_HALF_UP)
    if not result.is_finite() or not 0 <= result <= MAX_AMOUNT:
        raise ValueError("calculated amount is outside supported range")
    return result


def usage_amount(rates: dict, quantities: dict[str, int], factor: Decimal = Decimal(1)):
    if set(rates) != set(quantities) or not set(rates) <= DIMENSIONS:
        raise ValueError("usage dimensions must exactly match the frozen price")
    with localcontext() as context:
        context.prec = 60
        lines = []
        for dimension in sorted(rates):
            quantity = quantities[dimension]
            if type(quantity) is not int or not 0 <= quantity <= 1_000_000_000:
                raise ValueError("invalid token quantity")
            rate = rates[dimension]
            raw = Decimal(quantity) * Decimal(rate["unit_price"]) * factor / rate["unit_quantity"]
            lines.append(
                {
                    "dimension": dimension,
                    "quantity": quantity,
                    "unit_price": rate["unit_price"],
                    "unit_quantity": rate["unit_quantity"],
                    "discount_factor": format(factor, ".6f"),
                    "unrounded_amount": format(raw, "f"),
                }
            )
        total = sum((Decimal(line["unrounded_amount"]) for line in lines), Decimal(0))
        return rounded(total), lines


def reserve_amount(rates: dict, input_limit: int, output_limit: int, factor: Decimal):
    if not {"input_uncached_tokens", "output_tokens"} <= set(rates) or not set(rates) <= DIMENSIONS:
        raise ValueError("incomplete price dimensions")
    if any(
        type(value) is not int or not 0 <= value <= 1_000_000_000
        for value in (input_limit, output_limit)
    ):
        raise ValueError("invalid token bounds")
    with localcontext() as context:
        context.prec = 60
        unit = {
            dimension: Decimal(rate["unit_price"]) / rate["unit_quantity"]
            for dimension, rate in rates.items()
        }
        worst_input = max(
            price for dimension, price in unit.items() if dimension != "output_tokens"
        )
        return rounded(
            (input_limit * worst_input + output_limit * unit["output_tokens"]) * factor,
            ceiling=True,
        )
