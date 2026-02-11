"""MT5 API 包装器，修复 order_check 和 order_send 的参数传递问题"""

import MetaTrader5 as mt5


def order_check(request):
    """包装 mt5.order_check，确保正确传递参数"""
    return mt5.order_check(request)


def order_send(request):
    """包装 mt5.order_send，确保正确传递参数"""
    return mt5.order_send(request)