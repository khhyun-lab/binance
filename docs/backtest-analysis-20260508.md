# 2026-05-08 백테스트 분석 요약

이 문서는 2026년 4월 BTCUSDT 단일 심볼 백테스트를 기준으로 최근 전략 조정 결과를 정리한다.

## 실행 기준

- 심볼: BTCUSDT
- 기간: 2026-04-01 ~ 2026-04-30
- 초기 자본: 1000 USDT
- 레버리지: 7배
- 거래당 증거금: 25 USDT
- 워밍업 캔들: 1300

## 리포트 비교

| 버전 | 리포트 경로 | 거래 수 | 순손익 | 비고 |
| --- | --- | ---: | ---: | --- |
| baseline | reports/backtests_202604/20260507T134741Z | 82 | -299.9361 | 과다 매매, 비용과 연속 손실이 큼 |
| v2 | reports/backtests_202604_v2/20260507T135827Z | 9 | -71.6253 | 진입 필터 강화 후 거래 급감, 전부 손실 |
| v5 | reports/backtests_202604_v5/20260507T142105Z | 3 | -19.3131 | 현재까지 손실 최소 상태 |
| v6 | reports/backtests_202604_v6/20260507T142945Z | 3 | -19.3131 | TP/SL 조정했지만 v5와 동일 |
| v7 | reports/backtests_202604_v7/20260507T144525Z | 3 | -21.4906 | 롱 breakout_failure 완화, 성과 악화 |
| v8 | reports/backtests_202604_v8/20260507T145811Z | 3 | -19.3131 | 점수 임계값 완화 시도, 실제 결과 변화 없음 |
| v9 | reports/backtests_202604_v9/20260507T150801Z | 6 | -49.0019 | 거래량 기준 완화로 거래 수 증가, 성과 크게 악화 |

## pullback 재가속 실험

2026-05-08에 pullback 재가속 진입 구조를 추가한 뒤 동일한 2026-04 BTCUSDT 조건으로 variant 실험을 다시 수행했다.

| variant | 리포트 경로 | 거래 수 | 순손익 | 최대 낙폭 | 진입 유형 | 비고 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| baseline | reports/strategy_experiments_202604/baseline/20260508T075659Z | 6 | -15.7599 | 3.4074 | breakout_chase_long 4, breakout_chase_short 2 | 기존 breakout만 체결 |
| pullback_conservative | reports/strategy_experiments_202604/pullback_conservative/20260508T080359Z | 6 | -15.7599 | 3.4074 | breakout_chase_long 4, breakout_chase_short 2 | baseline과 완전 동일 |
| pullback_balanced | reports/strategy_experiments_202604/pullback_balanced/20260508T081057Z | 6 | -15.7599 | 3.4074 | breakout_chase_long 4, breakout_chase_short 2 | baseline과 완전 동일 |
| pullback_aggressive | reports/strategy_experiments_202604/pullback_aggressive/20260508T081755Z | 6 | -15.7599 | 3.4074 | breakout_chase_long 4, breakout_chase_short 2 | baseline과 완전 동일 |

### decision log 요약

pullback_balanced variant의 decision log를 analyze_decisions.py로 집계한 결과는 다음과 같다.

- 전체 row: 41900
- entry_type_candidate: pullback_reaccel_long 484, pullback_reaccel_short 259
- pullback_candidate_rows: 743
- pullback_valid_rows: 280
- reaccel_valid_rows: 315
- pullback_min_rr_blocked: 4
- 실제 체결: breakout_chase_long 4, breakout_chase_short 2

즉, pullback 후보 자체는 존재했지만 실제 주문으로 이어질 만큼 구조적 RR과 재가속 조건을 동시에 만족한 케이스는 없었다. 현재 구현은 거래 수를 무리하게 늘리지 않으면서도, 0건 문제를 진단할 수 있는 후보/차단 사유를 decision log에 남기는 단계까지는 도달했다.

## 핵심 해석

### 1. baseline 대비 큰 개선은 있었지만 아직 수익 전략은 아님

- baseline은 82건 거래에 순손익 -299.9361 USDT였다.
- 진입 품질을 크게 강화한 뒤 v5/v6에서 3건, -19.3131 USDT까지 손실 폭을 줄였다.
- 즉, 무차별 진입을 줄인 방향 자체는 맞았다.

### 2. 모든 거래가 손실인 이유는 TP/SL보다 진입과 early exit 구조에 가까웠다

- v2부터 v6까지 남은 거래 대부분은 breakout_failure로 종료됐다.
- v5와 v6 결과가 완전히 동일하다는 점은 TP/SL 조정이 실제 청산에 거의 개입하지 못했다는 뜻이다.
- 실제 지배 청산 조건은 breakout_failure였다.

### 3. 롱 breakout_failure 완화는 해법이 아니었다

- v7은 롱 포지션에 한해 breakout_failure를 더 늦게 허용하는 실험이었다.
- 결과는 v6 대비 손실 확대였다.
- 작은 손실을 더 오래 끌었을 뿐, 수익 전환 효과는 없었다.

### 4. 거래 수를 늘리기 위해 문턱만 낮추는 것도 해법이 아니었다

- v8은 점수 임계값과 점수 우위를 완화했지만 실제 거래 수 변화가 없었다.
- 실제 병목은 점수 임계값보다 breakout 준비 조건이었다.
- v9는 거래량 기준을 완화해 거래 수를 6건까지 늘렸지만, 모두 손실이어서 순손익이 -49.0019 USDT로 악화됐다.

## 현재 결론

- 2026-04 월간 실험 기준 현재 best-known 상태는 여전히 v6 계열이다.
- 새 pullback 재가속 구조는 아직 체결 기여를 만들지 못했고, 결과적으로 baseline과 동일한 거래/손익을 기록했다.
- 채택한 방향:
  - 멀티타임프레임 정렬 기반 진입 강화
  - 추격 진입 억제
  - 과열 롱 진입 억제
  - breakout_failure와 near_target_fade를 포함한 보수적 청산
  - pullback 후보와 blocker를 decision log에 남기는 진단 구조 추가
- 기각한 방향:
  - 롱 breakout_failure 완화
  - 진입 점수 임계값 단순 하향
  - breakout 거래량 하한 완화

## 다음 개선 방향

현재 문제는 거래 수 부족 자체보다, 진입 문턱을 낮췄을 때 손실 진입만 늘어난다는 점이다.

다음 실험 우선순위:

1. 돌파 직후 추격형만 허용하지 말고, 돌파 후 첫 눌림 뒤 재가속 진입 타입을 별도 추가
2. pullback 후보 743건 중 실제 체결 0건이 된 병목을 RR, reaccel, volume 조건 중 어디서 더 많이 잃는지 추가 분해
3. 현재 전략을 저빈도 추세추종형으로 인정할지, 단타형으로 유지할지 전략 성격을 먼저 확정
4. breakout_failure 이전에 진입 품질을 더 정교하게 판별할 수 있는 눌림 구조 점수 추가

## 운영 판단

- 실거래 반영 기준으로는 v6가 가장 안전하다.
- 단타봇으로 재정의하려면 거래 수를 억지로 늘리는 완화가 아니라, 다른 진입 구조를 새로 추가해야 한다.