"""Leave requests, decisions on them, and what the history says.

A local ledger rather than a connector to somebody's HR system, on purpose.
Every vendor's API is their own scopes and their own rate limits, and the part
that is portable is the decisions *you* have made and the policy you said out
loud. See `openmirror/hr/store.py` for the reasoning, and for why an install
that wants a real HR system should reach it through a
[hook](../HOOKS.md) rather than through code here.
"""

from __future__ import annotations

from openmirror.hr.store import MIN_FOR_A_BAND, Ledger, Request_, StoreError, span_days

__all__ = ['MIN_FOR_A_BAND', 'Ledger', 'Request_', 'StoreError', 'span_days']
