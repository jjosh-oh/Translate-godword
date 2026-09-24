# -*- coding: utf-8 -*-
"""구글 인증 파일을 고르는 규칙을 검사한다. API를 부르지 않으므로 비용 0.

키를 교회 것으로 갈아 끼울 때 걸리는 함정이 있었다. 설정 화면에서 서비스 계정
JSON을 올리면 google-key.json으로 저장되는데, .env에 GOOGLE_APPLICATION_CREDENTIALS가
남아 있으면 껐다 켠 뒤 **그 옛 경로가 이겼다.** 오류가 안 뜨므로 알아채기 어렵다.

이 도구는 server.py에서 그 판단 부분만 꺼내 실제로 실행해, 네 가지 경우에
무엇을 고르는지 확인한다. 규칙이 다시 뒤집히면 여기서 실패한다.

    python 개발도구/구글인증_점검.py

관련: CLAUDE.md '겪었던 함정'
"""
import io
import json
import os
import sys
import shutil
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "배포빌드", "server.py")

START = "def _strip_bom("
END = "# 이 프로그램의 버전"

fails = []


def check(name, ok, detail=""):
    print(("  통과  " if ok else "  실패  ") + name + (("  → " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def 판단(app_dir, env_cred, 키파일있음):
    """server.py의 판단 부분을 그대로 실행해 결과를 돌려준다."""
    src = io.open(DEPLOY, encoding="utf-8").read()
    if START not in src or END not in src:
        raise RuntimeError("판단 부분을 찾지 못했습니다 (server.py 구조가 바뀌었습니까?)")
    block = src[src.index(START):src.index(END)]

    if 키파일있음:
        with open(os.path.join(app_dir, "google-key.json"), "w") as f:
            f.write('{"type":"service_account"}')

    환경 = {}
    if env_cred:
        환경["GOOGLE_APPLICATION_CREDENTIALS"] = env_cred
    ns = {"os": os, "APP_DIR": app_dir, "os_environ_backup": None}
    # os.environ을 건드리지 않도록 가짜 환경을 쓴다
    class _Env(dict):
        pass
    fake = _Env(환경)
    real_environ = os.environ
    try:
        os.environ = fake
        exec(block, ns)
    finally:
        os.environ = real_environ
    return fake.get("GOOGLE_APPLICATION_CREDENTIALS", ""), ns.get("_cred_note", "")


print("=" * 66)
print("구글 인증 파일 고르는 규칙")
print("=" * 66)

tmp = tempfile.mkdtemp()
try:
    옛경로 = os.path.join(tmp, "옛_개인인증.json")
    io.open(옛경로, "w", encoding="utf-8").write("{}")
    키파일 = os.path.join(tmp, "google-key.json")

    # ① 둘 다 있으면 google-key.json이 이겨야 한다 (이번에 고친 핵심)
    고른것, 알림 = 판단(tmp, 옛경로, True)
    check("① .env에 옛 경로가 있어도 google-key.json을 쓴다",
          os.path.normcase(고른것) == os.path.normcase(키파일),
          os.path.basename(고른것))
    check("① 무엇을 대신 썼는지 알려준다", "google-key.json" in 알림, 알림[:60])

    # ② google-key.json만 있을 때
    os.remove(키파일)
    고른것, _ = 판단(tmp, "", True)
    check("② .env가 비어 있으면 google-key.json을 쓴다",
          os.path.normcase(고른것) == os.path.normcase(키파일))

    # ③ .env 경로만 있을 때는 그대로 둔다 (gcloud 사용자 인증 쓰는 PC)
    os.remove(키파일)
    고른것, _ = 판단(tmp, 옛경로, False)
    check("③ google-key.json이 없으면 .env 경로를 그대로 쓴다",
          os.path.normcase(고른것) == os.path.normcase(옛경로))

    # ④ .env가 없는 파일을 가리키면 알려준다
    없는경로 = os.path.join(tmp, "사라진파일.json")
    고른것, 알림 = 판단(tmp, 없는경로, False)
    check("④ .env가 없는 파일을 가리키면 알려준다", "찾을 수 없" in 알림, 알림[:60])

    # ⑤ BOM이 붙은 키 파일을 제자리에서 고친다
    #    구글 라이브러리는 BOM이 붙은 JSON을 못 읽는다. 메모장으로 열어 저장하면 붙는다.
    본문 = '{"type":"service_account","project_id":"bom-test"}'
    for 이름, bom, enc in (("UTF-8 BOM", b"\xef\xbb\xbf", "utf-8"),
                          ("UTF-16 BOM", b"\xff\xfe", "utf-16-le")):
        with open(키파일, "wb") as f:
            f.write(bom + 본문.encode(enc))
        판단(tmp, "", False)                  # 키 파일은 이미 만들어 두었다
        raw = open(키파일, "rb").read()
        붙어있나 = (raw.startswith(b"\xef\xbb\xbf") or raw.startswith(b"\xff\xfe")
                  or raw.startswith(b"\xfe\xff"))
        check("⑤ %s가 붙어 있으면 떼어 낸다" % 이름, not 붙어있나)
        try:
            읽힘 = json.loads(open(키파일, "rb").read().decode("utf-8"))
            check("⑤ %s 파일이 고친 뒤 읽힌다" % 이름, 읽힘.get("project_id") == "bom-test")
        except Exception as e:
            check("⑤ %s 파일이 고친 뒤 읽힌다" % 이름, False, str(e)[:60])
        if os.path.exists(키파일):
            os.remove(키파일)

    # ⑥ BOM이 없는 정상 파일은 건드리지 않는다
    with open(키파일, "wb") as f:
        f.write(본문.encode("utf-8"))
    전 = open(키파일, "rb").read()
    판단(tmp, "", False)
    check("⑥ 멀쩡한 파일은 손대지 않는다", open(키파일, "rb").read() == 전)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 66)
print("실패 %d건" % len(fails))
for f in fails:
    print("  -", f)
if fails:
    print("\n키를 갈아 끼울 때 새 키가 조용히 무시되는 상태입니다.")
sys.exit(1 if fails else 0)
