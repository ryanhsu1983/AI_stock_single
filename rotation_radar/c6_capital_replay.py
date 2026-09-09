"""Bounded opening-capital replay for the accepted buy-and-hold forward segment.

Refuses later sells, withdrawals or corporate actions: those require full event replay.
Uses existing exact official execution prices, recomputing whole shares and costs.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
from .c6_account_basis import INITIAL_CAPITAL


def rebase_opening_segment(payload):
    result = deepcopy(payload)
    events = result['ledger_rows']
    if any(r['event_type'] not in {'buy', 'daily_mark'} for r in events):
        raise ValueError('Full event replay required after sells, withdrawals or corporate actions')
    if result['accounting_snapshot_as_of'] > '2026-09-08':
        raise ValueError('Migration is bounded through 2026-09-08')
    cash = {i: Decimal(str(INITIAL_CAPITAL))/3 for i in (1, 2, 3)}
    state = {}
    for row in events:
        slot = int(row['slot_id'])
        close = Decimal(str(row['raw_close']))
        if close <= 0:
            raise ValueError('Missing exact execution or holding mark')
        if row['event_type'] == 'buy':
            if slot in state:
                raise ValueError('Repeated buy requires full event replay')
            execution = close * Decimal('1.001')
            unit_cost = execution * Decimal('1.000855')
            shares = int((cash[slot]/unit_cost).to_integral_value(rounding=ROUND_FLOOR))
            gross = execution*shares
            fee = gross*Decimal('.000855')
            basis = gross+fee
            cash[slot] -= basis
            state[slot] = (shares, basis)
            row.update(shares=shares, gross_amount=float(gross), transaction_cost=float(fee), net_amount=-float(basis))
        else:
            shares, basis = state[slot]
            row.update(shares=shares, gross_amount=float(close*shares), relative_return_pct=float(close*shares/basis-1))
        row['cash_after'] = float(cash[slot])
    for slot in result['slots']:
        shares, basis = state[int(slot['slot_id'])]
        slot.update(shares=shares, position_cost=float(basis), slot_cash=float(cash[int(slot['slot_id'])]))
    result['cash'] = float(sum(cash.values()))
    result['initial_capital'] = INITIAL_CAPITAL
    result['capital_migration'] = {'approved_date': '2026-09-09', 'start': '2026-08-05',
        'end': result['accounting_snapshot_as_of'], 'whole_share_recomputed': True,
        'buy_commission': .000855, 'buy_slippage': .001,
        'event_coverage_status': 'retained_pending_not_upgraded', 'signal_rules_changed': False}
    return result
