"""Cash-flow-neutral position accounting shared by C6 account consumers."""
from decimal import Decimal

INITIAL_CAPITAL = 7_676_961.04
INITIAL_EXISTING_CASH = 198_073.04


def allocate_sale_basis(position_cost: float, held_shares: int, sold_shares: int) -> tuple[float, float]:
    """Allocate acquisition cost by units, never by withdrawal proceeds."""
    if held_shares <= 0 or not 0 < sold_shares <= held_shares or position_cost <= 0:
        raise ValueError('Invalid disposal quantity or acquisition cost')
    basis = Decimal(str(position_cost))
    sold = basis * Decimal(sold_shares) / Decimal(held_shares)
    return float(sold), float(basis - sold)
