"""Deprecated: use core.broker.MT5Broker instead."""

from core.broker.mt5_adapter import MT5Broker


class MT5Client(MT5Broker):
    pass
