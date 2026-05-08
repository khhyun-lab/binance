# Binance Futures 백테스팅 가이드

이 문서는 현재 저장소의 백테스트와 리플레이 경로를 설명한다. 과거에는 단일 strategy.py 파일을 기준으로 설명할 수 있었지만, 현재 전략 로직은 binance_bot/strategy 패키지로 분리되어 있다. 백테스트는 이 패키지의 실제 snapshot, regime, entry, exit, risk 로직을 재사용하고 주문 실행만 simulator로 분리한다.

## 데이터 다운로드 방법

과거 캔들은 공개 Binance USD-M Futures Kline API로 받는다.

```bash
python -m binance_bot.backtest download --symbols BTCUSDT,ETHUSDT,SOLUSDT --intervals 1m,3m,5m,15m --start 2025-01-01 --end 2025-01-31
```

저장 위치는 data/binance_futures/klines/{symbol}/{interval}/{YYYY-MM}.jsonl 이다.

## 백테스트 실행 방법

```bash
python -m binance_bot.backtest run --symbols BTCUSDT,ETHUSDT,SOLUSDT --start 2025-01-01 --end 2025-01-31 --initial-balance 1000 --leverage 7 --margin-per-trade 20 --taker-fee 0.0004 --maker-fee 0.0002 --slippage-bps 2 --output-dir reports/backtests
```

## 리플레이 실행 방법

```bash
python -m binance_bot.backtest replay --symbol BTCUSDT --start 2025-01-01 --end 2025-01-03 --initial-balance 1000 --leverage 7 --margin-per-trade 20 --debug-decisions --output-dir reports/replay
```

## 7배 레버리지 예시

7배 레버리지 예시는 기본 설정과 동일하다.

```bash
python -m binance_bot.backtest run --symbols BTCUSDT --start 2025-01-01 --end 2025-01-07 --initial-balance 1000 --leverage 7 --margin-per-trade 25
```

## 수수료 가정

- 기본 taker fee는 0.0004다.
- 기본 maker fee는 0.0002다.
- 기존 전략의 손익 계산은 round_trip_fee_pct와 leverage를 반영한다.
- 백테스트 metrics에는 실제 simulator에 적용된 fees_paid가 별도로 기록된다.

## 슬리피지 가정

- slippage_bps 기본값은 2 bps다.
- 진입과 청산은 다음 캔들 시가 체결을 기준으로 하고, side 방향에 맞춰 불리한 방향으로 슬리피지가 적용된다.

## 체결 가정

- entry와 scale_in은 decision이 발생한 다음 캔들 시가에 체결된다.
- exit도 pending order로 들어가며 다음 캔들 시가 체결 또는 intrabar TP/SL로 종료된다.
- 백테스트 경로는 live Binance order service를 호출하지 않는다.

## same-candle TP/SL 보수적 처리

한 캔들 안에서 TP와 SL이 모두 닿으면 보수적으로 더 불리한 방향을 우선 체결로 본다.

- LONG 포지션은 stop_loss 우선
- SHORT 포지션도 stop_loss 우선

이 가정은 과도하게 낙관적인 체결 결과를 줄이기 위한 것이다.

## 미래 데이터 누수 방지 방식

- HistoricalMarketDataProvider는 current_time_ms 이하의 캔들만 노출한다.
- get_klines는 close_time 기준으로 현재 시점까지 닫힌 캔들만 반환한다.
- snapshot 계산은 provider가 노출한 visible candle만 사용한다.
- 백테스트 runner는 decision을 현재 캔들 종료 시점에 계산하고, 체결은 다음 캔들에서 처리한다.

## 기존 strategy.py와의 통합 방식

현재 저장소에는 단일 binance_bot/strategy.py 파일이 없고, 아래 모듈로 분리돼 있다.

- snapshot.py
- regime.py
- entry.py
- exit.py
- risk.py
- scoring.py

백테스트는 다음 통합 원칙을 사용한다.

- indicator 계산은 strategy.snapshot.build_market_snapshot 경로를 그대로 사용한다.
- market regime 판정은 RegimeMixin._update_market_regime_state 를 그대로 사용한다.
- entry와 scale_in 판단은 EntryMixin의 _plan_entry, _plan_scale_in 경로를 사용한다.
- exit 판단은 ExitMixin._plan_exit 경로를 사용한다.
- TP/SL 계산은 RiskMixin._calculate_exit_lines 를 그대로 사용한다.
- 주문 실행만 simulator로 대체한다.

즉, decision logic은 strategy 패키지의 실제 로직을 타고, execution만 backtest 전용 구현으로 분리된다.

## decision_log 해석법

--debug-decisions를 켜면 decision_log.jsonl이 생성된다.

추가 분석 스크립트:

```bash
python scripts/analyze_decisions.py reports/backtests/20260508T000000Z
```

주요 필드:

- timestamp
- symbol
- price
- market_regime
- trend_direction
- preferred_side
- entry_type_candidate
- long_score
- short_score
- score_threshold
- score_edge
- entry_side
- entry_allowed
- entry_reason
- entry_detail_reasons
- entry_blockers
- exit_allowed
- exit_reason
- exit_detail_reasons
- tp_price
- sl_price
- rsi_1m
- rsi_3m
- ema_fast_1m
- ema_slow_1m
- atr_3m
- atr_15m
- volume_ratio
- breakout_high
- breakout_low
- latest_close_1m
- previous_close_1m
- breakout_chase_candidate
- pullback_reaccel_candidate
- pullback_valid
- reaccel_valid
- trend_alignment_ok
- volume_ok
- rsi_ok
- not_chasing_ok
- quantity_ok
- recent_high
- recent_low
- position_state

entry_allowed가 false면 진입 후보는 있었더라도 score, momentum, split, quantity 조건에서 차단된 상태일 수 있다. entry_type_candidate와 entry_blockers를 같이 보면 breakout chase가 막힌 것인지, pullback 재가속 후보가 있었는데 reaccel이나 RR에서 탈락한 것인지 바로 분리할 수 있다. exit_allowed가 false면 당시 캔들 기준으로 기존 전략의 청산 조건이 성립하지 않은 것이다.

## 리포트 파일 설명

- summary.json: 총 손익, 승률, profit factor, max drawdown 등 요약 지표
- trades.csv: 개별 체결 결과
- trades.csv에는 entry_reason 컬럼이 포함되어 breakout_chase_long, pullback_reaccel_short 같은 진입 유형별 집계가 가능하다.
- equity_curve.csv: 시점별 balance, equity, drawdown
- daily_pnl.csv: 일자별 순손익
- config.json: 실행 설정
- decision_log.jsonl: 디버그 decision trace, 기본 off

## 백테스트 비교 스크립트

```bash
python scripts/compare_backtests.py reports/backtests/run_a reports/backtests/run_b reports/backtests/run_c
```

이 스크립트는 각 리포트 디렉터리에서 다음 정보를 읽어 한 줄씩 비교한다.

- trade_count
- net_pnl
- max_drawdown_pct
- win_rate
- profit_factor
- entry_type_counts
- exit_reason_counts

## 실험 배치 실행

```bash
python scripts/run_strategy_experiments.py --symbols BTCUSDT --start 2026-04-01 --end 2026-04-30 --output-dir reports/strategy_experiments
```

기본 variant는 baseline, pullback_conservative, pullback_balanced, pullback_aggressive 이다. 각 variant는 pullback 재가속 관련 env override만 바꾸고 같은 백테스트 엔진을 재사용한다.

## 현재 한계점

- live 환경의 실주문 IOC 재시도와 실제 미체결 잔량 변동은 simulator가 완전히 재현하지 않는다.
- historical provider의 order book은 현재 top-of-book 수준의 단순 모델이다.
- funding fee, liquidation, maintenance margin, ADL은 아직 반영하지 않는다.
- 실시간 계좌 동기화에서 사용하는 user trade 집계는 백테스트에서 빈 값으로 처리된다.

## 다음 개선 방향

- historical order book depth와 체결량 기반 체결 모델 추가
- funding fee와 liquidation 모델 추가
- symbol rule 자동 수집과 월별 캐시 고도화
- decision_log에 strategy score 세부 항목별 증감 기록 추가
- multi-symbol portfolio exposure 제한 로직 추가