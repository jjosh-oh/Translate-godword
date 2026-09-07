# -*- coding: utf-8 -*-
"""번역 요금 폭증을 미리 막는 점검. API를 부르지 않으므로 비용 0, 1초.

한 시간 예배에 $40이 나온 적이 있다. 원인은 '문장마다 바뀌는 문맥'을
설교 원고(캐시 블록) *앞*에 붙인 것이었다. 프롬프트 캐시는 앞에서부터
같아야 적중하므로, 앞쪽이 바뀌면 원고 5,400토큰이 매 문장 새로 청구된다.

이 도구는 server.py에서 프롬프트를 만드는 부분만 꺼내 실제로 실행한 뒤,
**문맥을 바꿔도 캐시 블록까지가 한 글자도 안 변하는지** 확인한다.
구조가 다시 망가지면 여기서 실패한다.

    python 개발도구/캐시블록_점검.py

관련: CLAUDE.md '겪었던 함정' — 요금이 몇 배로 뜀
"""
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [os.path.join(ROOT, "배포빌드", "server.py"),
           os.path.join(ROOT, "server.py")]

START = "    base_instruction = ("
END = '    hub.publish(target_lang, "__clear__", is_primary)'

SERMON = "누가복음 16장 부자와 나사로. " * 200          # 큰 원고(캐시 대상)
MAPPING = {"요한": "John", "나사로": "Lazarus"}

fails = []


def check(name, ok, detail=""):
    print(("  통과  " if ok else "  실패  ") + name + (("  → " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def build_system(src, context, sermon=SERMON, target="English", source="Korean"):
    """server.py의 프롬프트 조립 부분만 그대로 실행해 system을 얻는다."""
    if START not in src or END not in src:
        raise RuntimeError("프롬프트 조립 부분을 찾지 못했습니다 "
                           "(translate_and_stream의 구조가 바뀌었습니까?)")
    # 함수 안에 있는 코드라 4칸 들여써져 있다 — 벗겨서 그대로 실행한다
    import textwrap
    block = textwrap.dedent(src[src.index(START):src.index(END)])
    ns = {"source_lang": source, "target_lang": target,
          "translation_mapping": MAPPING, "sermon_context": sermon,
          "context": context}
    exec(block, ns)
    return ns["system"]


def cached_prefix(system):
    """캐시가 걸린 블록까지(그 블록 포함)를 돌려준다. 여기까지는 절대 변하면 안 된다."""
    if isinstance(system, str):
        return None                      # 원고가 없을 때는 캐시를 안 쓴다
    out = []
    for blk in system:
        out.append(blk.get("text", ""))
        if "cache_control" in blk:
            return "".join(out)
    return None


for path in TARGETS:
    print("\n" + "=" * 64)
    print(os.path.relpath(path, ROOT))
    print("=" * 64)
    src = io.open(path, encoding="utf-8").read()
    try:
        a = build_system(src, "첫 번째 문장입니다.")
        b = build_system(src, "전혀 다른 두 번째 문장입니다. 길이도 다릅니다.")
        c = build_system(src, "")
    except Exception as e:
        check("프롬프트 조립 부분을 실행할 수 있다", False, str(e)[:120])
        continue

    pa, pb, pc = cached_prefix(a), cached_prefix(b), cached_prefix(c)
    check("설교 원고가 있으면 캐시 블록을 쓴다", pa is not None)
    if pa is None:
        continue

    check("문맥이 달라져도 캐시 블록까지가 똑같다", pa == pb,
          "" if pa == pb else "앞부분이 %d자에서 갈라짐" % next(
              (i for i, (x, y) in enumerate(zip(pa, pb)) if x != y), min(len(pa), len(pb))))
    check("문맥이 아예 없을 때도 똑같다", pa == pc)
    check("설교 원고가 캐시 블록 안에 있다", SERMON[:40] in pa)

    # 바뀌는 내용(문맥)은 반드시 캐시 블록 '뒤'에 있어야 한다
    check("바뀌는 문맥이 캐시 블록 앞에 없다", "첫 번째 문장입니다." not in pa)
    tail_a = "".join(blk.get("text", "") for blk in a)[len(pa):]
    check("바뀌는 문맥이 캐시 블록 뒤에 있다", "첫 번째 문장입니다." in tail_a)

    # 캐시 블록은 충분히 커야 값어치가 있다 (Anthropic 최소 단위)
    check("캐시 블록이 충분히 크다", len(pa) > 2000, "%d자" % len(pa))

print("\n" + "=" * 64)
print("실패 %d건" % len(fails))
for f in fails:
    print("  -", f)
if fails:
    print("\n요금이 폭증하는 구조입니다. 변하는 내용(문맥·시각·ID)은")
    print("반드시 cache_control 블록 '뒤'에 두십시오.")
sys.exit(1 if fails else 0)
