
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


def matches(condition, context):
    if not condition:
        return True

    if "all" in condition:
        return all(matches(c, context) for c in condition["all"])

    if "any" in condition:
        return any(matches(c, context) for c in condition["any"])

    attr = condition["attr"]
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
    except (TypeError, IndexError):
        raise ValueError(f"Incompatible values for {attr} and operator {op}")



def compute_price(config, product_id, context, at_time=None):
    from datetime import date
    from decimal import Decimal, ROUND_HALF_UP

    products = {p["id"]: p for p in config["products"]}
    if product_id not in products:
        raise ValueError(f"Unknown product: {product_id}")

    product = products[product_id]
    base = product["base"]
    if base["type"] != "fixed":
        raise ValueError("Only fixed base prices are supported")

    base_price = Decimal(str(base["value"]))
    price = base_price
    today = at_time or date.today()
    strategy = config.get("strategy", {})
    stacking = strategy.get("stacking", "sequential")

    supported = {
        "sequential", "on_base",
        "best_for_customer", "best_for_business",
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
            before = base_price + total_adjustment

            if kind == "percent":
                delta = base_price * value / 100
            elif kind == "flat":
                delta = value
            elif kind == "multiplier":
                delta = base_price * (value - 1)
            elif kind == "set":
                # A set rule overrides previous adjustments.
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

    # Apply minimum and maximum price constraints
    constraints = config.get("constraints", {})

    if "min_price" in constraints:
        price = max(price, Decimal(str(constraints["min_price"])))

    if "max_price" in constraints:
        price = min(price, Decimal(str(constraints["max_price"])))

    # Round the final price according to the configured strategy
    rounding = strategy.get("rounding", {})
    step = Decimal(str(rounding.get("step", 1)))

    if step <= 0:
        raise ValueError("Rounding step must be positive")

    rounding_mode = rounding.get("mode", "nearest")

    if rounding_mode == "nearest":
        price = (price / step).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ) * step
    elif rounding_mode == "up":
        from decimal import ROUND_CEILING
        price = (price / step).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        ) * step
    elif rounding_mode == "down":
        from decimal import ROUND_FLOOR
        price = (price / step).quantize(
            Decimal("1"), rounding=ROUND_FLOOR
        ) * step
    else:
        raise ValueError(f"Unsupported rounding mode: {rounding_mode}")

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
