"""Strategy execution runtime: invoke strategy lifecycle and flush queued actions."""

from __future__ import annotations

from typing import Any, List, Optional

import MetaTrader5 as mt5

from core.domain.protocols import ExecutionGatewayProtocol, StrategyExecutionProtocol
from core.logger import Logger


class StrategyRuntime:
    def __init__(self, broker: ExecutionGatewayProtocol, *, use_action_queue: bool = False):
        self._broker = broker
        self._use_action_queue = bool(use_action_queue)

    def execute(self, strategy: StrategyExecutionProtocol, ctx: Any) -> bool:
        actions: List[Any] = []
        action_collector = actions if self._use_action_queue else None
        cycle_failed = False
        try:
            if hasattr(strategy, "on_tick"):
                strategy.on_tick(ctx, action_collector=action_collector)
            else:
                strategy.update(
                    orders_list=ctx.orders,
                    positions_list=ctx.positions,
                    tick=ctx.tick,
                    orders_filtered=True,
                    positions_filtered=True,
                    atr=ctx.atr,
                    action_collector=action_collector,
                )
        except Exception as exc:
            cycle_failed = True
            Logger.log(strategy.symbol, "异常", f"策略执行生命周期内发生崩溃 (magic={strategy.magic}): {exc}")

        if self._use_action_queue and (not cycle_failed):
            self._flush_actions(strategy, actions)

        return not cycle_failed

    def _flush_actions(self, strategy: StrategyExecutionProtocol, actions: List[Any]) -> None:
        if not actions:
            return

        pending = list(actions)
        actions.clear()
        strategy._action_collector = None

        for request in pending:
            if not isinstance(request, dict):
                continue

            action = request.get("action")
            is_trade_action = action in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING)
            try:
                if is_trade_action:
                    result = strategy._send_with_fillings(request)
                else:
                    with self._broker.lock:
                        result = self._broker.order_send(request)
            except Exception as exc:
                Logger.log(
                    strategy.symbol,
                    "EXCEPTION",
                    f"flush_actions order_send exception (magic={strategy.magic}): {exc}",
                )
                continue

            if result is None:
                Logger.log(
                    strategy.symbol,
                    "错误",
                    f"order_send 底层返回 None，C-API 交互异常 (magic={strategy.magic})。代码: {mt5.last_error()}",
                )
                continue

            if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                if is_trade_action and hasattr(strategy, "_handle_order_error"):
                    try:
                        strategy._handle_order_error(
                            result.retcode,
                            getattr(result, "comment", ""),
                            request.get("price"),
                        )
                    except Exception as exc:
                        Logger.log(
                            strategy.symbol,
                            "EXCEPTION",
                            f"_handle_order_error failed (magic={strategy.magic}): {exc}",
                        )
                else:
                    Logger.log(
                        strategy.symbol,
                        "拒单",
                        f"RC: {result.retcode} | {getattr(result, 'comment', '无附言')} | magic={strategy.magic}",
                    )
                continue

            if is_trade_action and hasattr(strategy, "_on_order_submit_success"):
                try:
                    strategy._on_order_submit_success()
                except Exception:
                    pass
