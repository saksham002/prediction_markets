"""StrategyConfig: the decision-strategy spec the market-maker reads.

A flat, SYMMETRIC list of alpha gates (each: full alpha name + threshold + an
AlphaConfig carrying the dynamic half-life) plus the sizing / risk / budget knobs.
All gates are used identically: an order side is placed only if EVERY gate passes
its threshold for that side (equivalently, BLOCKED if ANY gate crosses it). There
is no "primary" alpha — variation lives only in each gate's threshold/half-life.

Half-lives are config here (not a fixed module-level set in alphas.py): each gate
carries its own HL via AlphaConfig, and the alpha engine maintains EMAs only for
the referenced HLs (see half_lives()).
"""
from __future__ import annotations

from dataclasses import dataclass

from research.hft.passive_fill import FORWARD_DELAY_S

# HL-parameterized alpha families -> the engine component that maintains the EMA(s)
# for them. Ordered LONGEST-PREFIX-FIRST so parse_alpha_name matches correctly
# (e.g. "agg_dev_10s" -> agg_dev, "agg_pw_ratio_60s" -> agg, not "agg_").
_HL_FAMILIES: list[tuple[str, str]] = [
    ("obi_dev", "obi_ma"),
    ("obi_ma", "obi_ma"),
    ("agg_dev", "agg_dev"),          # ALSO needs the base "agg" EMA at the same HL
    ("agg_pw_ratio", "agg"),
    ("agg_ratio", "agg"),
    ("agg_pw", "agg"),
    ("agg", "agg"),
    ("tfma_pw_ratio", "tfma"),
    ("tfma_ratio", "tfma"),
    ("tfma_pw", "tfma"),
    ("tfma", "tfma"),
    ("mom", "mom"),
]
_COMPONENT = dict(_HL_FAMILIES)


def hl_label(half_life: float) -> str:
    """60 -> '60s', 1 -> '1s', 0.5 -> '0.5s' (matches the existing label strings)."""
    return f"{half_life:g}s"


def parse_hl_label(label: str) -> float:
    """'60s' -> 60.0."""
    return float(label[:-1] if label.endswith("s") else label)


def parse_alpha_name(name: str) -> tuple[str, str]:
    """Split a full alpha name into (family, hl_label). Non-HL alphas (obi, agree,
    combo, ...) return (name, '')."""
    for fam, _comp in _HL_FAMILIES:
        if name.startswith(fam + "_"):
            return fam, name[len(fam) + 1:]
    return name, ""


@dataclass(frozen=True)
class AlphaConfig:
    """Per-alpha tuning carried by a gate; today just the dynamic half-life."""
    half_life: float

    @property
    def label(self) -> str:
        return hl_label(self.half_life)


@dataclass(frozen=True)
class AlphaGate:
    """One alpha used as a (symmetric) order gate: its full name, its threshold,
    and its HL config (None for non-HL alphas like 'obi'/'agree_om')."""
    name: str
    threshold: float
    config: AlphaConfig | None = None

    @staticmethod
    def from_spec(spec: dict) -> "AlphaGate":
        """Accepts {'name','threshold'} or {'family','hl','threshold'}."""
        if "name" in spec:
            name = spec["name"]
            _fam, label = parse_alpha_name(name)
            hl = parse_hl_label(label) if label else None
        else:
            hl = float(spec["hl"])
            name = f"{spec['family']}_{hl_label(hl)}"
        cfg = AlphaConfig(hl) if hl is not None else None
        return AlphaGate(name = name, threshold = float(spec["threshold"]), config = cfg)

    @property
    def family(self) -> str:
        return parse_alpha_name(self.name)[0]


@dataclass(frozen=True)
class StrategyConfig:
    """Everything the strategy's decision path reads. Built once per run via
    from_params(); runtime/wiring (timing_emit, live, tickers, combo, game_starts,
    series, ...) stays on the raw params object, NOT here."""
    alphas: tuple[AlphaGate, ...] = ()
    per_order_size: float = 1000
    inventory_cap: float = 3000
    max_spread: float = 0.01
    price_min: float = 0.05
    price_max: float = 0.95
    improve: bool = False
    pair_risk: bool = True
    square_off: bool = False
    size_ref: float | None = None
    per_leg_alpha: bool = False
    losing_leg_bias: float = 0.0
    liquidate_no_alpha: bool = False
    football: bool = False
    budget: float | None = 1000
    free_budget: bool = False
    forward_delay: float = FORWARD_DELAY_S
    aggro_entry: float | None = None
    aggro_limit: float = 300
    aggro_profit: float = 0.02
    aggro_stop: float = 0.05
    aggro_cross: bool = False
    aggro_neg: float | None = None
    ack_delay: float = 0.0
    pub_delay: float = 0.0
    fill_delay: float = 0.0
    fill_pub_lag: float = 0.0

    @property
    def alpha_name(self) -> str:
        """The first gate's name — for logging + the optional alpha-proportional
        sizing (size_ref/improve). NOT a gating primary (all gates are symmetric)."""
        return self.alphas[0].name if self.alphas else "agree_om"

    @property
    def primary(self) -> AlphaGate | None:
        return self.alphas[0] if self.alphas else None

    def track_agg(self) -> bool:
        return any("agg" in g.name for g in self.alphas)

    def track_obi_ma(self) -> bool:
        return any(("obi_ma" in g.name or "obi_dev" in g.name) for g in self.alphas)

    def half_lives(self) -> dict[str, dict[str, float]] | None:
        """Per-component {label: seconds} buckets the engine should maintain, or
        None to use the module defaults. None is returned when ANY gate is a non-HL
        / unrecognized family (agree/combo/obi) — those need the full default
        component sets (transitive deps), so we never silently drop them."""
        buckets: dict[str, dict[str, float]] = {}
        for g in self.alphas:
            fam, label = parse_alpha_name(g.name)
            comp = _COMPONENT.get(fam)
            if comp is None or not label:
                return None              # non-HL / unrecognized -> safe full defaults
            hl = parse_hl_label(label)
            buckets.setdefault(comp, {})[label] = hl
            if fam == "agg_dev":         # agg_dev = agg - EMA(agg): needs the base agg EMA too
                buckets.setdefault("agg", {})[label] = hl
        return buckets

    @classmethod
    def from_params(cls, params) -> "StrategyConfig":
        """Bridge a SimpleNamespace / argparse.Namespace into a StrategyConfig. The
        strategy spec is the `alphas` list (each {name|family, hl, threshold}); CLI
        entry points synthesize it from -a/-t via ensure_alphas() before calling this."""
        g = lambda k, d = None: getattr(params, k, d)
        spec = g("alphas", None)
        if not spec:
            raise ValueError("StrategyConfig.from_params needs an 'alphas' list "
                             "(CLI entry points call ensure_alphas(params) to build it from -a/-t)")
        gates = tuple(AlphaGate.from_spec(s) for s in spec)
        return cls(
            alphas = gates,
            per_order_size = g("per_order_size", 1000),
            inventory_cap = g("inventory_cap", 3000),
            max_spread = g("max_spread", 0.01),
            price_min = g("price_min", 0.05),
            price_max = g("price_max", 0.95),
            improve = g("improve", False),
            pair_risk = g("pair_risk", True),
            square_off = g("square_off", False),
            size_ref = g("size_ref", None),
            per_leg_alpha = g("per_leg_alpha", False),
            losing_leg_bias = g("losing_leg_bias", 0.0),
            liquidate_no_alpha = g("liquidate_no_alpha", False),
            football = g("football", False),
            budget = g("budget", 1000),
            free_budget = g("free_budget", False),
            forward_delay = g("forward_delay", FORWARD_DELAY_S),
            aggro_entry = g("aggro_entry", None),
            aggro_limit = g("aggro_limit", 300),
            aggro_profit = g("aggro_profit", 0.02),
            aggro_stop = g("aggro_stop", 0.05),
            aggro_cross = g("aggro_cross", False),
            aggro_neg = g("aggro_neg", None),
            ack_delay = g("ack_delay", 0.0),
            pub_delay = g("pub_delay", 0.0),
            fill_delay = g("fill_delay", 0.0),
            fill_pub_lag = g("fill_pub_lag", 0.0),
        )


def ensure_alphas(params):
    """CLI convenience for single-alpha entry points: if `params` has no `alphas`
    list, synthesize a one-gate list from the -a/-t args (alpha_name/skew_threshold).
    from_params REQUIRES `alphas`; this is the only place the -a/-t shorthand is
    bridged into the canonical list form."""
    if not getattr(params, "alphas", None):
        params.alphas = [{"name": getattr(params, "alpha_name", "agree_om"),
                          "threshold": getattr(params, "skew_threshold", 0.0)}]
    return params
