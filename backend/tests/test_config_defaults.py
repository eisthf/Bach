"""자동매매 설정 기본값 — 새로 추가한 종목에 적용되는 운영 기본값."""
from app.models import AutoConfig


def test_operational_defaults_enabled():
    cfg = AutoConfig()
    assert cfg.max_buy_amount == 1_000_000      # 총 투자액
    assert cfg.ulc_allow_lower_open is True     # 하락 시가 진입 허용
    assert cfg.ulc_trailing is True             # 트레일링 스탑
    assert cfg.ulc_first_buy_ask3 is True       # 1차 매수: 매도 3호가 지정가


def test_saved_config_keeps_its_own_values():
    """저장된 설정에 값이 있으면 기본값 변경과 무관하게 그 값을 쓴다."""
    cfg = AutoConfig(**{"ulc_trailing": False, "ulc_first_buy_ask3": False})
    assert cfg.ulc_trailing is False and cfg.ulc_first_buy_ask3 is False
