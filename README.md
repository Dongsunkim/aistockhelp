# StockDesk · 데이터 검증 우선 복구 버전 0.4.1

이 저장소는 이전 StockDesk v0.4.0 소스 백업에서 복원한 **부분 복구판**입니다. 이전 프로젝트의 v0.5.4 전체 기능이 그대로 이전된 것이 아닙니다.

- 국내 네이버증권 비공식 확정 OHLCV: 네이버 FCHART → 네이버 모바일 JSON 대체. **Yahoo 국내 대체 금지**
- 미국 Yahoo Finance 비공식 확정 일봉, 국내 네이버 비공식 현재가 참고
- 지표: 이동평균 SMA20/60/120, RSI14, ATR14, 거래량, 거래 위험, 기술적 백테스트
- 공식 DART/SEC 재무 주요계정 및 공시. 금융자료 미확인·기간·통화 불일치면 매매가격 비공개
- 18px 맑은 고딕 중심 가독성, 데이터 품질·경고 노출
- **확정매수/자동주문 기능 없음**. 내재가치 평가 및 증권사 원문 검증 기능은 복구 전입니다.
- 중요: 네이버 비공식 API 구조는 변경될 수 있습니다. 응답을 검증하지 못하면 오류를 반환합니다.

실행: `pip install -r requirements.txt` 후 `uvicorn app:app --reload`. 테스트: `python -m pytest -q`.

OpenDART와 SEC API 키는 Vercel 환경변수로만 제공하고 GitHub에 커밋하지 마세요.

Vercel에서 DART 인증키는 `DART_API_KEY`(권장) 또는 `DART_API`(기존 프로젝트 호환)로 설정합니다. 실제 값은 GitHub에 올리지 않습니다.
