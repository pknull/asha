# GitHub Copilot Prompt Template

## How Copilot Works

Autocomplete-first — reads your open file and cursor position as primary context. It completes what it predicts, not what you intend.

## Best Practice

Write the exact function signature, docstring, or comment BEFORE invoking.

## Structure

```python
def function_name(param1: Type1, param2: Type2) -> ReturnType:
    """
    [One-line description of what function does].

    Args:
        param1: [Description, constraints, edge cases]
        param2: [Description, constraints, edge cases]

    Returns:
        [Description of return value]

    Raises:
        [Exceptions that can be raised]

    Note:
        - [What function must NOT do]
        - [Edge case handling]
    """
    # [Optional: inline comment about approach]
```

Then let Copilot complete.

## Example

```python
def calculate_shipping_cost(weight_kg: float, destination: str) -> float:
    """
    Calculate shipping cost based on weight and destination zone.

    Args:
        weight_kg: Package weight in kilograms. Must be > 0 and <= 50.
        destination: Two-letter country code (ISO 3166-1 alpha-2).

    Returns:
        Shipping cost in USD, rounded to 2 decimal places.

    Raises:
        ValueError: If weight is out of range or destination invalid.

    Note:
        - Do NOT apply discounts (handled elsewhere)
        - Use ZONE_RATES constant for pricing
    """
```

## Common Mistakes

- Vague or missing docstring
- No type hints
- Leaving ambiguity about edge cases
- Not specifying what function should NOT do
