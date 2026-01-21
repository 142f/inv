"""Broker abstraction layer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BrokerBase(ABC):
    """Abstract base class for broker adapters."""

    @property
    @abstractmethod
    def lock(self) -> Any:
        """Shared lock for broker interactions (RLock recommended)."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if connected to the broker/terminal."""

    @abstractmethod
    def initialize(self) -> bool:
        """
        Initialize broker connection.
        Returns:
            bool: True if successful, False otherwise.
        """

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown broker connection and clean up resources."""

    @abstractmethod
    def ensure_symbol(self, symbol: str) -> bool:
        """
        Ensure a symbol is available and selected for data feed.
        """

    @abstractmethod
    def account_info(self) -> Any:
        """Return account info from broker."""

    @abstractmethod
    def terminal_info(self) -> Any:
        """Return terminal info from broker."""

    @abstractmethod
    def orders_get(self) -> Any:
        """Return active orders."""

    @abstractmethod
    def positions_get(self) -> Any:
        """Return active positions."""

    @abstractmethod
    def symbol_info_tick(self, symbol: str) -> Any:
        """Return latest tick for symbol."""

    @abstractmethod
    def order_send(self, request: dict) -> Any:
        """Send a trade request."""

    @abstractmethod
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        """Return rates from position."""
