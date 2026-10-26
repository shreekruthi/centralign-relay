"""Small explicit goal contract; model output never becomes executable code."""

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen

PRODUCTS = {"keyboard": "KEYBOARD", "mouse": "MOUSE", "monitor": "MONITOR"}
SCENARIOS = {"normal", "supplier_outage", "transient", "crash_after_order", "verification_mismatch", "timeout_after_commit"}


def parse_partial(text):
    """Recognize a bounded, underspecified goal without inventing missing values."""
    if not isinstance(text, str) or not 1 <= len(text) <= 500:
        raise ValueError("Use a goal of 1–500 characters.")
    match = re.fullmatch(
        r"\s*(?:buy|restock|purchase)\s+(?:(\d+)\s+)?(keyboards?|mice|mouse|monitors?)"
        r"(?:\s+(?:within|under|with a budget of|for at most)\s+\$([\d,]+(?:\.\d{1,2})?))?\s*[.!]?\s*",
        text, re.I,
    )
    if not match:
        raise ValueError('Try "Restock keyboards" or "Buy 12 keyboards within $900". Other instructions are unsupported.')
    qty, noun, money = match.groups()
    noun = noun.lower()
    result = {"sku": "MOUSE" if noun in {"mouse", "mice"} else PRODUCTS[noun.rstrip("s")]}
    if qty is not None:
        result["quantity"] = int(qty)
    if money is not None:
        try:
            result["budget_cents"] = int(Decimal(money.replace(",", "")) * 100)
        except InvalidOperation as exc:
            raise ValueError("Invalid budget.") from exc
    # Validate supplied values even while other fields are missing.
    validate_goal({"quantity":1, "budget_cents":1, **result})
    return result


def validate_goal(value):
    if not isinstance(value, dict) or set(value) != {"sku", "quantity", "budget_cents"}:
        raise ValueError("Goal must contain exactly sku, quantity and budget_cents.")
    if value["sku"] not in PRODUCTS.values():
        raise ValueError("Supported products: keyboards, mice, monitors.")
    if type(value["quantity"]) is not int or not 1 <= value["quantity"] <= 100:
        raise ValueError("Quantity must be a whole number from 1 to 100.")
    if type(value["budget_cents"]) is not int or not 1 <= value["budget_cents"] <= 10_000_000:
        raise ValueError("Budget must be between $0.01 and $100,000.")
    return dict(value)


def parse_goal(text):
    """Deliberately bounded grammar: buy/restock N product within $B."""
    if not isinstance(text, str) or len(text) > 500:
        raise ValueError("Use a goal of at most 500 characters.")
    match = re.fullmatch(
        r"\s*(?:buy|restock|purchase)\s+(\d+)\s+(keyboards?|mice|mouse|monitors?)"
        r"\s+(?:within|under|with a budget of|for at most)\s+\$([\d,]+(?:\.\d{1,2})?)\s*[.!]?\s*",
        text, re.I,
    )
    if not match:
        raise ValueError('Try: "Restock 12 keyboards within $900". Use the supported grammar or local-model mode.')
    quantity, noun, money = match.groups()
    noun = noun.lower()
    sku = "MOUSE" if noun in {"mice", "mouse"} else PRODUCTS[noun.rstrip("s")]
    try:
        cents = int(Decimal(money.replace(",", "")) * 100)
    except InvalidOperation as exc:
        raise ValueError("Invalid budget.") from exc
    return validate_goal({"sku": sku, "quantity": int(quantity), "budget_cents": cents})


def parse_with_ollama(text, model):
    """Optional local interpretation only. No remote endpoints, keys or autonomous shell."""
    if not isinstance(text, str) or not 1 <= len(text) <= 500:
        raise ValueError("Use a goal of 1–500 characters.")
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"sku": {"type": "string", "enum": list(PRODUCTS.values())},
                       "quantity": {"type": "integer"}, "budget_cents": {"type": "integer"}},
        "required": ["sku", "quantity", "budget_cents"],
    }
    body = {"model": model, "stream": False, "format": schema,
            "messages": [{"role": "system", "content":
                          "Extract an explicit purchase goal. USD only. Do not invent quantities or budgets. "
                          "If anything is missing or unsupported, return {}. Output only JSON."},
                         {"role": "user", "content": text}], "options": {"temperature": 0}}
    request = Request("http://127.0.0.1:11434/api/chat", json.dumps(body).encode(),
                      {"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=60) as response:
            data = json.load(response)
        return validate_goal(json.loads(data["message"]["content"]))
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError("Local model unavailable or returned an invalid goal. Use offline mode or check Ollama.") from exc
