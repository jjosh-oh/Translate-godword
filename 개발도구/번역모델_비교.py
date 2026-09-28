# -*- coding: utf-8 -*-
"""번역 모델 비교 — 지난 예배 로그의 입력을 그대로 여러 모델에 다시 넣어 본다.

**API를 실제로 부른다. 60문장이면 약 $0.55, 10분쯤 걸린다.** 먼저 물어보고 돌릴 것.

로그.txt에서 가장 최근의 긴 예배(100문장 이상)를 찾아, 지정한 위치부터 N문장을
모델마다 번갈아 번역한다. 프롬프트는 배포빌드/server.py의 translate_and_stream()을
그대로 옮겨 적었다(대응표 + 직전 3문장 문맥). **그쪽 프롬프트를 바꾸면 여기도 바꿀 것.**

    python 개발도구/번역모델_비교.py [시작문장=300] [개수=60] [결과.json]

재는 것: 첫 글자까지 걸린 시간(자막이 뜨기 시작하는 때), 전체 시간, 출력 토큰, 비용.
품질은 결과 JSON을 보고 사람이 판단한다. 볼 것은 셋:
  - 주어지지 않은 말을 덧붙이는지(앞 문맥 반복, 성경 구절 이어 쓰기)
  - 번역 대신 안내문("Please provide the Korean text…")을 내는지
  - 과부하 오류(overloaded)가 SDK 재시도 뒤에도 남는지 — 예배라면 자막이 빠진다

2026-09-28 결과: Opus 4.8 유지. Sonnet 5는 덧붙임 약 8건/60, Opus 5.5는 첫 글자 1.8초.
관련: 번역음성_비교.py
"""
import os, re, sys, json, time, statistics
from dotenv import dotenv_values
import anthropic

APP = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LiveWord")
START = int(sys.argv[1]) if len(sys.argv) > 1 else 300
N = int(sys.argv[2]) if len(sys.argv) > 2 else 60
OUT = sys.argv[3] if len(sys.argv) > 3 else "번역모델_비교.json"

SRC, TGT = "Korean", "English"
# (이름, 모델, 추가 인자). 앞의 둘은 같은 설정을 두 번 — 모델 자체의 편차를 보려고.
CONFIGS = [
    ("opus-4-8 (현행) 1회", "claude-opus-4-8", {}),
    ("opus-4-8 (현행) 2회", "claude-opus-4-8", {}),
    ("sonnet-5 생각끔", "claude-sonnet-5", {"thinking": {"type": "disabled"}}),
    ("sonnet-5 low", "claude-sonnet-5", {"output_config": {"effort": "low"}}),
    # Opus 5.5는 생각하기를 끌 수 없다(disabled는 400). low가 가장 가볍다.
    ("opus-5-5 low", "claude-opus-5-5", {"output_config": {"effort": "low"}}),
]
PRICE = {"claude-opus-4-8": (5, 25), "claude-sonnet-5": (2, 10), "claude-opus-5-5": (4, 20)}

HANGUL = re.compile(r"[가-힣]")


def load_mapping():
    """값이 영어인 대응표 항목만 프롬프트에 들어간다(한글 값은 인식 교정용)."""
    m = {}
    for fn in ("mapping.txt", "mapping_주간.txt"):
        p = os.path.join(APP, fn)
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="ignore"):
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    m[k.strip()] = v.strip()
    return {k: v for k, v in m.items() if not HANGUL.search(v)}


def last_service(lines):
    """'누적 $'가 줄어드는 곳이 프로그램을 새로 켠 자리다. 100문장 넘는 마지막 구간."""
    sess, cur, prev = [], None, 1e9
    for i, l in enumerate(lines):
        m = re.search(r"누적 \$([\d.]+)", l)
        if m:
            v = float(m.group(1))
            if cur is None or v < prev - 0.001:
                cur = [i, i, 0]
                sess.append(cur)
            cur[1], cur[2], prev = i, cur[2] + 1, v
    big = [s for s in sess if s[2] >= 100]
    if not big:
        sys.exit("로그에 100문장 넘는 예배가 없습니다.")
    return big[-1][0], big[-1][1]


def main():
    key = dotenv_values(os.path.join(APP, ".env")).get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key)
    lines = open(os.path.join(APP, "로그.txt"), encoding="utf-8").read().splitlines()
    a, b = last_service(lines)
    # 로그의 '입력'은 인식 교정이 끝난 글이다 — 다시 교정하지 않는다
    inputs = [l.split(" 입력: ", 1)[1] for l in lines[a:b] if " 입력: " in l]
    logged = [l.split(" 번역: ", 1)[1] for l in lines[a:b] if " 번역: " in l]
    print(f"로그 {a}~{b}줄, 문장 {len(inputs)}개 중 {START}번부터 {N}개")

    base = (
        f"You are a professional church interpreter providing live subtitles. "
        f"Translate ONLY the exact given text from {SRC} into {TGT}. "
        f"Output ONLY the translation of what is given — never add, continue, complete, "
        f"or quote additional text. If the input is a short or partial sentence (e.g. part of "
        f"a Bible verse), translate only that fragment; do NOT finish the verse or add the rest. "
        f"No explanations or commentary."
    )
    mapping = load_mapping()
    if mapping:
        base += ("\n\nName/term translation table (use these EXACT translations when the term appears):\n"
                 + "\n".join(f"  {k} → {v}" for k, v in mapping.items()))

    rows = []
    for i in range(START, min(START + N, len(inputs))):
        text = inputs[i]
        ctx = " ".join(inputs[max(0, i - 3):i])
        system = base + (
            f"\n\nPreceding {SRC} text (context ONLY — do NOT translate, repeat, or output it; "
            f"use it only so pronouns, referents, terms, and sentence flow stay natural):\n{ctx}"
            if ctx else "")
        max_out = max(256, min(1024, len(text) * 4))
        row = {"i": i, "src": text, "logged": logged[i] if i < len(logged) else "", "res": {}}
        # 한 문장을 모든 설정에 번갈아 넣는다 — 시간대에 따른 서버 부하 차이가 고르게 섞이도록
        for label, model, extra in CONFIGS:
            # 생각하기가 켜지면 생각 토큰도 max_tokens에 들어가므로 여유를 준다
            thinks = extra and "thinking" not in extra
            t0, first, out = time.time(), None, []
            try:
                with client.messages.stream(model=model, max_tokens=max_out + (2048 if thinks else 0),
                                            system=system,
                                            messages=[{"role": "user", "content": text}],
                                            **extra) as s:
                    for ch in s.text_stream:
                        if first is None:
                            first = time.time() - t0
                        out.append(ch)
                    fm = s.get_final_message()
                u = fm.usage
                pi, po = PRICE[model]
                row["res"][label] = dict(out="".join(out), ttft=first, total=time.time() - t0,
                                         inp=u.input_tokens, outp=u.output_tokens,
                                         cost=(u.input_tokens * pi + u.output_tokens * po) / 1e6)
            except Exception as e:
                row["res"][label] = dict(err=f"{type(e).__name__}: {e}"[:300])
        rows.append(row)
        print(f"  {i}", flush=True)

    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n설정별 요약 (800문장 = 한 예배 환산)")
    for label, _, _ in CONFIGS:
        ok = [r["res"][label] for r in rows if "err" not in r["res"][label]]
        err = len(rows) - len(ok)
        if not ok:
            print(f"  {label}: 전부 실패")
            continue
        tt = sorted(x["ttft"] for x in ok if x["ttft"] is not None)
        cost = sum(x["cost"] for x in ok)
        print(f"  {label:20s} 첫글자 중앙 {statistics.median(tt):.2f}초 · p90 {tt[int(len(tt)*.9)-1]:.2f}초"
              f" · 최대 {tt[-1]:.2f}초 | 출력토큰 {statistics.mean(x['outp'] for x in ok):.0f}"
              f" | 800문장 ${cost/len(ok)*800:.2f} | 오류 {err}")
    print(f"\n문장별 번역은 {OUT}")


if __name__ == "__main__":
    main()
