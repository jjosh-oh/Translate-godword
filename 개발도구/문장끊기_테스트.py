# -*- coding: utf-8 -*-
"""server.py에 들어간 Segmenter를 그대로 꺼내서 검증한다.
(서버를 띄우지 않고 클래스 소스만 실행)"""
import io
import os
import re
import sys
from collections import deque as _deque

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = io.open(os.path.join(ROOT, "배포빌드", "server.py"), encoding="utf-8").read()
start = SRC.index("_SENT_END_CHARS =")
end = SRC.index('@sock.route("/audio")')
ns = {"re": re, "_deque": _deque}
exec(SRC[start:end], ns)
Segmenter = ns["Segmenter"]

fails = []


def check(name, ok, detail=""):
    print(("  통과  " if ok else "  실패  ") + name + (("  → " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def growing(full, step, t0=0.0, dt=0.4):
    """한 글자/한 단어씩 자라는 중간 결과를 흉내낸다."""
    if step == "word":
        w = full.split(" ")
        return [(t0 + i * dt, " ".join(w[:i])) for i in range(1, len(w) + 1)]
    return [(t0 + i * dt, full[:i]) for i in range(1, len(full) + 1)]


# ── 1. 한국어: 마침표가 제때 붙는 평소 상황 ────────────────────────────
print("\n[1] 한국어 · 마침표가 제때 붙는 경우")
KO = "형제 여러분 말씀은 씨앗과 같습니다. 우리가 성경을 읽을 때 씨앗을 만납니다. 오늘 그 약속을 받으십시오."
seg = Segmenter(0.0)
out = []
for t, tr in growing(KO, "char", dt=0.1):
    out += [(t, s) for s in seg.on_interim(tr, t)]
out += [(99.0, s) for s in seg.on_final(KO, 99.0)]
for t, s in out:
    print("    %5.1fs  %s" % (t, s))
joined = " ".join(s for _, s in out)
check("문장이 온전히 나온다", all(s.rstrip().endswith(".") for _, s in out))
check("원문이 그대로 재조합된다", " ".join(joined.split()) == " ".join(KO.split()),
      joined[:60])
check("중복 없음", len(set(s for _, s in out)) == len(out))

# ── 2. 스페인어: 마침표가 전혀 안 붙는 긴 발화 ─────────────────────────
print("\n[2] 스페인어 · 마침표 없이 60초 이어지는 발화")
ES = ("lobos semilla rima semillas plantadas en nuestro corazon por medio del contacto "
      "que nosotros tengamos con la palabra en la vida que uno va leyendo la Palabra "
      "no se si les ha pasado que uno lee un versiculo y de repente entiende todo")
seg = Segmenter(0.0)
out = []
for t, tr in growing(ES, "word", dt=1.2):
    out += [(t, s) for s in seg.on_interim(tr, t)]
last = growing(ES, "word", dt=1.2)[-1][0] + 0.5
out += [(last, s) for s in seg.on_final(ES, last)]
prev, worst = 0.0, 0.0
for t, s in out:
    worst = max(worst, t - prev)
    prev = t
    print("    %5.1fs  %s" % (t, s))
joined = " ".join(s for _, s in out)
check("자막 공백이 12초를 넘지 않는다", worst <= 12.0, "최대 %.1f초" % worst)
check("원문이 그대로 재조합된다 (누락·중복 없음)",
      " ".join(joined.split()) == " ".join(ES.split()))

# ── 3. 음성활동 종료(VAD) 신호로 즉시 끊기 ─────────────────────────────
print("\n[3] 말이 멈췄다는 신호가 오면 즉시 내보낸다")
seg = Segmenter(0.0)
seg.on_interim("hermanos la palabra de Dios es", 1.0)
seg.on_interim("hermanos la palabra de Dios es viva", 1.4)
got = seg.on_speech_end(1.6)
print("    1.6s  %s" % got)
check("마침표도 7초도 기다리지 않고 나온다", got == ["hermanos la palabra de Dios es viva"],
      str(got))

# ── 4. 인식기가 앞 단어를 고쳐도 깨지지 않는다 ─────────────────────────
print("\n[4] 인식 결과가 도중에 수정되는 경우 (LocalAgreement)")
seg = Segmenter(0.0)
emitted = []
for t, tr in [(0.4, "그는 정원에서"), (0.8, "그는 정원에서 기도"),
              (1.2, "그는 동산에서 기도하셨습니다."),   # '정원'→'동산' 수정
              (1.6, "그는 동산에서 기도하셨습니다.")]:
    emitted += seg.on_interim(tr, t)
emitted += seg.on_final("그는 동산에서 기도하셨습니다.", 2.0)
for s in emitted:
    print("    %s" % s)
check("수정 전 잘못된 단어를 내보내지 않는다",
      not any("정원" in s for s in emitted), str(emitted))
check("최종 문장이 온전하다",
      " ".join(" ".join(emitted).split()) == "그는 동산에서 기도하셨습니다.", str(emitted))

# ── 5. 같은 발화 안에서 반복되는 강조 문구는 살려야 한다 ───────────────
print("\n[5] 설교자가 같은 문장을 반복할 때")
seg = Segmenter(0.0)
R = "preciosas promesas. preciosas promesas."
emitted = []
for t, tr in growing(R, "char", dt=0.1):
    emitted += seg.on_interim(tr, t)
emitted += seg.on_final(R, 9.0)
print("    %s" % emitted)
check("반복이 통째로 사라지지는 않는다", len(" ".join(emitted)) >= len("preciosas promesas."),
      str(emitted))

print("\n" + "=" * 60)
print("실패 %d건" % len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
