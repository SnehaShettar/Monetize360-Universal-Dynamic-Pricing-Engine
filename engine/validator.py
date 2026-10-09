
from datetime import date

SUPPORTED_OPERATORS = {
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in", "contains", "between",
}
SUPPORTED_ADJUSTMENTS = {"percent", "flat", "multiplier", "set"}


def validate_config(config):
    errors = []
    warnings = []

    def error(path, message):
        errors.append({"path": path, "message": message})

    if not isinstance(config, dict):
        return {
            "valid": False,
            "errors": [{"path": "$", "message": "Config must be an object"}],
            "warnings": [],
        }

    if not isinstance(config.get("products"), list) or not config["products"]:
        error("products", "At least one product is required")

    products = config.get("products", [])
    product_ids = set()

    for i, product in enumerate(products):
        path = f"products[{i}]"
        if not isinstance(product, dict):
            error(path, "Product must be an object")
            continue

        product_id = product.get("id")
        if not product_id:
            error(path + ".id", "Product ID is required")
        elif product_id in product_ids:
            error(path + ".id", f"Duplicate product ID: {product_id}")
        else:
            product_ids.add(product_id)

        base = product.get("base")
        if not isinstance(base, dict):
            error(path + ".base", "Base pricing configuration is required")
        elif base.get("type") != "fixed":
            error(path + ".base.type", "MVP supports fixed base prices only")
        else:
            try:
                value = float(base["value"])
                if value < 0:
                    error(path + ".base.value", "Base price cannot be negative")
            except (KeyError, TypeError, ValueError):
                error(path + ".base.value", "Base price must be numeric")

    rules = config.get("rules", [])
    if not isinstance(rules, list):
        error("rules", "Rules must be a list")
        rules = []

    rule_ids = set()
    priorities = {}

    def check_condition(condition, path):
        if not isinstance(condition, dict):
            error(path, "Condition must be an object")
            return

        groups = [key for key in ("all", "any") if key in condition]
        if groups:
            if len(groups) != 1 or len(condition) != 1:
                error(path, "Condition groups must contain only all or any")
                return
            group = groups[0]
            children = condition[group]
            if not isinstance(children, list) or not children:
                error(path + "." + group, "Group must be a non-empty list")
                return
            for i, child in enumerate(children):
                check_condition(child, f"{path}.{group}[{i}]")
            return

        if not condition.get("attr"):
            error(path + ".attr", "Condition attribute is required")

        op = condition.get("op")
        if op not in SUPPORTED_OPERATORS:
            error(path + ".op", f"Unsupported operator: {op}")

        if "value" not in condition:
            error(path + ".value", "Condition value is required")

        if op == "between":
            value = condition.get("value")
            if not isinstance(value, list) or len(value) != 2:
                error(path + ".value", "between requires a two-item list")

    for i, rule in enumerate(rules):
        path = f"rules[{i}]"
        if not isinstance(rule, dict):
            error(path, "Rule must be an object")
            continue

        rule_id = rule.get("id")
        if not rule_id:
            error(path + ".id", "Rule ID is required")
        elif rule_id in rule_ids:
            error(path + ".id", f"Duplicate rule ID: {rule_id}")
        else:
            rule_ids.add(rule_id)

        try:
            priority = int(rule.get("priority", 100))
            if priority < 0:
                error(path + ".priority", "Priority cannot be negative")
            priorities.setdefault(priority, []).append(rule_id or path)
        except (TypeError, ValueError):
            error(path + ".priority", "Priority must be an integer")

        check_condition(rule.get("when", {}), path + ".when")

        adjustment = rule.get("then")
        if not isinstance(adjustment, dict):
            error(path + ".then", "Adjustment is required")
        else:
            kind = adjustment.get("type")
            if kind not in SUPPORTED_ADJUSTMENTS:
                error(path + ".then.type", f"Unsupported adjustment: {kind}")
            try:
                value = float(adjustment["value"])
                if kind == "multiplier" and value < 0:
                    error(path + ".then.value", "Multiplier cannot be negative")
            except (KeyError, TypeError, ValueError):
                error(path + ".then.value", "Adjustment value must be numeric")

        for field in ("effective_from", "effective_to"):
            value = rule.get(field)
            if value is not None:
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    error(path + "." + field, "Date must use YYYY-MM-DD")

        if rule.get("effective_from") and rule.get("effective_to"):
            try:
                if date.fromisoformat(rule["effective_from"]) > date.fromisoformat(rule["effective_to"]):
                    error(path, "Effective start date must not be after end date")
            except (TypeError, ValueError):
                pass

    for priority, ids in priorities.items():
        if len(ids) > 1:
            warnings.append({
                "path": "rules",
                "message": f"Rules {ids} share priority {priority}; check for conflicts",
            })

    constraints = config.get("constraints", {})
    if not isinstance(constraints, dict):
        error("constraints", "Constraints must be an object")
        constraints = {}

    minimum = constraints.get("min_price")
    maximum = constraints.get("max_price")

    for key, value in (("min_price", minimum), ("max_price", maximum)):
        if value is not None:
            try:
                if float(value) < 0:
                    error(f"constraints.{key}", "Price constraint cannot be negative")
            except (TypeError, ValueError):
                error(f"constraints.{key}", "Price constraint must be numeric")

    if minimum is not None and maximum is not None:
        try:
            if float(minimum) > float(maximum):
                error("constraints", "min_price cannot exceed max_price")
        except (TypeError, ValueError):
            pass

    rounding = config.get("strategy", {}).get("rounding", {})
    try:
        if float(rounding.get("step", 1)) <= 0:
            error("strategy.rounding.step", "Rounding step must be positive")
    except (AttributeError, TypeError, ValueError):
        error("strategy.rounding.step", "Rounding step must be numeric")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
