# -*- coding: utf-8 -*-
"""배포용 암호 ZIP을 만든다 — **ZipCrypto** 방식.

왜 ZipCrypto인가: 윈도우 탐색기의 기본 압축 해제는 **AES를 못 풉니다.**
7-Zip 등으로 AES 암호 ZIP을 만들어 올리면 받는 분이 압축을 풀 때
`0x80004005` 오류를 봅니다. 실제로 겪은 문제입니다.

파이썬 `zipfile`은 암호 ZIP **읽기만** 되고 쓰기는 안 되므로
전통 PKWARE(ZipCrypto) 암호화를 직접 구현했습니다.

    python 개발도구/배포ZIP만들기.py <넣을파일> <만들ZIP> <비밀번호>

예:
    python 개발도구/배포ZIP만들기.py \\
        배포빌드/installer/LiveWord_Setup.exe \\
        배포빌드/installer/LiveWord_Setup_v1.2.zip  '비밀번호'

만든 뒤 스스로 열어서 내용이 원본과 같은지 확인합니다.
비밀번호는 릴리스 설명에 적지 말고 따로 전달하십시오.
"""
import os
import sys
import zlib
import struct
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

_CRC = None


def _crc_table():
    global _CRC
    if _CRC is None:
        t = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ (0xEDB88320 if c & 1 else 0)
            t.append(c)
        _CRC = t
    return _CRC


class _Keys:
    """PKWARE 전통 암호의 키 3개. 한 바이트씩 먹이며 갱신된다."""

    def __init__(self, password):
        self.k = [0x12345678, 0x23456789, 0x34567890]
        for b in password:
            self.update(b)

    def update(self, b):
        t = _crc_table()
        k = self.k
        k[0] = (k[0] >> 8) ^ t[(k[0] ^ b) & 0xFF]
        k[1] = (k[1] + (k[0] & 0xFF)) & 0xFFFFFFFF
        k[1] = (k[1] * 134775813 + 1) & 0xFFFFFFFF
        k[2] = (k[2] >> 8) ^ t[(k[2] ^ (k[1] >> 24)) & 0xFF]

    def stream_byte(self):
        t = (self.k[2] | 2) & 0xFFFF
        return ((t * (t ^ 1)) >> 8) & 0xFF

    def encrypt(self, data):
        out = bytearray(len(data))
        for i, p in enumerate(data):
            out[i] = p ^ self.stream_byte()
            self.update(p)
        return bytes(out)


def make(src, dst, password, arcname=None):
    raw = open(src, "rb").read()
    arcname = arcname or os.path.basename(src)
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = comp.compress(raw) + comp.flush()

    # 12바이트 암호 머리말. 마지막 바이트는 CRC의 최상위 바이트여야
    # 압축 푸는 쪽이 비밀번호가 맞는지 빨리 알 수 있다.
    header = bytearray(os.urandom(12))
    header[11] = (crc >> 24) & 0xFF
    keys = _Keys(password.encode("utf-8"))
    enc = keys.encrypt(bytes(header)) + keys.encrypt(body)

    name = arcname.encode("utf-8")
    flag = 0x0001 | 0x0800          # 0x0001 암호화 · 0x0800 파일명 UTF-8
    csize, usize = len(enc), len(raw)
    zip64 = csize >= 0xFFFFFFFF or usize >= 0xFFFFFFFF
    if zip64:
        raise RuntimeError("4GB 이상은 이 도구가 다루지 않습니다")

    with open(dst, "wb") as f:
        offset = f.tell()
        f.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flag, 8,
                            0, 0, crc, csize, usize, len(name), 0))
        f.write(name)
        f.write(enc)
        cd_start = f.tell()
        f.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flag, 8,
                            0, 0, crc, csize, usize, len(name), 0, 0, 0, 0, 0, offset))
        f.write(name)
        cd_size = f.tell() - cd_start
        f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1,
                            cd_size, cd_start, 0))
    return raw, usize, csize


def verify(dst, password, original):
    """만든 ZIP을 실제로 열어 원본과 같은지 확인한다."""
    with zipfile.ZipFile(dst) as z:
        info = z.infolist()[0]
        if not (info.flag_bits & 0x1):
            return False, "암호가 걸려 있지 않습니다"
        z.setpassword(password.encode("utf-8"))
        got = z.read(info.filename)
    if got != original:
        return False, "풀어낸 내용이 원본과 다릅니다"
    return True, "원본과 일치 (%d바이트)" % len(got)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    src, dst, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    raw, usize, csize = make(src, dst, pw)
    print("만듦: %s" % dst)
    print("  원본 %.1fMB → 압축 %.1fMB" % (usize / 1e6, csize / 1e6))
    ok, msg = verify(dst, pw, raw)
    print("  검증: %s — %s" % ("통과" if ok else "실패", msg))
    sys.exit(0 if ok else 1)
