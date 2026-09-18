"""经纪商品种名自动匹配，不依赖 MT5 或网络。"""

from __future__ import annotations

from collections.abc import Iterable


_ALIASES: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("XAUUSD", "GOLD"),
    "BTCUSD": ("BTCUSD", "XBTUSD", "BTCUSDT"),
}


def resolve_broker_symbol(
    canonical: str,
    available_symbols: Iterable[str],
    *,
    preferred: str | None = None,
) -> str | None:
    """从券商目录选择最接近标准品种名的实际名称。

    优先级为调用方指定名称、标准名称完全匹配、标准名称前缀匹配，再到同类别名。
    函数只处理名称字符串，因此可独立测试且不会改变 MT5 的 MarketWatch 状态。
    """

    target = canonical.upper().strip()
    requested = (preferred or "").upper().strip()
    aliases = _ALIASES.get(target, (target,))
    candidates = sorted({str(name) for name in available_symbols if str(name).strip()})
    if not candidates:
        return None

    def score(name: str) -> tuple[int, int, str]:
        normalized = name.upper()
        if requested and normalized == requested:
            return (10_000, -len(name), name)
        if normalized == target:
            return (9_000, -len(name), name)
        if normalized.startswith(target):
            return (8_000, -len(name), name)
        for index, alias in enumerate(aliases):
            if normalized == alias:
                return (7_000 - index, -len(name), name)
            if normalized.startswith(alias):
                return (6_000 - index, -len(name), name)
        return (0, -len(name), name)

    matched = [name for name in candidates if score(name)[0] > 0]
    return max(matched, key=score) if matched else None
