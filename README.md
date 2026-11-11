# MERRY AI DIGEST (test repository)

Independent from `ai-secretary` and `써보니`.

## Setup
1. Create a new public GitHub repository named `merry-ai-digest` (do not modify existing repos).
2. Upload these files, preserving `.github/workflows/digest.yml`.
3. GitHub repository Settings → Secrets and variables → Actions → New repository secret: `GEMINI_API_KEY`.
4. Actions → MERRY AI DIGEST → Run workflow → `DIRECT_URL` → enter a YouTube URL with available subtitles.
5. Download the run's artifact. It contains six PNGs, `sources.json`, `digest.json`, and `merry_ai_digest.zip`.

## Notes
- YouTube subtitle access from GitHub-hosted runners may be blocked; failure is reported, not fabricated.
- `LATEST_AI_YOUTUBE` is an initial verified-channel subset, not yet the requested full ten.
- `GITHUB` uses repositories created in the last 7 days ranked by current stars. It is **not** official weekly trending.
- Public repository workflow source is visible. Never commit API keys.
- GitHub Actions and Gemini API quotas/pricing must be checked on the actual accounts.
- This code has not been run against live external services in this environment.


## 주간 메일 (WEEKLY_YOUTUBE) — 매주 월요일 오전 7시 자동
- 10개 채널(`channels.json`)의 최근 7일 영상 중 쇼츠를 빼고, 채널당 1편씩 조회수 높은 순으로 5편을 고릅니다.
- 영상마다 Gemini가 "핵심 한 장" 요약 + 100점 평가(우리 일 25 / 바뀌는 정도 30 / 작동 근거 20 / 시작 쉬움 15 / 계속 쓰기 10)를 만듭니다. 합계는 코드가 계산하고, 써 보기 전이라 69점이 상한입니다.
- 6번 채널(AI한테 시켜봤습니다) 쇼츠 소재 후보 한 줄도 같이 만듭니다.
- 결과: 지메일 1통(요약 글 + 이미지 + 캘린더 일정 파일 .ics + 구글 캘린더 일정 넣기 링크), `weekly/날짜.md`와 `weekly/latest.md`에 보관, 실행 결과 ZIP.
- 메일을 받으려면 저장소 Settings → Secrets and variables → Actions에 비밀 2개를 넣습니다 (값은 저장소 주인만 직접 입력):
  - `GMAIL_ADDRESS`: 보내고 받을 지메일 주소
  - `GMAIL_APP_PASSWORD`: 구글 계정의 "앱 비밀번호" 16자리 (2단계 인증 필요)
- 비밀이 없으면 메일만 건너뛰고 나머지는 그대로 동작합니다.
- 조절(선택): `WEEKLY_COUNT`(편수, 기본 5), `WEEKLY_PER_CHANNEL`(채널당, 기본 1), `WEEKLY_EVENT_HOUR`(일정 시각, 기본 22시), `WEEKLY_PAUSE`(영상 사이 쉬는 초, 기본 20).
