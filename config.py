"""
config.py — 환경변수 로드, 전략 설정, 멀티 계좌 상태 관리

멀티 계좌 .env 설정 예시:
  # 계좌 목록 (쉼표 구분)
  KIS_ACCOUNTS=ACC1,ACC2

  # 계좌 1
  ACC1_APP_KEY=...
  ACC1_APP_SECRET=...
  ACC1_CANO=69007214
  ACC1_ACNT_PRDT_CD=22
  ACC1_FIXED_SEED=10000
  ACC1_SOXL_TARGET_PROFIT=12.0
  ACC1_TQQQ_TARGET_PROFIT=10.0

  # 계좌 2
  ACC2_APP_KEY=...
  ACC2_APP_SECRET=...
  ACC2_CANO=69007215
  ACC2_ACNT_PRDT_CD=22
  ACC2_FIXED_SEED=20000
  ACC2_SOXL_TARGET_PROFIT=12.0
  ACC2_TQQQ_TARGET_PROFIT=10.0

단일 계좌는 기존 방식 그대로 사용 가능:
  KIS_APP_KEY=...
  KIS_APP_SECRET=...
  KIS_CANO=69007214
"""

import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# ── 공통 상수 ─────────────────────────────────────────────────
URL_BASE = os.getenv("KIS_URL_BASE", "https://openapi.koreainvestment.com:9443")

EXCHANGE_CODE: dict = {
    "SOXL": "AMEX",
    "TQQQ": "NASD",
    "BIL":  "NYSE",   # 미국 단기국채 ETF (예수금 버퍼용)
}
PRICE_EXCHANGE_CODE: dict = {
    "SOXL": "AMS",
    "TQQQ": "NAS",
    "BIL":  "NYS",    # BIL 시세 조회용 거래소 코드
}

# ── BIL 버퍼 설정 ─────────────────────────────────────────────
BIL_ENABLED:     bool  = os.getenv("BIL_ENABLED", "true").lower() == "true"
BIL_BUFFER_USD:  float = float(os.getenv("BIL_BUFFER_USD", "200"))
BIL_WEEKLY_DAYS: int   = int(os.getenv("BIL_WEEKLY_DAYS", "5"))
# 예수금 > (1주일치 필요금 + BIL_BUFFER_USD) → BIL MOO 매수 (초과분 파킹)
# 예수금 < (오늘 하루치 필요금)              → BIL MOO 매도 (오늘치 부족분만)
# 그 사이                                     → 거래 없음 (수수료 절약)


# ============================================================
#  Account — 계좌 한 개의 모든 상태를 담는 클래스
# ============================================================

@dataclass
class Account:
    # ── 식별자 ──────────────────────────────────────────────
    name:         str            # 계좌 별칭 (예: "ACC1", "main")

    # ── KIS 인증 정보 ────────────────────────────────────────
    app_key:      str
    app_secret:   str
    cano:         str
    acnt_prdt_cd: str = "22"

    # ── 전략 설정 ────────────────────────────────────────────
    strategy_config: dict = field(default_factory=dict)

    # ── 런타임 상태 (sync_account()에 의해 갱신) ────────────
    strategy:     dict  = field(default_factory=dict)
    current_cash: float = 0.0

    # ── 파일 경로 (계좌별로 분리) ────────────────────────────
    @property
    def token_file(self) -> str:
        return f"token_{self.cano}.dat"

    @property
    def trade_lock_file(self) -> str:
        return f"trade_lock_{self.cano}.json"

    @property
    def cumul_profit_file(self) -> str:
        return f"cumul_profit_{self.cano}.json"

    @property
    def log_file(self) -> str:
        return f"trader_{self.cano}.log"

    @property
    def bil_sold_file(self) -> str:
        """BIL 매도 주문이 있었던 날 표시 파일 (더블체크 트리거용)."""
        return f"bil_sold_{self.cano}.json"

    def reset_strategy(self):
        """strategy_config 기반으로 런타임 strategy 초기화."""
        self.strategy = {
            ticker: {**cfg, "data": {"avg": 0.0, "qty": 0.0, "cumul": 0.0}}
            for ticker, cfg in self.strategy_config.items()
        }


# ============================================================
#  계좌 목록 로드
# ============================================================

def _load_strategy_config(prefix: str, fixed_seed: float) -> dict:
    """prefix 기반으로 종목별 전략 설정을 읽어 반환."""
    def _bool(key: str, default: str = "true") -> bool:
        return os.getenv(key, default).strip().lower() not in ("false", "0", "no")

    return {
        "SOXL": {
            "seed":          float(os.getenv(f"{prefix}_SOXL_SEED", str(fixed_seed))),
            "total_a":       float(os.getenv(f"{prefix}_SOXL_TOTAL_A", os.getenv("SOXL_TOTAL_A", "20"))),
            "target_profit": float(os.getenv(f"{prefix}_SOXL_TARGET_PROFIT", os.getenv("SOXL_TARGET_PROFIT", "12.0"))),
            "use_turbo":     _bool(os.getenv(f"{prefix}_SOXL_USE_TURBO", os.getenv("SOXL_USE_TURBO", "true"))),
        },
        "TQQQ": {
            "seed":          float(os.getenv(f"{prefix}_TQQQ_SEED", str(fixed_seed))),
            "total_a":       float(os.getenv(f"{prefix}_TQQQ_TOTAL_A", os.getenv("TQQQ_TOTAL_A", "20"))),
            "target_profit": float(os.getenv(f"{prefix}_TQQQ_TARGET_PROFIT", os.getenv("TQQQ_TARGET_PROFIT", "10.0"))),
            "use_turbo":     _bool(os.getenv(f"{prefix}_TQQQ_USE_TURBO", os.getenv("TQQQ_USE_TURBO", "true"))),
        },
    }


def load_accounts() -> list[Account]:
    """
    .env에서 계좌 목록을 읽어 Account 리스트 반환.

    멀티 계좌: KIS_ACCOUNTS=ACC1,ACC2 → 각 prefix로 설정 읽기
    단일 계좌: KIS_ACCOUNTS 없음 → 기존 KIS_APP_KEY 방식
    """
    accounts_env = os.getenv("KIS_ACCOUNTS", "").strip()

    # ── 멀티 계좌 모드 ──────────────────────────────────────
    if accounts_env:
        accounts = []
        for name in [n.strip() for n in accounts_env.split(",") if n.strip()]:
            p          = name  # prefix
            fixed_seed = float(os.getenv(f"{p}_FIXED_SEED", os.getenv("FIXED_SEED", "10000")))
            sc         = _load_strategy_config(p, fixed_seed)
            acc        = Account(
                name         = name,
                app_key      = os.getenv(f"{p}_APP_KEY", ""),
                app_secret   = os.getenv(f"{p}_APP_SECRET", ""),
                cano         = os.getenv(f"{p}_CANO", ""),
                acnt_prdt_cd = os.getenv(f"{p}_ACNT_PRDT_CD", "22"),
                strategy_config = sc,
            )
            acc.reset_strategy()
            accounts.append(acc)
        return accounts

    # ── 단일 계좌 모드 (기존 방식 호환) ─────────────────────
    fixed_seed = float(os.getenv("FIXED_SEED", "10000"))
    sc = _load_strategy_config("", fixed_seed)
    # prefix="" → f"_SOXL_SEED" 같은 키는 없으므로 공통 fallback으로 읽힘
    # 공통키(SOXL_TARGET_PROFIT 등)를 직접 읽도록 재구성
    sc = {
        "SOXL": {
            "seed":          fixed_seed,
            "total_a":       float(os.getenv("SOXL_TOTAL_A", "20")),
            "target_profit": float(os.getenv("SOXL_TARGET_PROFIT", "12.0")),
        },
        "TQQQ": {
            "seed":          fixed_seed,
            "total_a":       float(os.getenv("TQQQ_TOTAL_A", "20")),
            "target_profit": float(os.getenv("TQQQ_TARGET_PROFIT", "10.0")),
        },
    }
    acc = Account(
        name         = "default",
        app_key      = os.getenv("KIS_APP_KEY", ""),
        app_secret   = os.getenv("KIS_APP_SECRET", ""),
        cano         = os.getenv("KIS_CANO", ""),
        acnt_prdt_cd = os.getenv("KIS_ACNT_PRDT_CD", "22"),
        strategy_config = sc,
    )
    acc.reset_strategy()
    return [acc]


# ── 전역 계좌 목록 (프로그램 시작 시 1회 로드) ───────────────
ACCOUNTS: list[Account] = load_accounts()


# ============================================================
#  로깅 설정
# ============================================================

def setup_logging(log_file: str = "trader.log",
                  level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(log_file)  # 파일명으로 logger 구분
    if logger.handlers:
        return logger                      # 이미 설정된 경우 재사용
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh  = logging.FileHandler(log_file, encoding="utf-8")
    sh  = logging.StreamHandler()
    fh.setFormatter(fmt); sh.setFormatter(fmt)
    logger.addHandler(fh); logger.addHandler(sh)
    return logger


# 기본 로거 (모듈 레벨 import 용)
log = setup_logging()
