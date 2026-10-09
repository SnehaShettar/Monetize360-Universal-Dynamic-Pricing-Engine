from engine.pricing import compute_price
import json
from pathlib import Path

import pytest
from engine.pricing import compute_price, matches


@pytest.fixture
def config():
    path = Path(__file__).parents[1] / "examples" / "hotel.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_weekend_occupancy_loyalty_price(config):
    result = compute_price(
        config,
        "deluxe_room",
        {
            "day_of_week": "Sat",
            "occupancy_percent": 85,
            "customer_segment": "loyalty",
        },
    )
    assert result["price"] == 3730
    assert result["rules_fired"] == ["R1", "R2", "R3"]


def test_base_price_without_matching_rules(config):
    result = compute_price(
        config,
        "deluxe_room",
        {
            "day_of_week": "Mon",
            "occupancy_percent": 50,
            "customer_segment": "regular",
        },
    )
    assert result["price"] == 3000
    assert result["rules_fired"] == []


def test_unknown_product_fails(config):
    with pytest.raises(ValueError, match="Unknown product"):
        compute_price(config, "missing_product", {})


def test_nested_conditions():
    condition = {
        "all": [
            {"attr": "stock", "op": ">", "value": 5},
            {
                "any": [
                    {"attr": "segment", "op": "==", "value": "premium"},
                    {"attr": "vip", "op": "==", "value": True},
                ]
            },
        ]
    }
    assert matches(condition, {"stock": 10, "segment": "premium"})
    assert not matches(condition, {"stock": 2, "segment": "premium"})


from engine.validator import validate_config


def test_hotel_config_is_valid(config):
    result = validate_config(config)
    assert result["valid"] is True, result["errors"]


def test_validator_catches_invalid_adjustment(config):
    config["rules"][0]["then"]["type"] = "magic"
    result = validate_config(config)

    assert result["valid"] is False
    assert any(
        "Unsupported adjustment" in item["message"]
        for item in result["errors"]
    )



@pytest.mark.parametrize(
    "mode, expected",
    [
    ("sequential", 3960),
    ("on_base", 3900),
    ("best_for_customer", 3300),
    ("best_for_business", 3600),
    ],
)
def test_stacking_modes(config, mode, expected):
    config["strategy"]["stacking"] = mode
    config["rules"] = [
        {
            "id": "R1",
            "name": "Increase",
            "priority": 10,
            "when": {},
            "then": {"type": "percent", "value": 20},
        },
        {
            "id": "R2",
            "name": "Discount",
            "priority": 20,
            "when": {},
            "then": {"type": "percent", "value": 10},
        },
    ]

    result = compute_price(config, "deluxe_room", {})
    assert result["price"] == expected
