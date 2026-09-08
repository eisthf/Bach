"""KRX 일반 주식의 가격대별 호가 단위 (ETF/ETN 제외)."""
from decimal import Decimal, ROUND_FLOOR


def tick_size(price: float) -> int:
    for ceiling, step in ((2_000, 1), (5_000, 5), (20_000, 10),
                          (50_000, 50), (200_000, 100), (500_000, 500)):
        if price < ceiling:
            return step
    return 1_000


def buy_trigger(base: float, ratio: Decimal) -> float:
    """원래의 '현재가 <= 계산값' 조건을 보존하도록 유효 호가로 내림."""
    price = Decimal(str(base)) * ratio
    step = Decimal(tick_size(price))
    return float((price / step).to_integral_value(rounding=ROUND_FLOOR) * step)
