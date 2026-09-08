"""Read-only valuation of user-confirmed holdings; never creates actual fills."""
from __future__ import annotations

import math
import re
from datetime import date


def valid_source_hash(value: object) -> bool:
    """Only an actual SHA256 digest is evidence, not stringified NaN."""
    return isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{64}', value) is not None


def holding_history_observation(*, ticker: str, entry_date: str, as_of: str,
                                official_rows: list[dict], trading_dates: list[str],
                                calendar_complete: bool) -> dict:
    """TD uses complete market sessions, never the number of available quotes.

    Raw high is a labelled price statistic, not corporate-action adjusted peak
    return or an accepted exit signal. Missing observations remain explicit.
    """
    if date.fromisoformat(entry_date) > date.fromisoformat(as_of):
        raise ValueError('Entry after observation date')
    sessions = sorted({d for d in trading_dates if entry_date <= d <= as_of})
    if not calendar_complete or not sessions or sessions[0] != entry_date or sessions[-1] != as_of:
        return {'holding_td': None, 'raw_close_high': None, 'missing_dates': [],
                'status': 'calendar_incomplete', 'exit_basis_ready': False}
    by_date = {}
    for row in official_rows:
        if str(row.get('ticker')) == ticker and row.get('date') in sessions:
            by_date.setdefault(row['date'], []).append(row)
    accepted = []
    missing = []
    for day in sessions:
        matches = by_date.get(day, [])
        close = matches[0].get('close') if len(matches) == 1 else None
        if (len(matches) != 1 or not valid_source_hash(matches[0].get('source_hash'))
                or isinstance(close, bool) or not isinstance(close, (int, float))
                or not math.isfinite(close) or close <= 0):
            missing.append(day)
        else:
            accepted.append(close)
    return {'holding_td': len(sessions),
            'raw_close_high': max(accepted) if not missing else None,
            'missing_dates': missing, 'status': 'price_history_incomplete' if missing else 'raw_history_complete',
            'exit_basis_ready': False}


def evaluate_exit_observation(*, current_return: float, holding_td: int,
                              peak_return: float | None,
                              macro_triple: bool | None) -> dict:
    """C6 observation only. Inputs must be supplied by accepted account history.

    Missing peak/macro evidence stays unknown, not a false no-exit result.
    No fills, units, costs or cash are changed by evaluating these conditions.
    """
    if isinstance(holding_td, bool) or not isinstance(holding_td, int) or holding_td < 1:
        raise ValueError('Invalid holding TD')
    if not math.isfinite(current_return) or current_return < -1:
        raise ValueError('Invalid current return')
    if peak_return is not None and (not math.isfinite(peak_return) or peak_return < current_return):
        raise ValueError('Invalid peak return')
    if macro_triple is not None and not isinstance(macro_triple, bool):
        raise ValueError('Invalid macro state')
    checks = {
        'hard_loss_guard': current_return <= -.12,
        'activated_peak_drawdown': (None if peak_return is None else
            peak_return >= .20 and (1 + current_return) / (1 + peak_return) - 1 <= -.12),
        'first_growth_review_failed': holding_td >= 35 and current_return < 0,
        'second_growth_review_failed': holding_td >= 50 and current_return < .08,
        'maximum_holding_td': holding_td >= 60,
        'macro_triple': False if current_return < .15 else macro_triple,
    }
    triggered = [key for key, value in checks.items() if value is True]
    unknown = [key for key, value in checks.items() if value is None]
    return {'conditions': checks, 'triggered': triggered, 'unknown': unknown,
            'status': 'exit_observed' if triggered else ('incomplete' if unknown else 'hold_observed'),
            'actual_trades_changed': False}


def value_account(holdings: list[dict], market_rows: list[dict], as_of: str,
                  cash: float, cash_confirmed: bool, event_coverage: dict) -> dict:
    """Require exact official marks and per-position event coverage for NAV.

    Coverage must start no later than entry and finish at this session. An
    inventory, keyword search, or a previous-session certificate is insufficient.
    Cash estimates are always provisional, even with complete marks/events.
    """
    date.fromisoformat(as_of)
    if isinstance(cash, bool) or not isinstance(cash, (int, float)) or not math.isfinite(cash):
        raise ValueError('Invalid actual cash balance')
    seen = set()
    values = []
    blockers = []
    for holding in holdings:
        ticker = str(holding['ticker'])
        if ticker in seen:
            raise ValueError('Duplicate actual position')
        seen.add(ticker)
        units = holding['units']
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValueError('Actual units must be positive whole shares')
        entry = holding['entry_date']
        if date.fromisoformat(entry) > date.fromisoformat(as_of):
            raise ValueError('Actual position entry is in the future')
        rows = [row for row in market_rows if str(row.get('ticker')) == ticker
                and row.get('date') == as_of]
        price = rows[0].get('close') if len(rows) == 1 else None
        accepted = (not isinstance(price, bool) and isinstance(price, (int, float))
                    and math.isfinite(price) and price > 0
                    and valid_source_hash(rows[0].get('source_hash')) if len(rows) == 1 else False)
        if not accepted:
            blockers.append(f'{ticker}:exact_official_mark_missing_or_ambiguous')
        coverage = event_coverage.get(ticker, {})
        covered = (coverage.get('accepted') is True
                   and coverage.get('start') == entry
                   and coverage.get('end') == as_of
                   and bool(coverage.get('evidence_hash'))
                   and coverage.get('units_reconciled') == units)
        if not covered:
            blockers.append(f'{ticker}:holder_event_coverage_missing')
        values.append({'ticker': ticker, 'units': units,
                       'official_close': price if accepted else None,
                       'market_value': units * price if accepted and covered else None})
    nav = cash + sum(row['market_value'] for row in values) if not blockers else None
    return {'as_of': as_of, 'positions': values, 'nav': nav,
            'cash_confirmed': cash_confirmed, 'nav_is_provisional': not cash_confirmed,
            'status': 'blocked' if blockers else ('verified' if cash_confirmed else 'provisional_cash'),
            'blockers': blockers, 'actual_trades_changed': False}
