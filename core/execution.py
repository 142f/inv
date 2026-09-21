"""命令协调、幂等执行与兼容 MT5 调用网关。"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping, Protocol

from core.domain import CommandAction, CommandResult, ExecutionMode, OrderCommand


DONE_RETCODE = 10009
PLACED_RETCODE = 10008
DISABLED_RETCODE = 10027


class BrokerPort(Protocol):
    @property
    def lock(self) -> Any: ...

    def account_info(self) -> Any: ...
    def terminal_info(self) -> Any: ...
    def orders_get(self) -> Any: ...
    def positions_get(self) -> Any: ...
    def symbol_info_tick(self, symbol: str) -> Any: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any: ...
    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any: ...
    def order_check(self, request: Mapping[str, Any]) -> Any: ...
    def order_send(self, request: Mapping[str, Any]) -> Any: ...


@dataclass(slots=True)
class NativeExecutionResult:
    """兼容旧策略的 MT5 返回字段，同时标记纸面与幂等状态。"""

    retcode: int
    comment: str
    order: int = 0
    queued: bool = False
    simulated: bool = False
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ExistingOrder:
    """订单协调器所需的最小挂单快照。"""

    idempotency_key: str
    ticket: int
    strategy_id: str
    symbol: str


class OrderReconciler:
    """根据稳定幂等键比较目标订单，避免重复提交相同命令。"""

    def reconcile(
        self,
        desired: Iterable[OrderCommand],
        existing_keys: Iterable[str | ExistingOrder],
        *,
        max_actions: int,
    ) -> tuple[OrderCommand, ...]:
        existing: dict[str, ExistingOrder | None] = {}
        for item in existing_keys:
            if isinstance(item, ExistingOrder):
                existing[item.idempotency_key] = item
            else:
                existing[str(item)] = None
        candidates = sorted(desired, key=lambda item: (item.priority, item.idempotency_key))
        selected = [command for command in candidates if command.idempotency_key not in existing]
        desired_limit_keys = {
            command.idempotency_key
            for command in candidates
            if command.action.value == "place_limit"
        }
        cancellations = [
            OrderCommand(
                idempotency_key=f"{order.strategy_id}:cancel:{order.ticket}",
                action=CommandAction.CANCEL_ORDER,
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                payload={"ticket": order.ticket},
                priority=0,
            )
            for key, order in existing.items()
            if order is not None and key not in desired_limit_keys
        ]
        selected = cancellations + selected
        return tuple(selected[: max(0, int(max_actions))])


class ExecutionService:
    """唯一允许产生券商副作用的服务；默认纸面执行。"""

    def __init__(
        self,
        broker: BrokerPort,
        *,
        mode: ExecutionMode | str = ExecutionMode.PAPER,
        live_enabled: bool = False,
    ) -> None:
        self._broker = broker
        mode_value = mode.value if isinstance(mode, ExecutionMode) else str(mode).lower()
        self._mode = ExecutionMode(mode_value) if mode_value in {"paper", "live"} else ExecutionMode.PAPER
        self._live_enabled = bool(live_enabled)
        self._completed: OrderedDict[str, NativeExecutionResult] = OrderedDict()
        self._max_completed = 20_000
        self._cycle_counts: defaultdict[str, int] = defaultdict(int)
        self._cycle_limits: dict[str, int] = {}
        self._live_permissions: dict[str, bool] = {}
        self._paper_sequence = 0

    @property
    def mode(self) -> ExecutionMode:
        return self._mode

    def is_live_permitted(self, strategy_id: str) -> bool:
        return (
            self._mode is ExecutionMode.LIVE
            and self._live_enabled
            and self._live_permissions.get(strategy_id, False)
        )

    def begin_cycle(self, strategy_id: str, max_actions: int) -> None:
        self._cycle_counts[strategy_id] = 0
        self._cycle_limits[strategy_id] = max(1, int(max_actions))

    def set_live_permission(self, strategy_id: str, allowed: bool) -> None:
        """实盘许可由类型化风险配置授予，环境变量不能单独绕过。"""
        self._live_permissions[strategy_id] = bool(allowed)

    def submit_native(self, request: Mapping[str, Any], *, strategy_id: str) -> NativeExecutionResult:
        normalized = dict(request)
        identity = self._native_key(normalized, strategy_id)
        prior = self._completed.get(identity)
        if prior is not None:
            self._completed.move_to_end(identity)
            return NativeExecutionResult(
                retcode=prior.retcode,
                comment="重复命令已确认",
                order=prior.order,
                simulated=prior.simulated,
                duplicate=True,
            )

        if self._cycle_counts[strategy_id] >= self._cycle_limits.get(strategy_id, 10):
            return NativeExecutionResult(DISABLED_RETCODE, "单周期操作预算已耗尽")

        self._cycle_counts[strategy_id] += 1
        if not self.is_live_permitted(strategy_id):
            self._paper_sequence += 1
            result = NativeExecutionResult(
                retcode=PLACED_RETCODE,
                comment="纸面执行已记录" if self._mode is ExecutionMode.PAPER else "缺少策略级实盘许可，已降级纸面执行",
                order=self._paper_sequence,
                simulated=True,
            )
            self._completed[identity] = result
            self._trim_completed()
            return result

        try:
            with self._broker.lock:
                result = self._broker.order_send(normalized)
        except Exception as exc:
            return NativeExecutionResult(-1, f"执行网关异常: {type(exc).__name__}")
        if result is None:
            return NativeExecutionResult(-1, "券商未返回执行结果")

        converted = NativeExecutionResult(
            retcode=int(getattr(result, "retcode", -1)),
            comment=str(getattr(result, "comment", "") or ""),
            order=int(getattr(result, "order", 0) or 0),
        )
        if converted.retcode in {DONE_RETCODE, PLACED_RETCODE}:
            self._completed[identity] = converted
            self._trim_completed()
        return converted

    def submit(self, command: OrderCommand) -> CommandResult:
        result = self.submit_native(command.payload, strategy_id=command.strategy_id)
        return CommandResult(
            accepted=result.retcode in {DONE_RETCODE, PLACED_RETCODE},
            simulated=result.simulated,
            retcode=result.retcode,
            comment=result.comment,
            order=result.order,
            duplicate=result.duplicate,
        )

    @staticmethod
    def _native_key(request: Mapping[str, Any], strategy_id: str) -> str:
        # [P-02] 用排序拼接替代 json.dumps+dict() 拷贝，减少序列化与 SHA-256 的输入构建开销
        parts = "|".join(f"{k}={v}" for k, v in sorted(request.items()))
        return hashlib.sha256(f"{strategy_id}|{parts}".encode("utf-8")).hexdigest()

    def _trim_completed(self) -> None:
        while len(self._completed) > self._max_completed:
            self._completed.popitem(last=False)


class BrokerGateway:
    """旧策略调用的兼容网关，阻断其绕过 ExecutionService 的可能。"""

    def __init__(self, broker: BrokerPort, execution: ExecutionService) -> None:
        self._broker = broker
        self._execution = execution

    def set_strategy_permission(self, strategy_id: str, allowed: bool) -> None:
        self._execution.set_live_permission(strategy_id, allowed)

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "order_send":
            request = args[0] if args else kwargs.get("request", {})
            strategy_id = self._strategy_id_from_request(request)
            return self._execution.submit_native(request, strategy_id=strategy_id)
        if name == "orders_get":
            with self._broker.lock:
                orders = self._broker.orders_get() or ()
            symbol = kwargs.get("symbol")
            return tuple(item for item in orders if symbol is None or getattr(item, "symbol", None) == symbol)
        if name == "positions_get":
            with self._broker.lock:
                positions = self._broker.positions_get() or ()
            symbol = kwargs.get("symbol")
            return tuple(item for item in positions if symbol is None or getattr(item, "symbol", None) == symbol)
        if name == "copy_rates_from_pos":
            with self._broker.lock:
                return self._broker.copy_rates_from_pos(*args, **kwargs)
        if name == "symbol_info_tick":
            with self._broker.lock:
                return self._broker.symbol_info_tick(*args, **kwargs)
        if name == "symbol_info":
            with self._broker.lock:
                return self._broker.symbol_info(*args, **kwargs)
        if name == "account_info":
            with self._broker.lock:
                return self._broker.account_info()
        if name == "terminal_info":
            with self._broker.lock:
                return self._broker.terminal_info()
        if name == "history_deals_get":
            with self._broker.lock:
                return self._broker.history_deals_get(*args, **kwargs)
        if name == "order_check":
            # 纸面模式不向终端发送预检请求，保持策略执行无外部副作用。
            request = args[0] if args else kwargs.get("request", {})
            if not self._execution.is_live_permitted(self._strategy_id_from_request(request)):
                return NativeExecutionResult(DONE_RETCODE, "纸面预检通过", simulated=True)
            with self._broker.lock:
                return self._broker.order_check(*args, **kwargs)
        raise ValueError(f"不支持的券商调用: {name}")

    @staticmethod
    def _strategy_id_from_request(request: Mapping[str, Any]) -> str:
        magic = request.get("magic", "unknown")
        symbol = request.get("symbol", "unknown")
        return f"{magic}:{symbol}"
