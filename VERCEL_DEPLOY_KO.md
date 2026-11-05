# SignalDesk v0.4: Python 설치 없이 GitHub + Vercel 배포

**이 버전은 Vercel에서 Python FastAPI 앱을 서버리스로 실행합니다.** 본인 PC에 Python을 설치할 필요가 없습니다. 아직 **배포된 URL이 아니며**, 사용자의 GitHub/Vercel 계정에서 배포해야 URL이 생성됩니다.

## 준비할 계정
- [GitHub](https://github.com) 회원가입
- [Vercel](https://vercel.com) 회원가입 및 GitHub 연결
- 필요 시 [OpenDART](https://opendart.fss.or.kr/) API 키
- 미국 공시를 위한 SEC 식별 문자열: `SignalDesk contact@your-real-domain.com` 형식 (실제 사용 가능한 연락처)
- 선택: OpenAI API 키, 한국투자증권 KIS 시세 API 키 (미래에셋 거래계좌를 옮길 필요 없음)

## 배포 단계
1. 이 압축파일을 컴퓨터에서 **풀어** 폴더의 `app.py`, `requirements.txt`, `static/`, `analysis_engine.py` 등 **내용물 전체**를 준비합니다. **ZIP 한 개만 GitHub에 업로드하면 동작하지 않습니다.**
2. GitHub > `New repository` > 이름 `signaldesk` > **Private** 선택 > Create repository.
3. 저장소의 `Add file` > `Upload files`에서 압축 해제한 파일과 폴더 구조를 업로드하고 Commit합니다. `.env` 파일은 업로드하지 마세요. 업로드가 불편하면 GitHub Desktop을 이용할 수 있습니다.
4. Vercel > `Add New` > `Project` > `Import Git Repository` > `signaldesk` 선택.
5. FastAPI는 Vercel에서 지원합니다. Framework는 자동 감지 설정을 사용하세요. 별도 빌드 명령을 입력하지 않습니다.
6. Vercel 프로젝트 `Settings > Environment Variables`에 **최소한 **설정할 필요가 없습니다**. 공개 앱에 개인 비밀번호를 입력하지 않습니다**합니다. 공개 버전은 분석 기능을 비밀번호 없이 제공하며 자동 AI 요약은 비활성화합니다. 미니 종목 질문 AI는 OPENAI_API_KEY를 서버 환경변수에 설정한 경우 사용자가 질문할 때만 호출합니다. 또한 `DART_API_KEY`, `SEC_USER_AGENT`를 설정해 공시 근거를 확보하세요. 기타 선택 키: `OPENAI_API_KEY`, `KIS_APP_KEY`, `KIS_APP_SECRET`.
7. 환경변수 추가 후 `Redeploy`하고 `Visit`로 생성된 `https://...vercel.app` URL 접속.
8. 비밀번호 입력 없이 삼성전자(005930), Apple(AAPL) 등으로 테스트합니다.

## 기능과 제한
- 국내·미국 종목 기술지표, 보수적 가격 구간, 최신 뉴스 제목/발행일, 공시 목록 및 일부 공식 재무계정 조회, 최근 3년 기술적 백테스트.
- 관심종목은 **브라우저 `localStorage`에 저장**됩니다. 다른 기기와 동기화되지 않고 브라우저 데이터 삭제 시 사라집니다. 서버 DB가 없기 때문에 서버 간 동기화는 비활성입니다.
- 텔레그램 24시간 가격알림 **아직 미구현**입니다. `worker.py` 같은 영구 실행 프로세스는 Vercel Functions로 계속 실행할 수 없습니다. 장중 주기적 감시에는 별도 백그라운드 서비스(예: Render worker / Railway)와 클라우드 DB(Supabase 등)가 필요합니다.
- Vercel Hobby Cron은 하루 한 번이며 실행 시각이 일정하지 않을 수 있습니다. 따라서 **무료 Cron만으로 실시간 매매 타점을 포착할 수 없습니다.**
- Yahoo Finance는 비공식 확정 일봉이고 Google News RSS는 헤드라인·발행시점만 가져옵니다. 데이터 API가 연결되지 않은 상태에서 **실시간 신호나 보고서 원문 분석을 제공하지 않습니다.**
- 숫자로 표시한 매수 구간은 **실제 기업가치의 적정가가 아니라 기술 분석 가설**입니다. 거래 전 미래에셋 M-STOCK의 실제 시세와 원문을 확인하세요.
- 무료 요금제에는 서버 함수 사용량 등 한도가 있으며 뉴스/AI API 호출 비용이나 라이선스 제약은 별도입니다.
- GitHub 저장소는 Private로 유지하세요. 클라이언트와 API 통신은 HTTPS여도 공개 앱은 누구나 접속할 수 있으므로 호출량·비용 감시가 필요합니다. 자동 AI 요약은 차단하고, 사용자가 요청한 종목 질문만 키가 있을 때 유료 AI를 호출합니다. 질문 호출 제한은 프로세스 단위이며 계정 전체의 과금 한도는 별도로 설정해야 합니다.

## 빠른 오류 점검
- 429: 요청 제한에 도달했습니다. 잠시 후 다시 분석하세요.
- 서버에서 외부 데이터를 조회하지 못한다면 시세/공시 API 상태를 확인하세요.
- 502: 외부 시세/뉴스/공시 API 미응답 또는 제한. Vercel Functions 로그 확인.
- 404 또는 500: 저장소 루트에 `app.py`, `requirements.txt`, `static/`이 있는지 확인. Vercel > Deployments > Function Logs 확인.

공식 문서:
- https://vercel.com/docs/frameworks/backend/fastapi
- https://vercel.com/docs/cron-jobs/usage-and-pricing
- https://vercel.com/kb/guide/is-sqlite-supported-in-vercel
