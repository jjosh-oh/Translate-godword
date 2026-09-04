# LiveWord — 실시간 설교 통역 프로그램

한국어 설교를 실시간으로 인식·번역해 송출 화면과 교인 휴대폰에 **자막과 음성**으로 전달하는
Windows 데스크톱 프로그램. SAEROUN Reformed Church에서 매주 실제 예배에 사용 중이며,
한 선교사에게 기부 예정.

**현재 버전: v1.1** · 저장소: `jjosh-oh/Translate-godword`

---

## 작업 원칙 (가장 중요)

교회에서 매주 실제로 쓰는 프로그램이다. 예배 중 고장 나면 대안이 없다.

1. **요청한 것만 수정한다.** 그 외에 손볼 필요가 보이면 **먼저 물어본다.**
2. **잘 돌아가는 기존 부분은 건드리지 않는다.** 새 기능은 브랜치에서 따로 진행한다.
3. **API 키를 대화창·코드·git에 절대 붙여넣지 않는다.** `.env` 또는 설정(setup) 화면으로만 입력.
4. **빌드했으면 반드시 실행해서 확인하고 넘긴다.** "빌드 성공"은 동작 확인이 아니다.
5. 사용자는 한국어로 대화한다. 답변도 한국어로.

---

## ⚠️ 코드가 두 벌이다 (1순위 함정)

| 경로 | 역할 |
|---|---|
| `배포빌드\server.py` | **배포용 본체.** 실제 .exe에 들어감 (약 1,340줄) |
| `server.py` | 개발·실험용 사본. Gemini 비교 코드 등이 남아 배포본과 완전히 같지 않음 |

HTML 파일도 루트와 `배포빌드\`에 각각 있다. **자동 동기화되지 않는다.**

기능을 고칠 때는 **양쪽 모두** 수정할 것. 한쪽만 고치면 "개발에선 되는데 배포하면 안 되는" 현상이 생긴다.
수정 후 `grep`으로 양쪽에 들어갔는지 확인하는 습관을 권한다.

---

## 시스템 구조

```
마이크(브라우저) ─16kHz PCM/WebSocket→ /audio
   → Google Speech-to-Text (스트리밍 인식)
   → 문장부호(. ? !)로 끊어 enqueue_translation()
        ├─ primary_jobs   (대표 언어 — 송출 화면, 빠름)
        └─ secondary_jobs (휴대폰이 고른 다른 언어들)
   → Claude API (claude-opus-4-8) 스트리밍 번역
        ├─ LangHub.publish() → 언어별 자막 전달
        └─ tts_jobs → Google TTS → MP3를 해당 언어 휴대폰으로
```

**핵심 구조 3가지 — 함부로 바꾸지 말 것**

- **LangHub**: 언어별 구독 관리. 휴대폰은 `_lang_subs[언어]`, 송출 화면은 `_primary_subs`(대표 언어를 따라감).
- **큐 분리**: 대표 언어와 보조 언어를 별도 워커로 처리. 휴대폰용 추가 번역이 송출 자막을 늦추지 않게 하려는 설계.
- **TTS 직렬 처리**: `tts_jobs`에서 **한 번에 하나씩** 합성. 동시 처리하면 문장별 합성 시간 차로
  소리 순서가 자막과 어긋난다. (실제로 겪은 버그)

**화면**: `/operator`(운영자) · `/`(송출·OBS) · `/mobile`(교인 휴대폰) · `/setup`(최초 설정)

**지원 언어**: 영어·한국어·일본어·중국어·스페인어·프랑스어·독일어·아랍어.
아무도 고르지 않은 언어는 번역·음성이 실행되지 않아 추가 비용이 0이다.

---

## 빌드와 배포

HTML만 고쳤다면 **재빌드 불필요** — 서버가 요청마다 디스크에서 읽으므로 설치 폴더에 덮어쓰면 즉시 반영:

```bash
cp 배포빌드/mobile.html "$LOCALAPPDATA/Programs/LiveWord/_internal/mobile.html"
```

Python 코드를 고쳤다면 전체 절차:

```bash
# 1. 실행 중 종료 (파일 잠김 방지)
taskkill //F //IM LiveWord.exe; taskkill //F //IM ngrok.exe

# 2. exe 빌드
cd "C:/Claude/배포빌드" && python -m PyInstaller --noconfirm LiveWord.spec

# 3. 설치 프로그램 생성
"$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" LiveWord.iss

# 4. 설치
"C:/Claude/배포빌드/installer/LiveWord_Setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

# 5. 확인 — 응답하면 정상
curl -s http://127.0.0.1:5000/settings
```

**새 버전을 낼 때는 버전을 두 곳 모두 올린다** (하나만 고치면 업데이트 알림이 어긋남):
- `배포빌드\server.py` → `APP_VERSION = "1.2"`
- `배포빌드\LiveWord.iss` → `#define AppVersion "1.2"`

배포 ZIP은 **ZipCrypto** 방식이어야 한다 (아래 함정 참고). 비밀번호는 릴리스 설명에 적지 않고 별도 전달.

**셸**: 여러 줄이나 한글이 들어가는 명령(git 커밋 메시지 등)은 **Bash heredoc**을 쓸 것.
PowerShell은 한글 인코딩·따옴표 문제로 자주 실패한다.

---

## 겪었던 함정

| 증상 | 원인과 대응 |
|---|---|
| 배포본에 수정이 반영 안 됨 | **코드 두 벌.** 양쪽 다 고쳤는지 확인 |
| ZIP 풀 때 `0x80004005` | Windows 기본 압축은 **AES를 지원하지 않음.** ZipCrypto로 만들 것 (Python `zipfile`은 쓰기 미지원 → 직접 구현 필요) |
| `gh release create`가 404 | GitHub 계정이 둘. 저장소 주인은 `jjosh-oh`인데 CLI는 `jjoshoh`(읽기 전용)로 로그인됨. `git credential fill`의 저장된 토큰으로 REST API 호출하면 됨 |
| 음성이 안 나옴 | 재시작하면 `settings`가 초기화되어 `voice: false`. 운영자 화면 토글부터 확인 |
| 아이폰에서 소리 없음 | 무음 스위치 때문. 그래서 **Web Audio(AudioContext)**로 구현했고, 교인이 🔊 버튼을 눌러야 잠금이 풀림 |
| 휴대폰에 옛 화면이 남음 | HTML 응답에 `no-store` 헤더로 해결됨. **이 설정을 제거하지 말 것** |
| 번역이 엉터리 | 대개 Claude가 아니라 **음성인식** 문제. `로그.txt`에서 *입력*이 뭘로 찍혔는지 먼저 볼 것 (예: "Pardon me"→"Garden me") |
| QR 안 뜸 / 송출창 안 열림 | 죽지 않은 `LiveWord.exe`·`ngrok.exe`가 포트 5000을 점유. 둘 다 종료 후 재실행 |
| 번역이 갑자기 멈춤 | 코드 문제가 아니라 **Anthropic 크레딧 소진**인 경우가 많음. 잔액부터 확인 |

**문장 끊는 규칙과 번역 시점 로직**(`/audio`의 `split_sentences`, `is_final` 처리)은 오래 다듬어진 부분이다.
품질 개선은 문맥 전달(직전 3문장)·용어집·대응표 쪽으로 접근할 것.

---

## 사용자 데이터 (지우지 말 것)

`APP_DIR`에 저장된다. `APP_DIR`은 **.exe 폴더**를 우선 사용하고, 쓰기가 막히면 `%APPDATA%\LiveWord`로 넘어간다.

| 파일 | 내용 | 업데이트 시 |
|---|---|---|
| `mapping.txt` | 이름·용어 대응표 (예: 요한=John) | 안전 — 설치 파일에 미포함 |
| `glossary.txt` | 음성인식 힌트용 교회 용어 278개 | 안전 — `onlyifdoesntexist`로 보호됨 |
| `.env` | API 키·토큰 | 안전 — 설치가 건드리지 않음 |
| `로그.txt` | 번역 기록 (입력 원문 → 번역 결과) | **문제 진단 1순위 자료** |

---

## 외부 서비스

| 서비스 | 용도 | 없으면 |
|---|---|---|
| Anthropic API | 번역 (`claude-opus-4-8`) | 번역 안 됨 |
| Google Cloud STT | 음성 인식 | 자막 시작 안 됨 |
| Google Cloud TTS | 번역 음성 합성 | 자막만 나오고 소리 없음 |
| ngrok | 외부 접속 터널(고정 도메인) | QR 접속 불가 |

`.env` 항목은 `.env.example` 참고. 자세한 배경은 `README.md`와 인수인계서를 볼 것.
