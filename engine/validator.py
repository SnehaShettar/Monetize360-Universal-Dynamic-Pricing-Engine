import ast
import math
from datetime import date

SUPPORTED_BASE_TYPES = {"fixed", "formula", "table", "tiered"}

SUPPORTED_OPERATORS = {
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in", "contains", "between",
}

SUPPORTED_ADJUSTMENTS = {"percent", "flat", "multiplier", "set"}

SUPPORTED_STACKING_MODES = {
    "sequential",
    "on_base",
    "best_for_customer",
    "best_for_business",
}

ALLOWED_FORMULA_VARIABLES = {
    "base_price",
    "demand_factor",
    "distance",
    "rate",
    "occupancy_percent",
    "quantity",
}

ALLOWED_FORMULA_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
)


def _is_number(value):
    """Accept finite numeric values, but reject booleans."""
    if isinstance(value, bool):
        return False

    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_formula(base, path, error):
    expression = base.get("expression")

    if not isinstance(expression, str) or not expression.strip():
        error(path + ".expression", "Formula expression is required")
    else:
        try:
            tree = ast.parse(expression, mode="eval")

            for node in ast.walk(tree):
                if not isinstance(node, ALLOWED_FORMULA_NODES):
                    error(
                        path + ".expression",
                        "Formula contains an unsupported operation",
                    )
                    break

                if isinstance(node, ast.Name):
                    if node.id not in ALLOWED_FORMULA_VARIABLES:
                        error(
                            path + ".expression",
                            f"Unsupported formula variable: {node.id}",
                        )
                        break
        except (SyntaxError, ValueError, TypeError):
            error(path + ".expression", "Formula expression is invalid")

    variables = base.get("variables")

    if not isinstance(variables, dict) or not variables:
        error(path + ".variables", "Formula variables must be a non-empty object")
        return

    for name, value in variables.items():
        variable_path = path + ".variables." + str(name)

        if name not in ALLOWED_FORMULA_VARIABLES:
            error(variable_path, f"Unsupported formula variable: {name}")

        if not _is_number(value):
            error(variable_path, "Formula variable must be a finite number")


def _validate_base(base, path, error):
    if not isinstance(base, dict):
        error(path, "Base pricing configuration is required")
        return

    base_type = base.get("type")

    if base_type not in SUPPORTED_BASE_TYPES:
        error(path + ".type", f"Unsupported base pricing type: {base_type}")
        return

    if base_type == "fixed":
        value = base.get("value")

        if not _is_number(value):
            error(path + ".value", "Base price must be a finite number")
        elif float(value) < 0:
            error(path + ".value", "Base price cannot be negative")

    elif base_type == "formula":
        _validate_formula(base, path, error)

    elif base_type == "table":
        context_key = base.get("context_key")

        if not isinstance(context_key, str) or not context_key.strip():
            error(path + ".context_key", "Table context_key is required")

        table = base.get("table")

        if not isinstance(table, dict) or not table:
            error(path + ".table", "Price table must be a non-empty object")
            return

        for key, value in table.items():
            entry_path = path + ".table." + str(key)

            if not _is_number(value):
                error(entry_path, "Table price must be a finite number")
            elif float(value) < 0:
                error(entry_path, "Table price cannot be negative")

    elif base_type == "tiered":
        context_key = base.get("context_key")

        if not isinstance(context_key, str) or not context_key.strip():
            error(path + ".context_key", "Tiered context_key is required")

        tiers = base.get("tiers")

        if not isinstance(tiers, list) or not tiers:
            error(path + ".tiers", "At least one pricing tier is required")
            return

        previous_max = None

        for i, tier in enumerate(tiers):
            tier_path = f"{path}.tiers[{i}]"

            if not isinstance(tier, dict):
                error(tier_path, "Tier must be an object")
                continue

            minimum = tier.get("min", 0)
            maximum = tier.get("max")
            price = tier.get("price")

            if not _is_number(minimum):
                error(tier_path + ".min", "Tier minimum must be a finite number")
                minimum_valid = False
            else:
                minimum = float(minimum)
                minimum_valid = True

                if minimum < 0:
                    error(tier_path + ".min", "Tier minimum cannot be negative")

            if maximum is not None:
                if not _is_number(maximum):
                    error(tier_path + ".max", "Tier maximum must be a finite number")
                    maximum_valid = False
                else:
                    maximum = float(maximum)
                    maximum_valid = True

                    if minimum_valid and maximum <= minimum:
                        error(
                            tier_path + ".max",
                            "Tier maximum must be greater than its minimum",
                        )
            else:
                maximum_valid = True

            if not _is_number(price):
                error(tier_path + ".price", "Tier price must be a finite number")
            elif float(price) < 0:
                error(tier_path + ".price", "Tier price cannot be negative")

            if (
                minimum_valid
                and previous_max is not None
                and minimum < previous_max
            ):
                error(tier_path, "Tier ranges must not overlap")

            if maximum is not None and maximum_valid and _is_number(maximum):
                previous_max = float(maximum)
            elif maximum is None:
                previous_max = None


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

    # Products
    products = config.get("products")

    if not isinstance(products, list) or not products:
        error("products", "At least one product is required")
        products = []

    product_ids = set()

    for i, product in enumerate(products):
        path = f"products[{i}]"

        if not isinstance(product, dict):
            error(path, "Product must be an object")
            continue

        product_id = product.get("id")

        if not isinstance(product_id, str) or not product_id.strip():
            error(path + ".id", "Product ID is required")
        elif product_id in product_ids:
            error(path + ".id", f"Duplicate product ID: {product_id}")
        else:
            product_ids.add(product_id)

        _validate_base(product.get("base"), path + ".base", error)

    # Rules and conditions
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
        elif not isinstance(condition.get("attr"), str):
            error(path + ".attr", "Condition attribute must be a string")

        op = condition.get("op")

        if op not in SUPPORTED_OPERATORS:
            error(path + ".op", f"Unsupported operator: {op}")

        if "value" not in condition:
            error(path + ".value", "Condition value is required")
        elif op == "between":
            value = condition["value"]

            if (
                not isinstance(value, list)
                or len(value) != 2
                or not all(_is_number(item) for item in value)
            ):
                error(
                    path + ".value",
                    "between requires a two-item numeric list",
                )

    for i, rule in enumerate(rules):
        path = f"rules[{i}]"

        if not isinstance(rule, dict):
            error(path, "Rule must be an object")
            continue

        rule_id = rule.get("id")

        if not isinstance(rule_id, str) or not rule_id.strip():
            error(path + ".id", "Rule ID is required")
        elif rule_id in rule_ids:
            error(path + ".id", f"Duplicate rule ID: {rule_id}")
        else:
            rule_ids.add(rule_id)

        priority = rule.get("priority", 100)

        if isinstance(priority, bool) or not isinstance(priority, int):
            error(path + ".priority", "Priority must be an integer")
        elif priority < 0:
            error(path + ".priority", "Priority cannot be negative")
        else:
            priorities.setdefault(priority, []).append(rule_id or path)

        check_condition(rule.get("when", {}), path + ".when")

        adjustment = rule.get("then")

        if not isinstance(adjustment, dict):
            error(path + ".then", "Adjustment is required")
        else:
            kind = adjustment.get("type")

            if kind not in SUPPORTED_ADJUSTMENTS:
                error(path + ".then.type", f"Unsupported adjustment: {kind}")

            value = adjustment.get("value")

            if not _is_number(value):
                error(path + ".then.value", "Adjustment value must be numeric")
            elif kind == "multiplier" and float(value) < 0:
                error(path + ".then.value", "Multiplier cannot be negative")

        for field in ("effective_from", "effective_to"):
            value = rule.get(field)

            if value is not None:
                try:
                    if not isinstance(value, str):
                        raise ValueError
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    error(path + "." + field, "Date must use YYYY-MM-DD")

        start = rule.get("effective_from")
        end = rule.get("effective_to")

        if start and end:
            try:
                if date.fromisoformat(start) > date.fromisoformat(end):
                    error(
                        path,
                        "Effective start date must not be after end date",
                    )
            except (TypeError, ValueError):
                pass

        exclusive_group = rule.get("exclusive_group")

        if exclusive_group is not None and (
            not isinstance(exclusive_group, str) or not exclusive_group.strip()
        ):
            error(
                path + ".exclusive_group",
                "Exclusive group must be a non-empty string",
            )

        if "stop_processing" in rule and not isinstance(
            rule["stop_processing"], bool
        ):
            error(path + ".stop_processing", "stop_processing must be a boolean")

    for priority, ids in priorities.items():
        if len(ids) > 1:
            warnings.append({
                "path": "rules",
                "message": (
                    f"Rules {ids} share priority {priority}; "
                    "check for conflicts"
                ),
            })

    # Constraints
    constraints = config.get("constraints", {})

    if not isinstance(constraints, dict):
        error("constraints", "Constraints must be an object")
        constraints = {}

    minimum = constraints.get("min_price")
    maximum = constraints.get("max_price")

    for key, value in (
        ("min_price", minimum),
        ("max_price", maximum),
    ):
        if value is not None:
            if not _is_number(value):
                error(
                    f"constraints.{key}",
                    "Price constraint must be numeric",
                )
            elif float(value) < 0:
                error(
                    f"constraints.{key}",
                    "Price constraint cannot be negative",
                )

    if minimum is not None and maximum is not None:
        if _is_number(minimum) and _is_number(maximum):
            if float(minimum) > float(maximum):
                error("constraints", "min_price cannot exceed max_price")

    # Strategy and rounding
    strategy = config.get("strategy", {})

    if not isinstance(strategy, dict):
        error("strategy", "Strategy must be an object")
        strategy = {}

    stacking = strategy.get("stacking", "sequential")

    if stacking not in SUPPORTED_STACKING_MODES:
        error(
            "strategy.stacking",
            f"Unsupported stacking mode: {stacking}",
        )

    rounding = strategy.get("rounding", {})

    if not isinstance(rounding, dict):
        error("strategy.rounding", "Rounding configuration must be an object")
        rounding = {}

    step = rounding.get("step", 1)

    if not _is_number(step):
        error("strategy.rounding.step", "Rounding step must be numeric")
    elif float(step) <= 0:
        error("strategy.rounding.step", "Rounding step must be positive")

    rounding_mode = rounding.get("mode", "nearest")

    if rounding_mode not in {"nearest", "up", "down"}:
        error(
            "strategy.rounding.mode",
            "Rounding mode must be nearest, up, or down",
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
