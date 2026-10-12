# -*- coding: utf-8 -*-
"""Gemini_세션관찰.py가 남긴 jsonl에서 인식 결과를 모으고 낱말 횟수를 센다.

사용: python 개발도구/Gemini_세션관찰_분석.py <a.jsonl,b.jsonl,...> [낱말1,낱말2,...]
턴은 프로그램과 같은 규칙(누적 길이가 절반 아래로 줄면 새 턴)으로 나눈다.
"""
import sys, json, re
sys.stdout.reconfigure(encoding="utf-8")
def turns(path):
    """프로그램과 같은 규칙(누적 길이가 절반 아래로 줄면 새 턴)으로 턴별 마지막 interim을 모은다."""
    out, prev = [], ""
    for l in open(path, encoding="utf-8"):
        d = json.loads(l)
        if d["kind"] != "interim": continue
        t = d["text"]
        if prev and len(t) < len(prev) * 0.5:
            out.append(prev)
        prev = t
    if prev: out.append(prev)
    return out
if __name__ == "__main__":
    words = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    for p in sys.argv[1].split(","):
        txt = " ".join(turns(p))
        finals = " ".join(json.loads(l)["text"] for l in open(p, encoding="utf-8") if json.loads(l)["kind"] == "final")
        cnt = {w: (txt.count(w), finals.count(w)) for w in words}
        print(f"== {p}  interim {len(txt)}자 / final {len(finals)}자  {cnt}")
