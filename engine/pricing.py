
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR
import math

from simpleeval import SimpleEval


def matches(condition, context):
    if not condition:
        return True

    if "all" in condition:
        return all(matches(c, context) for c in condition["all"])

    if "any" in condition:
        return any(matches(c, context) for c in condition["any"])

    attr = condition.get("attr")
    if not attr:
        raise ValueError("Condition requires an attr")

    if attr not in context:
        return False

    actual = context[attr]
    op = condition.get("op", "==")
    expected = condition.get("value")

    checks = {
        "==": lambda: actual == expected,
        "!=": lambda: actual != expected,
        ">": lambda: actual > expected,
        ">=": lambda: actual >= expected,
        "<": lambda: actual < expected,
        "<=": lambda: actual <= expected,
        "in": lambda: actual in expected,
        "not_in": lambda: actual not in expected,
        "contains": lambda: expected in actual,
        "between": lambda: expected[0] <= actual <= expected[1],
    }

    if op not in checks:
        raise ValueError(f"Unsupported condition operator: {op}")

    try:
        return checks[op]()
    except (TypeError, IndexError, KeyError):
        raise ValueError(
            f"Incompatible values for {attr} and operator {op}"
        ) from None


def evaluate_formula(expression, variables):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Formula base requires an expression")

    if not isinstance(variables, dict):
        raise ValueError("Formula variables must be an object")

    allowed_names = {
        "base_price",
        "demand_factor",
        "distance",
        "rate",
        "occupancy_percent",
        "quantity",
    }

    if not variables or not set(variables).issubset(allowed_names):
        raise ValueError("Formula contains unsupported or missing variables")

    safe_variables = {}

    for name, value in variables.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError(f"Formula variable {name} must be numeric")

        try:
            number = float(value)
        except (ValueError, TypeError):
            raise ValueError(f"Formula variable {name} must be numeric") from None

        if not math.isfinite(number):
            raise ValueError(f"Formula variable {name} must be finite")

        safe_variables[name] = number

    evaluator = SimpleEval(names=safe_variables)
    try:
        result = evaluator.eval(expression)
    except Exception as exc:
        raise ValueError(f"Invalid pricing formula: {exc}") from None

    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ValueError("Formula must produce a numeric result")

    if not math.isfinite(float(result)):
        raise ValueError("Formula result must be finite")

    return Decimal(str(result))


def compute_price(config, product_id, context, at_time=None):
    context = context or {}
    products = {p["id"]: p for p in config["products"]}

    if product_id not in products:
        raise ValueError(f"Unknown product: {product_id}")

    product = products[product_id]
    base = product["base"]
    base_type = base.get("type")

    if base_type == "fixed":
        base_price = Decimal(str(base["value"]))

    elif base_type == "formula":
        base_price = evaluate_formula(
            base.get("expression"),
            base.get("variables", {}),
        )

    elif base_type == "table":
        table = base.get("table", {})
        key = base.get("context_key")

        if not key:
            raise ValueError("Table base requires context_key")

        actual = context.get(key)

        if actual is None or actual not in table:
            raise ValueError(f"No table price found for {key}: {actual}")

        base_price = Decimal(str(table[actual]))

    elif base_type == "tiered":
        key = base.get("context_key")
        tiers = base.get("tiers", [])

        if not key:
            raise ValueError("Tiered base requires context_key")

        actual = context.get(key)

        if actual is None:
            raise ValueError(f"Missing context attribute: {key}")

        actual = Decimal(str(actual))
        matching_tiers = []

        for tier in tiers:
            lower = Decimal(str(tier["min"]))
            upper = (
                Decimal(str(tier["max"]))
                if tier.get("max") is not None
                else None
            )

            if actual >= lower and (upper is None or actual < upper):
                matching_tiers.append(tier)

        if len(matching_tiers) != 1:
            raise ValueError(
                f"Expected exactly one matching tier for {key}"
            )

        base_price = Decimal(str(matching_tiers[0]["price"]))

    else:
        raise ValueError(f"Unsupported base price type: {base_type}")

    if not base_price.is_finite() or base_price < 0:
        raise ValueError("Base price must be finite and non-negative")

    price = base_price
    today = at_time or date.today()

    if isinstance(today, str):
        today = date.fromisoformat(today)

    strategy = config.get("strategy", {})
    stacking = strategy.get("stacking", "sequential")

    supported = {
        "sequential",
        "on_base",
        "best_for_customer",
        "best_for_business",
    }

    if stacking not in supported:
        raise ValueError(f"Unsupported stacking mode: {stacking}")

    breakdown = [{
        "step": "Base price",
        "effect": None,
        "running": float(price),
    }]

    fired = []
    used_groups = set()
    matched = []

    for rule in sorted(
        config.get("rules", []),
        key=lambda r: r.get("priority", 100),
    ):
        start = rule.get("effective_from")
        end = rule.get("effective_to")

        if start and today < date.fromisoformat(start):
            continue
        if end and today > date.fromisoformat(end):
            continue
        if not matches(rule.get("when", {}), context):
            continue

        group = rule.get("exclusive_group")

        if group and group in used_groups:
            continue

        matched.append(rule)

        if group:
            used_groups.add(group)

        if rule.get("stop_processing"):
            break

    def apply_adjustment(current, rule, use_base=False):
        adjustment = rule["then"]
        kind = adjustment["type"]
        value = Decimal(str(adjustment["value"]))
        reference = base_price if use_base else current

        if kind == "percent":
            return reference * (1 + value / 100)
        if kind == "flat":
            return reference + value if use_base else current + value
        if kind == "multiplier":
            return reference * value
        if kind == "set":
            return value

        raise ValueError(f"Unsupported adjustment type: {kind}")

    if stacking == "on_base":
        total_adjustment = Decimal("0")

        for rule in matched:
            adjustment = rule["then"]
            kind = adjustment["type"]
            value = Decimal(str(adjustment["value"]))
            before = price

            if kind == "percent":
                delta = base_price * value / 100
            elif kind == "flat":
                delta = value
            elif kind == "multiplier":
                delta = base_price * (value - 1)
            elif kind == "set":
                price = value
                total_adjustment = price - base_price
                delta = None
            else:
                raise ValueError(f"Unsupported adjustment type: {kind}")

            if delta is not None:
                total_adjustment += delta
                price = base_price + total_adjustment

            fired.append(rule["id"])
            breakdown.append({
                "step": rule.get("name", rule["id"]),
                "effect": kind,
                "before": float(before),
                "running": float(price),
            })

    elif stacking in {"best_for_customer", "best_for_business"}:
        candidates = [
            (apply_adjustment(base_price, rule, True), rule)
            for rule in matched
        ]

        if candidates:
            chooser = min if stacking == "best_for_customer" else max
            price, selected = chooser(candidates, key=lambda item: item[0])
            fired = [selected["id"]]

            breakdown.append({
                "step": selected.get("name", selected["id"]),
                "effect": f"Selected by {stacking}",
                "running": float(price),
            })

    else:
        for rule in matched:
            before = price
            price = apply_adjustment(price, rule)
            fired.append(rule["id"])

            breakdown.append({
                "step": rule.get("name", rule["id"]),
                "effect": rule["then"]["type"],
                "before": float(before),
                "running": float(price),
            })

    constraints = config.get("constraints", {})
    original_price = price

    if "min_price" in constraints:
        price = max(price, Decimal(str(constraints["min_price"])))

    if "max_price" in constraints:
        price = min(price, Decimal(str(constraints["max_price"])))

    if price != original_price:
        breakdown.append({
            "step": "Price constraints",
            "effect": "clamped",
            "running": float(price),
        })

    rounding = strategy.get("rounding", {})
    step = Decimal(str(rounding.get("step", 1)))

    if not step.is_finite() or step <= 0:
        raise ValueError("Rounding step must be finite and positive")

    rounding_modes = {
        "nearest": ROUND_HALF_UP,
        "up": ROUND_CEILING,
        "down": ROUND_FLOOR,
    }
    rounding_mode = rounding.get("mode", "nearest")

    if rounding_mode not in rounding_modes:
        raise ValueError(f"Unsupported rounding mode: {rounding_mode}")

    price = (
        (price / step).quantize(
            Decimal("1"),
            rounding=rounding_modes[rounding_mode],
        )
        * step
    )

    breakdown.append({
        "step": "Final rounded price",
        "effect": None,
        "running": float(price),
    })

    return {
        "price": float(price),
        "currency": config.get("currency", "INR"),
        "breakdown": breakdown,
        "rules_fired": fired,
        "rules_evaluated": len(config.get("rules", [])),
    }
