# 무한매수법칙 자동매매 봇

한국투자증권(KIS) OpenAPI 기반 SOXL/TQQQ 무한매수법칙 자동매매 시스템.  
텔레그램 봇으로 실시간 모니터링 및 수동 주문을 지원합니다.

---

## 파일 구조

```
trader/
├── config.py        # 환경변수 로드, 전략 설정, 계좌 관리
├── kis_api.py       # KIS OpenAPI 래퍼 (토큰/시세/주문/계좌/미체결)
├── storage.py       # 로컬 파일 관리 (잠금/누적수익/계좌동기화/BIL플래그)
├── strategy.py      # 무한매수 전략 계산 + 통합 주문 실행
├── reporter.py      # 텔레그램/이메일 리포트 생성
├── jobs.py          # 스케줄 작업 정의
├── main.py          # 실행 진입점
├── calculator.py    # 전략 계산기 (주문 없음, 시뮬레이션 전용)
├── .env             # 민감 정보 (git 제외)
├── .env.example     # 설정 템플릿
└── requirements.txt
```

---

## 빠른 시작

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. 설정 파일 생성
cp .env.example .env
# .env 를 열어서 필수 항목 입력 (아래 설정 가이드 참고)

# 3. 실행
python main.py --telegram     # 텔레그램 봇 + 자동매매 (권장)
python main.py                # 텔레그램 없이 스케줄러만
```

---

## .env 설정 가이드

### 단일 계좌

```env
# ── KIS API ──────────────────────────────────────
KIS_APP_KEY=PSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
KIS_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
KIS_CANO=xxxxxxxx          # 계좌번호 앞 8자리
KIS_ACNT_PRDT_CD=xx        # 계좌번호 뒤 2자리

# ── 전략 설정 ─────────────────────────────────────
FIXED_SEED=10000           # 종목당 시드 (USD)
SOXL_TARGET_PROFIT=12.0   # SOXL 목표 수익률 %
TQQQ_TARGET_PROFIT=10.0   # TQQQ 목표 수익률 %
SOXL_TOTAL_A=20           # SOXL 분할 횟수
TQQQ_TOTAL_A=20           # TQQQ 분할 횟수
SOXL_USE_TURBO=true    # true/false (기본값: true)
TQQQ_USE_TURBO=false
```

**텔레그램 `/set` 에서 즉시 토글**
```
/set → 종목 선택 → ⚡ 가속매수: ON ✅ → OFF로 변경  (탭하면 즉시 전환)
```

**`/settings` 에서 현재 상태 확인**
```
[SOXL]
  가속매수: ON ✅
[TQQQ]
  가속매수: OFF ❌


# ── 텔레그램 ─────────────────────────────────────
TELEGRAM_BOT_TOKEN=7xxxxxxxxx:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789

# ── 이메일 리포트 (선택) ───────────────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_gmail@gmail.com
SMTP_PASS=xxxxxxxxxxxx     # Gmail 앱 비밀번호 16자리
MAIL_TO=your_email@naver.com

# ── BIL 예수금 버퍼 (RP 대신 사용, 선택) ──────────
BIL_ENABLED=true           # BIL 버퍼 기능 ON/OFF (기본: true)
BIL_WEEKLY_DAYS=5          # 예수금 보유 목표 기간 (기본: 5영업일)
BIL_BUFFER_USD=200         # 1주일치 위에 추가 여유금 (기본: $200)
```

### 멀티 계좌

```env
KIS_ACCOUNTS=ACC1,ACC2

ACC1_APP_KEY=PSxxxx...
ACC1_APP_SECRET=xxxx...
ACC1_CANO=xxxxxxxx
ACC1_ACNT_PRDT_CD=xx
ACC1_FIXED_SEED=10000
ACC1_SOXL_TARGET_PROFIT=12.0
ACC1_TQQQ_TARGET_PROFIT=10.0
ACC1_SOXL_USE_TURBO=false
ACC1_TQQQ_USE_TURBO=false

ACC2_APP_KEY=PSxxxx...
ACC2_APP_SECRET=xxxx...
ACC2_CANO=xxxxxxxz
ACC2_ACNT_PRDT_CD=xx
ACC2_FIXED_SEED=20000
ACC2_SOXL_TARGET_PROFIT=12.0
ACC2_TQQQ_TARGET_PROFIT=10.0
```

---

## 자동매매 스케줄 (KST 기준)

| 시각 | 요일 | 작업 |
|------|------|------|
| 00:10 | 화~토 | BIL 매도일 주문 더블체크 (거부된 주문 재접수) |
| 08:00 | 월~토 | 전일 체결 확인 + 수익 기록 |
| 18:00~18:30 | 월~금 (5분마다) | 프리마켓 목표가 체크 → 자동 익절+재진입 |
| 18:25 | 월~금 | BIL 예수금 버퍼 관리 (MOO 주문) |
| 18:30 | 월~금 | 정규장 자동매매 (LOC/지정가 주문 접수) |
| 6시간마다 | 매일 | KIS API 토큰 갱신 |

> **주문 타이밍**: 18:30에 LOC/지정가 주문을 접수하면 미국장 개장(KST 23:30)에 체결됩니다.

---

## 전략 설명

### 매수 주문 (매일 18:30)

| 주문 | 수량 | 가격 | 타입 |
|------|------|------|------|
| 매수1 (전반전) | 약 1단위 | 평단가 | LOC |
| 매수2 | 약 0.5단위 | 별가 (star_p) | LOC |
| 가속매수 | 약 1단위 | min(평단, 전일종가) × 0.95 | LOC |
| 줍줍 | 각 1주 × 6단계 | 평단 대비 점진적 낮은 가격 | LOC |

> **별가(star_p)** = 평단가 × (1 + 별% / 100)  
> **별%** = 목표수익률 × (1 - 2T/total_a)  — T가 쌓일수록 별% 감소

### 매도 주문 (동시에 접수)

| 주문 | 수량 | 가격 | 타입 |
|------|------|------|------|
| 매도1 | 보유의 3/4 | 목표가 (평단 × 목표수익률) | 지정가 |
| 매도2 | 보유의 1/4 | 별가 + $0.01 | LOC |

> **자전거래 방지**: 매도2 LOC 호가는 항상 모든 매수 LOC 호가보다 $0.01 높게 자동 보정됩니다.

### T값과 전략 전환

- **전반전** (T ≤ total_a/2): 매수1 + 매수2 절반씩
- **후반전** (T > total_a/2): 매수2만 (별가로 집중 추가 매수)
- **T ≥ 19**: 쿼터매도 발동 (1/4 MOC + 3/4 지정가)

---

## BIL 예수금 버퍼

RP 자동매매 대신 BIL(미국 단기국채 ETF)로 예수금을 운용합니다.

```
[매수 조건] 예수금 > 1주일치 필요금 + BIL_BUFFER_USD
  → 초과분을 BIL MOO 매수 (여유 현금 파킹)

[매도 조건] 예수금 < 오늘 하루치 필요금
  → 부족분만큼 BIL MOO 매도

[유지 구간] 그 사이 → 거래 없음 (수수료 절약)
```

BIL 매도가 있었던 날은 다음날 00:10에 자동으로 더블체크합니다.  
예수금 부족으로 거부된 주문이 있으면 **원래 타입(LOC/지정가)으로 재접수**합니다.

---

## 텔레그램 명령어

| 명령어 | 설명 |
|--------|------|
| `/start` | 봇 시작 + 명령어 목록 |
| `/status [계좌명]` | 실시간 시세 + 전략 현황 |
| `/sync [계좌명]` | 계좌 동기화 + 주문 지시서 (버튼 클릭으로 실행) |
| `/order [계좌명]` | 즉시 주문 실행 (버튼 없이 바로) |
| `/orders [계좌명]` | 미체결 주문 조회 |
| `/cash [계좌명]` | 예수금 + 보유 종목 + BIL 버퍼 현황 |
| `/report [계좌명]` | 포트폴리오 리포트 즉시 발송 |
| `/reset [계좌명]` | 오늘 매매 잠금 해제 (재주문 허용) |
| `/profit_history [계좌명]` | 누적 수익 내역 |
| `/profit_reset [계좌명]` | 수익 기록 초기화 |

> `[계좌명]` 생략 시 전체 계좌에 적용됩니다.  
> 멀티 계좌 예시: `/sync ACC1`

---

## CLI 명령어 (main.py)

```bash
python main.py                   # 스케줄러 단독 실행
python main.py --telegram        # 텔레그램 봇 + 스케줄러
python main.py status            # 전략 현황 출력
python main.py report            # 리포트 즉시 발송
python main.py unlock            # 전체 매매 잠금 해제
python main.py history 30        # 수익 내역 최근 30건
```

---

## 전략 계산기 (calculator.py)

계좌 연결 없이 주문 계획을 시뮬레이션합니다.

```bash
# 대화형 모드
python calculator.py

# CLI 모드
python calculator.py --ticker SOXL --avg 63.80 --qty 25 --seed 10000 --t 20 --target 12
```

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--ticker` | 종목 | 필수 |
| `--avg` | 평단가 (USD) | 필수 |
| `--qty` | 보유 수량 | 필수 |
| `--seed` | 시드 (USD) | 필수 |
| `--t` | 분할 횟수 | 20 |
| `--target` | 목표 수익률 % | 12 |
| `--price` | 현재가 (0=자동조회) | 0 |
| `--prev` | 전일종가 (0=자동조회) | 0 |
| `--no-turbo` | 가속매수 제외 | False |

---

## 런타임 생성 파일

| 파일 | 설명 |
|------|------|
| `token_{cano}.dat` | KIS API 토큰 캐시 |
| `trade_lock_{cano}.json` | 당일 매매 잠금 (중복 방지) |
| `cumul_profit_{cano}.json` | 누적 실현수익 내역 |
| `bil_sold_{cano}.json` | BIL 매도일 더블체크 트리거 |
| `trader_{cano}.log` | 실행 로그 |

---

## 주의사항

- 모든 매수 주문은 **LOC(Limit-On-Close)** 타입으로 미국장 개장가 기준 체결됩니다.
- KIS OpenAPI 실전 계좌 기준입니다. 모의 계좌는 TR_ID가 다를 수 있습니다.
- BIL 거래소 코드: `NYSE` (주문), `NYS` (시세조회).
- 주문 가격은 소수점 2자리까지만 허용됩니다 (KIS API 제한).
- 텔레그램 콜백 데이터는 64바이트 제한으로 LOC 매도가는 버튼 클릭 시 재계산됩니다.
