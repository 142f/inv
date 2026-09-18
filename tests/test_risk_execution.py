from __future__ import annotations

from types import SimpleNamespace
import time
import unittest

from core.domain import ExecutionMode, RiskSettings
from core.execution import DONE_RETCODE, BrokerGateway, ExecutionService, ExistingOrder, OrderReconciler
from core.domain import CommandAction, OrderCommand
from core.risk import RiskCoordinator, RiskSnapshot


class _Broker:
    def __init__(self) -> None:
        self.lock = _Lock()
        self.sent = []

    def order_send(self, request):
        self.sent.append(dict(request))
        return SimpleNamespace(retcode=DONE_RETCODE, comment="完成", order=len(self.sent))


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RiskAndExecutionTests(unittest.TestCase):
    def test_net_exposure_halts_and_requests_cancellation(self) -> None:
        now = time.time()
        decision = RiskCoordinator().assess(
            RiskSnapshot(
                strategy_id="1:TEST",
                now=now,
                equity=100.0,
                balance=100.0,
                margin_level=500.0,
                bid=10.0,
                ask=10.01,
                tick_time=now,
                positions=[SimpleNamespace(type=0, volume=2.0)],
                orders=[],
            ),
            RiskSettings(max_net_volume=1.0),
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.cancel_pending)
        self.assertIn("净敞口", decision.reasons[0])

    def test_paper_execution_never_sends_and_is_idempotent(self) -> None:
        broker = _Broker()
        service = ExecutionService(broker, mode=ExecutionMode.PAPER)
        service.begin_cycle("1:TEST", 2)
        request = {"action": 5, "symbol": "TEST", "magic": 1, "price": 10.0}
        first = service.submit_native(request, strategy_id="1:TEST")
        second = service.submit_native(request, strategy_id="1:TEST")
        self.assertTrue(first.simulated)
        self.assertTrue(second.duplicate)
        self.assertEqual(broker.sent, [])

    def test_live_execution_requires_strategy_permission(self) -> None:
        broker = _Broker()
        service = ExecutionService(broker, mode=ExecutionMode.LIVE, live_enabled=True)
        service.begin_cycle("1:TEST", 2)
        request = {"action": 5, "symbol": "TEST", "magic": 1, "price": 10.0}
        downgraded = service.submit_native(request, strategy_id="1:TEST")
        self.assertTrue(downgraded.simulated)
        service.set_live_permission("2:TEST", True)
        service.begin_cycle("2:TEST", 2)
        result = service.submit_native({**request, "magic": 2}, strategy_id="2:TEST")
        self.assertFalse(result.simulated)
        self.assertEqual(len(broker.sent), 1)

    def test_action_budget_and_reconciliation_limit_work(self) -> None:
        broker = _Broker()
        service = ExecutionService(broker, mode=ExecutionMode.PAPER)
        service.begin_cycle("1:TEST", 1)
        first = service.submit_native({"action": 5, "magic": 1, "symbol": "TEST", "price": 1.0}, strategy_id="1:TEST")
        second = service.submit_native({"action": 5, "magic": 1, "symbol": "TEST", "price": 2.0}, strategy_id="1:TEST")
        self.assertTrue(first.simulated)
        self.assertFalse(second.simulated)
        self.assertNotEqual(second.retcode, DONE_RETCODE)
        commands = (
            OrderCommand("keep", CommandAction.PLACE_LIMIT, "1:TEST", "TEST", {"price": 1.0}, 1),
            OrderCommand("new", CommandAction.PLACE_LIMIT, "1:TEST", "TEST", {"price": 2.0}, 2),
        )
        reconciled = OrderReconciler().reconcile(commands, {"keep"}, max_actions=1)
        self.assertEqual([item.idempotency_key for item in reconciled], ["new"])
        removal = OrderReconciler().reconcile(
            (),
            (ExistingOrder("stale", 99, "1:TEST", "TEST"),),
            max_actions=1,
        )
        self.assertEqual(removal[0].action, CommandAction.CANCEL_ORDER)
        self.assertEqual(removal[0].payload["ticket"], 99)

    def test_gateway_routes_paper_orders_without_calling_broker(self) -> None:
        broker = _Broker()
        service = ExecutionService(broker, mode=ExecutionMode.PAPER)
        service.begin_cycle("1:TEST", 2)
        result = BrokerGateway(broker, service).call(
            "order_send", {"action": 5, "magic": 1, "symbol": "TEST", "price": 1.0}
        )
        self.assertTrue(result.simulated)
        self.assertEqual(broker.sent, [])


if __name__ == "__main__":
    unittest.main()
