# StockDesk v0.4 · 국내/미국 주식 매수 구간 근거자료 웹앱

- GitHub → Vercel FastAPI 기반으로 배포. 개인 비밀번호 입력 **없음**.
- 매수 관찰 가격은 20일 SMA와 14일 ATR로 계산하며, 손절 기준·저항·기술 조건 6개·근거 요약을 함께 제시합니다.
- 종목별 근거 자료실: 확정 일봉, 공식 DART/SEC 공시 원문 링크, 표준화된 일부 재무계정, 뉴스 RSS, **리포트 관련 보도**를 각기 다른 신뢰도로 분류.
- 웹에서 `/static/evidence-guide.html` 로 가격 산식·증거 위계·한계·원문 확인 지침을 열 수 있습니다.
- **매수 확정 신호 없음**: 기사·공시의 제목만 확인한 결과는 원문 검증으로 간주하지 않으므로, 기술 조건 충족 시에도 원문 확인을 요구합니다.
- 별도의 로그인 없이 누구나 페이지를 조회할 수 있으므로 **공개 Vercel에서는 유료 OpenAI API 요약 호출을 강제로 차단**합니다. 공개 서버에 OPENAI_API_KEY를 추가하지 않는 것을 권장합니다.
- 요청 제한: 앱 인스턴스 내 분당 24회 수준의 *최선 노력* 요청 제한. 분산 환경에서 완전한 비용/남용 방지는 아니므로 비용 한도를 추가로 설정하세요.
- `DART_API_KEY`, `SEC_USER_AGENT`, 선택적 시세 API 키는 Vercel 환경변수에만 넣으며 GitHub에 업로드하지 마세요.
- 배포 후 Vercel 프로젝트 URL에서 `/api/health`를 열어 서버 상태 확인.
- **24시간 알림, 진짜 틱 단위 실시간 모니터링, 리포트 원본 PDF 자동 분석, 매매 주문 기능은 아직 없습니다.**

## 공식 근거 정보
- OpenDART [공시검색 API](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- 미국 [SEC EDGAR 개발자 안내](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Vercel FastAPI 배포](https://vercel.com/docs/frameworks/backend/fastapi)

## 개발 테스트
`python -m pytest -q` (테스트용 의존 패키지 설치 필요).
