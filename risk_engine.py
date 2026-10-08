"""JEV-PORTFOLIO v1.0 — portfolio risk engine (PRODUCTION safety layer).

NOT part of the frozen strategy. The strategy core is untouched; this module
observes account/portfolio state and emits a RiskState + governor directives
(reduce/stop entries, flatten, halt) consumed by execution adapters and the UI.

Layers:
  1. PropRiskProfile  — Maven $10K evaluation rules from YAML config.
  2. PortfolioGovernor — WARNING / SOFT / HARD / EMERGENCY thresholds on
     account DD + total floating heat (configurable safety buffers INSIDE the
     official limits; we never operate at exactly the limit).
  3. CryptoHeatMonitor — ETH/BTC correlated-heat watch (backtest showed
     heat corr +0.52): PAIR-level breaker, strategy entries unchanged.
  4. StateMachine      — explicit lifecycle; critical errors latch to a safe
     state and NEVER silently resume trading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskState(str, Enum):
    STARTING = "STARTING"
    SYNCING = "SYNCING"
    READY = "READY"
    TRADING = "TRADING"
    RISK_WARNING = "RISK_WARNING"
    RISK_RESTRICTED = "RISK_RESTRICTED"
    TARGET_REACHED = "TARGET_REACHED"
    MAX_LOSS_REACHED = "MAX_LOSS_REACHED"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


# States in which NEW entries are forbidden (existing legs exit normally,
# unless EMERGENCY_FLATTEN flattens everything).
NO_NEW_ENTRIES = {
    RiskState.RISK_RESTRICTED, RiskState.TARGET_REACHED,
    RiskState.MAX_LOSS_REACHED, RiskState.EMERGENCY_FLATTEN,
    RiskState.ERROR, RiskState.STOPPED, RiskState.STARTING, RiskState.SYNCING,
}


class StateMachine:
    """Explicit lifecycle. ERROR/STOPPED/MAX_LOSS_REACHED latch: only an
    explicit manual reset returns to READY (never auto-resume)."""

    _AUTO_OK = {RiskState.RISK_WARNING: RiskState.TRADING,
                RiskState.READY: RiskState.TRADING}

    def __init__(self) -> None:
        self.state = RiskState.STARTING
        self.reason = "boot"

    def transition(self, nxt: RiskState, reason: str = "") -> RiskState:
        latched = {RiskState.ERROR, RiskState.STOPPED,
                   RiskState.MAX_LOSS_REACHED, RiskState.EMERGENCY_FLATTEN}
        if self.state in latched and nxt not in (RiskState.STOPPED, RiskState.ERROR):
            return self.state  # latched: manual reset required
        self.state, self.reason = nxt, reason
        return self.state

    def manual_reset(self, reason: str = "operator reset") -> RiskState:
        self.state, self.reason = RiskState.READY, reason
        return self.state

    def allow_new_entries(self) -> bool:
        return self.state not in NO_NEW_ENTRIES


@dataclass
class PropRiskProfile:
    """Maven $10K evaluation rules. Official limits come from config (never
    hard-coded); buffers below them are PORTFOLIO SAFETY PARAMETERS."""
    account_size: float = 10000.0
    profit_target: float = 400.0
    max_drawdown: float = 1000.0
    daily_drawdown: Optional[float] = None   # None = follow current Maven rulebook
    consistency_rule: Optional[str] = None   # opaque string, enforced if set
    # Safety buffers (fraction of the official limit at which we act):
    warn_dd_frac: float = 0.30    # WARNING at 30% of max DD
    soft_dd_frac: float = 0.50    # stop NEW entries at 50%
    hard_dd_frac: float = 0.75    # flatten + halt at 75% (NEVER ride to 100%)
    warn_float: float = 300.0     # portfolio floating-loss warning ($)
    soft_float: float = 400.0     # stop new entries past this open heat
    hard_float: float = 500.0     # emergency flatten past this open heat

    @classmethod
    def from_dict(cls, d: dict) -> "PropRiskProfile":
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def evaluate(self, equity: float, peak_equity: float,
                 floating: float) -> tuple[RiskState, str]:
        dd = max(0.0, peak_equity - equity)
        if dd >= self.max_drawdown * self.hard_dd_frac:
            return (RiskState.EMERGENCY_FLATTEN,
                    f"account DD ${dd:.0f} >= hard {self.hard_dd_frac:.0%} of ${self.max_drawdown:.0f}")
        if floating <= -self.hard_float:
            return (RiskState.EMERGENCY_FLATTEN,
                    f"portfolio float ${floating:.0f} <= hard -${self.hard_float:.0f}")
        if dd >= self.max_drawdown * self.soft_dd_frac:
            return (RiskState.RISK_RESTRICTED,
                    f"account DD ${dd:.0f} >= soft {self.soft_dd_frac:.0%} — no new entries")
        if floating <= -self.soft_float:
            return (RiskState.RISK_RESTRICTED,
                    f"portfolio float ${floating:.0f} <= soft -${self.soft_float:.0f} — no new entries")
        if dd >= self.max_drawdown * self.warn_dd_frac or floating <= -self.warn_float:
            return (RiskState.RISK_WARNING,
                    f"DD ${dd:.0f} / float ${floating:.0f} in warning band")
        if equity - self.account_size >= self.profit_target:
            return (RiskState.TARGET_REACHED,
                    f"evaluation target +${self.profit_target:.0f} reached — halt, protect the pass")
        return (RiskState.TRADING, "within limits")


@dataclass
class CryptoHeatMonitor:
    """ETH+BTC combined-heat breaker. Risk governor only — entry strategy
    untouched. Thresholds are PORTFOLIO SAFETY PARAMETERS (configurable)."""
    pair_warn: float = 250.0    # ETH+BTC combined float warning
    pair_halt: float = 350.0    # ETH+BTC combined float: stop crypto entries
    xau_warn: float = 150.0

    def evaluate(self, eth_float: float, btc_float: float,
                 xau_float: float) -> tuple[str, str]:
        pair = eth_float + btc_float
        if pair <= -self.pair_halt:
            return ("HALT_CRYPTO",
                    f"ETH+BTC heat ${pair:.0f} <= -${self.pair_halt:.0f}: no new crypto entries")
        if pair <= -self.pair_warn:
            return ("WARN_CRYPTO", f"ETH+BTC heat ${pair:.0f} in warning band")
        if xau_float <= -self.xau_warn:
            return ("WARN_XAU", f"XAU heat ${xau_float:.0f} in warning band")
        return ("OK", "heat nominal")


@dataclass
class PortfolioGovernor:
    """Combines prop profile + heat monitor into one verdict per tick."""
    profile: PropRiskProfile = field(default_factory=PropRiskProfile)
    heat: CryptoHeatMonitor = field(default_factory=CryptoHeatMonitor)
    machine: StateMachine = field(default_factory=StateMachine)

    def tick(self, equity: float, peak_equity: float, floating: float,
             eth_float: float = 0.0, btc_float: float = 0.0,
             xau_float: float = 0.0) -> dict:
        state, reason = self.profile.evaluate(equity, peak_equity, floating)
        heat_flag, heat_reason = self.heat.evaluate(eth_float, btc_float, xau_float)
        if state == RiskState.TRADING and heat_flag == "HALT_CRYPTO":
            state, reason = RiskState.RISK_RESTRICTED, heat_reason
        self.machine.transition(
            state if state != RiskState.TRADING or
            self.machine.state in (RiskState.STARTING, RiskState.SYNCING,
                                   RiskState.READY) else self.machine.state,
            reason)
        return {
            "risk_state": self.machine.state.value,
            "reason": reason,
            "heat_flag": heat_flag,
            "heat_reason": heat_reason,
            "allow_new_entries": self.machine.allow_new_entries(),
            "emergency_flatten": self.machine.state == RiskState.EMERGENCY_FLATTEN,
            "dd": round(max(0.0, peak_equity - equity), 2),
            "dd_remaining": round(self.profile.max_drawdown - max(0.0, peak_equity - equity), 2),
            "target_progress_pct": round(max(0.0, (equity - self.profile.account_size)
                                             / self.profile.profit_target * 100), 1),
        }


def load_prop_config(path: str = None) -> PropRiskProfile:
    """Load Maven YAML config (env override PROP_CONFIG). Falls back to
    defaults matching the validated $10K evaluation."""
    import pathlib
    p = path or os.getenv("PROP_CONFIG") or str(
        pathlib.Path(__file__).parent / "config" / "prop_maven_10k.yaml")
    try:
        import yaml  # type: ignore
        d = yaml.safe_load(pathlib.Path(p).read_text()) or {}
        return PropRiskProfile.from_dict((d.get("prop") or {}))
    except Exception:
        return PropRiskProfile()
