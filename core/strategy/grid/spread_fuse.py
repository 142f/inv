from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SpreadFuseResult:
    triggered: bool
    active: bool
    pause_until: float
    spread: float
    rel_atr: float
    rel_mid: float
    cooldown: float


class RelativeSpreadFusePolicy:
    """Relative spread fuse with enter/exit hysteresis."""

    def evaluate(
        self,
        *,
        bid: float,
        ask: float,
        point: float,
        atr_reference: float,
        now: float,
        active: bool,
        max_spread_points: float | None,
        extreme_cooldown: float,
        enter_atr: float,
        enter_mid: float,
        exit_atr: float,
        exit_mid: float,
        hold_seconds: float,
    ) -> SpreadFuseResult:
        spread = max(0.0, float(ask - bid))
        safe_point = max(float(point), 1e-9)
        mid = max(float((ask + bid) * 0.5), safe_point)
        atr_ref = max(float(atr_reference), safe_point, 1e-9)

        abs_limit = None
        if max_spread_points is not None and max_spread_points > 0:
            abs_limit = float(max_spread_points) * safe_point

        rel_atr = spread / atr_ref
        rel_mid = spread / mid

        enter = rel_atr >= float(enter_atr) or rel_mid >= float(enter_mid)
        if abs_limit is not None and spread >= abs_limit:
            enter = True

        exit_ok = rel_atr <= float(exit_atr) and rel_mid <= float(exit_mid)
        if abs_limit is not None:
            exit_ok = exit_ok and spread <= abs_limit * 0.90

        if active:
            if exit_ok:
                return SpreadFuseResult(
                    triggered=False,
                    active=False,
                    pause_until=0.0,
                    spread=spread,
                    rel_atr=rel_atr,
                    rel_mid=rel_mid,
                    cooldown=0.0,
                )
            pause_until = now + max(1.0, float(hold_seconds))
            return SpreadFuseResult(
                triggered=True,
                active=True,
                pause_until=pause_until,
                spread=spread,
                rel_atr=rel_atr,
                rel_mid=rel_mid,
                cooldown=pause_until - now,
            )

        if not enter:
            return SpreadFuseResult(
                triggered=False,
                active=False,
                pause_until=0.0,
                spread=spread,
                rel_atr=rel_atr,
                rel_mid=rel_mid,
                cooldown=0.0,
            )

        severity = 1.0
        if abs_limit is not None and abs_limit > 0:
            severity = max(severity, spread / abs_limit)
        severity = max(
            severity,
            rel_atr / max(float(enter_atr), 1e-9),
            rel_mid / max(float(enter_mid), 1e-9),
        )
        cooldown = float(extreme_cooldown) * min(3.0, max(1.0, severity))
        cooldown = max(cooldown, max(1.0, float(hold_seconds)))
        pause_until = now + cooldown
        return SpreadFuseResult(
            triggered=True,
            active=True,
            pause_until=pause_until,
            spread=spread,
            rel_atr=rel_atr,
            rel_mid=rel_mid,
            cooldown=cooldown,
        )

