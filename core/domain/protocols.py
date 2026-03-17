from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class StrategyExecutionProtocol(Protocol):
    symbol: str
    magic: int
    enabled: bool

    def on_tick(self, ctx: Any, *, action_collector: Optional[list] = None) -> Any: ...
    def update(self, **kwargs: Any) -> Any: ...

    def _send_with_fillings(self, request: Dict[str, Any]) -> Any: ...
    def _handle_order_error(self, retcode: int, comment: str, price: Any) -> None: ...
    def _on_order_submit_success(self) -> None: ...


class ExecutionGatewayProtocol(Protocol):
    @property
    def lock(self) -> Any: ...

    def order_send(self, request: Dict[str, Any]) -> Any: ...

