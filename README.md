# LiveWord

한국어 설교를 실시간으로 인식·번역해 **송출 화면과 교인 휴대폰에 자막과 음성으로** 전달하는
Windows 데스크톱 프로그램입니다.

운영자가 마이크를 선택하면 설교가 실시간으로 번역되어, 스크린에는 대표 언어 자막이 나가고
교인은 QR로 접속해 **각자 원하는 언어**로 자막을 보거나 이어폰으로 음성을 들을 수 있습니다.

지원 언어 — 영어 · 한국어 · 일본어 · 중국어 · 스페인어 · 프랑스어 · 독일어 · 아랍어

---

## 설치해서 쓰기만 할 경우

개발이 필요 없다면 [최신 릴리스](https://github.com/jjosh-oh/Translate-godword/releases/latest)에서
설치 파일을 내려받으면 됩니다. 압축을 풀 때 비밀번호가 필요하니 배포자에게 문의하세요.

설치 후 프로그램을 처음 실행하면 설정 화면이 나옵니다. 아래 세 서비스의 키를 입력하면 바로 사용할 수 있습니다.

| 서비스 | 용도 | 발급처 |
|---|---|---|
| Anthropic API | 번역 | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| Google Cloud | 음성 인식(STT) + 번역 음성(TTS) | [console.cloud.google.com](https://console.cloud.google.com) |
| ngrok | 교인 휴대폰 외부 접속 | [dashboard.ngrok.com](https://dashboard.ngrok.com/get-started/your-authtoken) |

세 서비스 모두 사용한 만큼 과금됩니다. 비영리단체 지원 프로그램(Goodstack 경유) 대상이 될 수 있습니다.

---

## 개발 환경 준비

새 PC에서 이어받아 개발할 때의 순서입니다.

### 1. 코드 받기

```bash
git clone https://github.com/jjosh-oh/Translate-godword.git C:\Claude
cd C:\Claude
```

### 2. 파이썬 패키지 설치

Python 3.13 기준입니다.

```bash
pip install -r 배포빌드/requirements.txt
pip install pyinstaller
```

### 3. 빌드 도구 설치

설치 프로그램을 만들려면 [Inno Setup 6](https://jrsoftware.org/isdl.php)이 필요합니다.

### 4. git 에 없는 파일 준비

용량과 보안 때문에 저장소에 포함하지 않은 파일들입니다.

| 파일 | 구하는 방법 |
|---|---|
| `배포빌드/ngrok.exe` | [ngrok.com/download](https://ngrok.com/download) 에서 내려받아 `배포빌드/` 에 둡니다 (약 31MB) |
| Google 서비스 계정 JSON | Google Cloud 콘솔에서 발급 |
| `.env` | `.env.example` 을 복사해 채우거나, 프로그램의 설정 화면에서 입력 |

### 5. 개발 중 실행

```bash
cd 배포빌드
python server.py
```

브라우저에서 `http://localhost:5000/operator` 로 접속합니다.

---

## 빌드

```bash
# 실행 중이면 먼저 종료 (파일이 잠겨 빌드가 실패합니다)
taskkill //F //IM LiveWord.exe
taskkill //F //IM ngrok.exe

# exe 빌드
cd 배포빌드
python -m PyInstaller --noconfirm LiveWord.spec

# 설치 프로그램 생성 → 배포빌드/installer/LiveWord_Setup.exe
"$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe" LiveWord.iss
```

HTML만 고쳤다면 다시 빌드할 필요 없이 설치 폴더의 `_internal/` 에 덮어쓰면 즉시 반영됩니다.

---

## 화면 구성

| 주소 | 화면 | 쓰는 사람 |
|---|---|---|
| `/operator` | 운영자 대시보드 | 음향 담당자 — 마이크·언어 설정, 음성 켜기 |
| `/` | 송출 화면 | 스크린 · OBS 브라우저 소스 |
| `/mobile` | 휴대폰 화면 | 교인 — 언어 선택, 음성 재생 |
| `/setup` | 설정 화면 | 최초 설치 시 API 키 입력 |
| `/guide`, `/poster` | 교인용 안내문 · 포스터 | 인쇄해서 배포 |

---

## 이어받아 작업하는 분께

**[`CLAUDE.md`](CLAUDE.md) 를 먼저 읽어 주세요.** 프로젝트 구조, 빌드 절차,
그리고 실제로 시간을 많이 쓴 함정들이 정리되어 있습니다. 특히 중요한 두 가지만 미리 적어 둡니다.

- **코드가 두 벌입니다.** 루트의 `server.py`(개발용)와 `배포빌드/server.py`(배포용)는 별개 파일이며
  자동으로 동기화되지 않습니다. 기능을 고칠 때는 양쪽 모두 수정해야 합니다.
- **교회에서 매주 실제로 사용 중입니다.** 요청받은 범위 밖은 먼저 확인하고, 잘 돌아가는 부분은
  그대로 두고 새 작업은 브랜치에서 진행하는 방식을 권합니다.
