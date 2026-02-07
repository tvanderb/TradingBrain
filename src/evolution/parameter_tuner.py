"""Strategy parameter validation and bounds enforcement.

Ensures the Executive Brain's parameter adjustments stay within safe ranges.
"""

from __future__ import annotations

# Allowed ranges for each parameter
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "momentum.lookback": (5, 50),
    "momentum.threshold": (0.3, 0.95),
    "momentum.weight": (0.0, 1.0),
    "mean_reversion.lookback": (10, 100),
    "mean_reversion.std_dev": (1.0, 4.0),
    "mean_reversion.weight": (0.0, 1.0),
    "volume.relative_threshold": (1.0, 5.0),
    "volume.weight": (0.0, 1.0),
    "trend.ema_fast": (3, 20),
    "trend.ema_slow": (10, 50),
    "trend.weight": (0.0, 1.0),
}


def validate_param(param_path: str, value: float) -> float | None:
    """Validate and clamp a parameter to its allowed range.

    Returns the clamped value, or None if the parameter path is unknown.
    """
    bounds = PARAM_BOUNDS.get(param_path)
    if bounds is None:
        return None
    lo, hi = bounds
    return max(lo, min(hi, value))


def validate_weights(params: dict) -> dict:
    """Ensure strategy weights sum to approximately 1.0.

    If they don't, normalize them.
    """
    weight_keys = ["momentum", "mean_reversion", "volume", "trend"]
    total = sum(params.get(k, {}).get("weight", 0) for k in weight_keys)

    if abs(total - 1.0) > 0.01 and total > 0:
        for k in weight_keys:
            if k in params and "weight" in params[k]:
                params[k]["weight"] = round(params[k]["weight"] / total, 3)

    return params
