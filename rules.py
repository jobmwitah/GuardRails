"""
Risk rule engine. Extend evaluate() to add more rules — each rule just
appends a command string ("BLOCK", "CLOSE_ALL", "CLOSE <ticket>") to
the returned list when it's breached.
"""

from dataclasses import dataclass


@dataclass
class RuleConfig:
    daily_loss_limit: float = 0.0     # $ — 0 disables
    max_drawdown_pct: float = 0.0     # % trailing from equity peak — 0 disables
    max_lot_size: float = 0.0         # per-position cap — 0 disables
    max_positions: int = 0            # concurrent positions — 0 disables


@dataclass
class RuleState:
    day_start_balance: float = 0.0
    peak_equity: float = 0.0
    already_blocked: bool = False


def evaluate(config: RuleConfig, state: RuleState, report: dict) -> list[str]:
    """Given the latest EA report, return any new commands to queue."""
    commands = []
    equity = float(report.get("equity", 0) or 0)
    balance = float(report.get("balance", 0) or 0)

    if state.peak_equity == 0:
        state.peak_equity = equity
    state.peak_equity = max(state.peak_equity, equity)

    if state.day_start_balance == 0:
        state.day_start_balance = balance

    breached = False

    if config.daily_loss_limit > 0:
        loss = state.day_start_balance - equity
        if loss >= config.daily_loss_limit:
            breached = True

    if config.max_drawdown_pct > 0 and state.peak_equity > 0:
        dd_pct = (state.peak_equity - equity) / state.peak_equity * 100
        if dd_pct >= config.max_drawdown_pct:
            breached = True

    if breached and not state.already_blocked:
        commands.append("BLOCK")
        state.already_blocked = True
    elif not breached:
        state.already_blocked = False

    # Max lot size per open position
    if config.max_lot_size > 0:
        for pos in (p for p in report.get("positions", "").split("|") if p):
            symbol, ticket, lots, profit = pos.split(",")
            if float(lots) > config.max_lot_size:
                commands.append(f"CLOSE {ticket}")

    return commands


def reset_day(state: RuleState, current_balance: float) -> None:
    """Call this on a scheduler at your local midnight to reset the daily counter."""
    state.day_start_balance = current_balance
    state.already_blocked = False
