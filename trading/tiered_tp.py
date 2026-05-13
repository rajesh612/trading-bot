"""
Tiered take-profit logic for swing positions.
Pure — no Alpaca calls, no file I/O.
"""


def _compute_tier_quantities(total_qty: int) -> tuple[int, int, int]:
    """
    Split total_qty into (tp1_qty, tp2_qty, trailing_qty) at 40/30/30.
    Guarantees whole shares that sum to total_qty.
    """
    if total_qty <= 1:
        return total_qty, 0, 0
    if total_qty == 2:
        return 1, 1, 0
    tp1 = max(1, round(total_qty * 0.4))
    tp2 = max(1, round(total_qty * 0.3))
    trailing = total_qty - tp1 - tp2
    if trailing <= 0:
        tp2 = max(1, total_qty - tp1 - 1)
        trailing = total_qty - tp1 - tp2
    if trailing <= 0:
        trailing = 1
        tp2 = total_qty - tp1 - trailing
    return tp1, tp2, trailing


def evaluate_tiered_tp_action(position: dict, current_price: float) -> dict:
    """
    Priority: hard stop > TP1 > TP2 (activates trailing) > trailing drawdown.
    Returns {action, qty, activate_trailing, reason}.
    action ∈ NONE | CLOSE_TP1 | CLOSE_TP2 | CLOSE_TRAILING | CLOSE_HARD_STOP

    Pure — no Alpaca calls, no file I/O.
    """
    entry = position["entry_price"]
    pnl_pct = (current_price - entry) / entry * 100

    sl_pct = position.get("stop_loss_pct", 7.0)
    tp1_pct = position.get("tp1_pct", 5.0)
    tp2_pct = position.get("tp2_pct", 10.0)

    # Hard stop (entire remaining position)
    remaining_qty = (
        position.get("trailing_qty", 0)
        + (0 if position.get("tp1_hit") else position.get("tp1_qty", 0))
        + (0 if position.get("tp2_hit") else position.get("tp2_qty", 0))
    )
    if pnl_pct <= -sl_pct:
        return {"action": "CLOSE_HARD_STOP", "qty": remaining_qty, "activate_trailing": False,
                "reason": f"hard stop at {pnl_pct:.2f}%"}

    # TP1: close tp1_qty shares
    if not position.get("tp1_hit") and pnl_pct >= tp1_pct:
        tp1_qty = position.get("tp1_qty", 0)
        if tp1_qty > 0:
            return {"action": "CLOSE_TP1", "qty": tp1_qty, "activate_trailing": False,
                    "reason": f"TP1 at {pnl_pct:.2f}%"}

    # TP2: close tp2_qty and activate trailing
    if position.get("tp1_hit") and not position.get("tp2_hit") and pnl_pct >= tp2_pct:
        tp2_qty = position.get("tp2_qty", 0)
        if tp2_qty > 0:
            return {"action": "CLOSE_TP2", "qty": tp2_qty, "activate_trailing": True,
                    "reason": f"TP2 at {pnl_pct:.2f}%, activating trailing stop"}

    # Trailing stop: 3% drawdown from high water after TP2 hit
    if position.get("tp2_hit") and position.get("trailing_stop_active"):
        high_water = position.get("trailing_high_water", entry)
        trailing_qty = position.get("trailing_qty", 0)
        if trailing_qty > 0 and current_price <= high_water * 0.97:
            return {"action": "CLOSE_TRAILING", "qty": trailing_qty, "activate_trailing": False,
                    "reason": f"trailing stop at {pnl_pct:.2f}% ({current_price:.2f} vs HW {high_water:.2f})"}

    return {"action": "NONE", "qty": 0, "activate_trailing": False, "reason": ""}
