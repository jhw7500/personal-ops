#!/usr/bin/env python3
"""ACPI BERT (Boot Error Record Table) 영역 디코더.

입력: /sys/firmware/acpi/tables/data/BERT 를 복사한 바이너리 파일.

주의: 커널(drivers/acpi/apei/bert.c)은 부팅 시 각 status block 의
block_status 를 0 으로 지운다. 따라서 block_status == 0 은 "레코드 없음"이
아니라 "이미 커널이 읽고 지운 상태"다. 이 디코더는 block_status 로 중단하지
않고 data_length 를 기준으로 항목을 순회한다.
"""

import struct
import sys

SEVERITY = {0: "Recoverable(복구가능)", 1: "Fatal(치명)",
            2: "Corrected(정정됨)", 3: "Informational(정보)"}

SECTION_TYPES = {
    "9876ccad-47b4-4bdb-b65e-16f193c4f3db": "Processor Generic (범용 프로세서)",
    "dc3ea0b0-a144-4797-b95b-53fa242b6e1d": "IA32/X64 Processor (CPU)",
    "a5bc1114-6f64-4ede-b863-3e83ed7c83b1": "Platform Memory (메모리/DIMM)",
    "61ec04fc-48e6-d813-25c9-8daa44750b12": "Platform Memory 2 (메모리/DIMM)",
    "d995e954-bbc1-430f-ad91-b44dcb3c6f35": "PCI Express (PCIe 장치)",
    "c5753963-3b84-4095-bf78-eddad3f9c9dd": "PCI/PCI-X Bus (버스)",
    "eb5e4685-ca66-4769-b6a2-26068b001326": "PCI Component (장치)",
    "81212a96-09ed-4996-9471-8d729c8e69ed": "Firmware Error Record (펌웨어)",
    "5b51fef7-c79d-4434-8f1b-aa62de3e2c64": "DMAr Generic",
    "71761d37-32b2-45cd-a7d0-b0fedd93e8cf": "Intel VT-d DMAr",
    "036f84e1-7f37-428c-a79e-575fdfaa84ec": "IOMMU DMAr",
}

IA_ERR_TYPES = {
    "a55701f5-e3ef-43de-ac72-249b573fad2c": "Cache Error (캐시)",
    "fc06b535-5e1f-4562-9f25-0a3b9adb63c3": "TLB Error (TLB)",
    "1cf3f8b3-c5b1-49a2-aa59-5eef92ffa63c": "Bus Error (버스)",
    "48ab7f57-dc34-4f6c-a7d3-b0b5b0a74314": "Micro-architectural Error (마이크로아키텍처)",
}

MEM_ERR_TYPE = {
    0: "Unknown", 1: "No error", 2: "Single-bit ECC", 3: "Multi-bit ECC",
    4: "Single-symbol ChipKill ECC", 5: "Multi-symbol ChipKill ECC",
    6: "Master abort", 7: "Target abort", 8: "Parity error",
    9: "Watchdog timeout", 10: "Invalid address", 11: "Mirror Broken",
    12: "Memory Sparing", 13: "Scrub corrected error",
    14: "Scrub uncorrected error", 15: "Physical Memory Map-out event",
}


def guid(b):
    d1, d2, d3 = struct.unpack_from("<IHH", b, 0)
    r = b[8:16]
    return "%08x-%04x-%04x-%02x%02x-%s" % (
        d1, d2, d3, r[0], r[1], "".join("%02x" % x for x in r[2:]))


def bcd(x):
    return (x >> 4) * 10 + (x & 0x0F)


def decode_timestamp(b):
    """CPER v3.0 timestamp: BCD. 반환: (문자열, precise 여부)"""
    if len(b) < 8 or not any(b):
        return None, None
    sec, minute, hour, flag, day, month, year, century = b[:8]
    try:
        s = "%02d%02d-%02d-%02d %02d:%02d:%02d" % (
            bcd(century), bcd(year), bcd(month), bcd(day),
            bcd(hour), bcd(minute), bcd(sec))
    except Exception:
        return None, None
    return s, bool(flag & 0x01)


def hexdump(b, limit=256, indent="      "):
    out = []
    data = b[:limit]
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexs = " ".join("%02x" % c for c in chunk)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append("%s%04x  %-47s  %s" % (indent, i, hexs, txt))
    if len(b) > limit:
        out.append("%s... (%d바이트 중 %d바이트만 표시)" % (indent, len(b), limit))
    return "\n".join(out)


def decode_memory(b):
    if len(b) < 80:
        return ["      (메모리 섹션이 80바이트 미만 — 부분 데이터)"]
    (valid, err_status, paddr, paddr_mask) = struct.unpack_from("<QQQQ", b, 0)
    (node, card, module, bank, device, row, col, bitpos) = \
        struct.unpack_from("<8H", b, 32)
    (req_id, resp_id, target_id) = struct.unpack_from("<QQQ", b, 48)
    err_type, extended = struct.unpack_from("<BB", b, 72)
    rank, card_handle, module_handle = struct.unpack_from("<3H", b, 74)

    def v(bit):
        return bool(valid & (1 << bit))

    lines = ["      validation_bits : 0x%016x" % valid,
             "      error_status    : 0x%016x" % err_status]
    if v(1):
        lines.append("      물리 주소       : 0x%016x" % paddr)
    if v(2):
        lines.append("      주소 마스크     : 0x%016x" % paddr_mask)
    loc = []
    for name, val, bit in (("node", node, 3), ("card", card, 4),
                           ("module", module, 5), ("bank", bank, 6),
                           ("device", device, 7), ("row", row, 8),
                           ("column", col, 9), ("bit", bitpos, 10)):
        if v(bit):
            loc.append("%s=%d" % (name, val))
    if loc:
        lines.append("      위치            : " + ", ".join(loc))
    if v(14):
        lines.append("      오류 유형       : %s (%d)" %
                     (MEM_ERR_TYPE.get(err_type, "?"), err_type))
    if v(15):
        lines.append("      rank            : %d" % rank)
    if v(16):
        lines.append("      card/module 핸들: %d / %d  (SMBIOS Type 17 핸들 → dmidecode 로 DIMM 슬롯 확인 가능)"
                     % (card_handle, module_handle))
    return lines


def decode_ia32(b):
    if len(b) < 64:
        return ["      (CPU 섹션이 64바이트 미만 — 부분 데이터)"]
    valid, lapic = struct.unpack_from("<QQ", b, 0)
    cpuid = b[16:64]
    lines = ["      validation_bits : 0x%016x" % valid]
    if valid & 0x1:
        lines.append("      Local APIC ID   : 0x%x (%d)" % (lapic, lapic))
    if valid & 0x2:
        eax, ebx, ecx, edx = struct.unpack_from("<4I", cpuid, 0)
        fam = (eax >> 8) & 0xF
        model = (eax >> 4) & 0xF
        step = eax & 0xF
        ext_model = (eax >> 16) & 0xF
        ext_fam = (eax >> 20) & 0xFF
        if fam == 0xF:
            fam += ext_fam
        if fam in (0x6, 0xF):
            model += ext_model << 4
        lines.append("      CPUID signature : 0x%08x  → family 0x%x, model 0x%x, stepping %d"
                     % (eax, fam, model, step))
    n_info = (valid >> 2) & 0x3F
    lines.append("      error info 개수 : %d" % n_info)
    off = 64
    for i in range(n_info):
        if off + 64 > len(b):
            lines.append("      [error info %d] 데이터 잘림" % i)
            break
        g = guid(b[off:off + 16])
        (ivalid, check_info, tgt, req, resp, ip) = \
            struct.unpack_from("<6Q", b, off + 16)
        lines.append("      [error info %d] %s" %
                     (i, IA_ERR_TYPES.get(g, "알 수 없는 유형 " + g)))
        lines.append("         validation  : 0x%016x" % ivalid)
        if ivalid & 0x1:
            lines.append("         check_info  : 0x%016x" % check_info)
            lines.extend("         " + s for s in decode_check_info(g, check_info))
        if ivalid & 0x2:
            lines.append("         target_id   : 0x%016x" % tgt)
        if ivalid & 0x4:
            lines.append("         requestor   : 0x%016x" % req)
        if ivalid & 0x8:
            lines.append("         responder   : 0x%016x" % resp)
        if ivalid & 0x10:
            lines.append("         IP          : 0x%016x" % ip)
        off += 64
    return lines


def decode_check_info(g, ci):
    """Cache/TLB/Bus check_info 비트 해석 (UEFI 스펙 N.2.4.2)."""
    out = []
    trans = {0: "Instruction", 1: "Data Access", 2: "Generic"}
    op = {0: "generic error", 1: "generic read", 2: "generic write",
          3: "data read", 4: "data write", 5: "instruction fetch",
          6: "prefetch", 7: "eviction", 8: "snoop"}
    if g in ("a55701f5-e3ef-43de-ac72-249b573fad2c",
             "fc06b535-5e1f-4562-9f25-0a3b9adb63c3",
             "1cf3f8b3-c5b1-49a2-aa59-5eef92ffa63c"):
        v = ci & 0xFFFF
        if v & 0x1:
            out.append("transaction type : %s" % trans.get((ci >> 16) & 0x3, "?"))
        if v & 0x2:
            out.append("operation        : %s" % op.get((ci >> 18) & 0xF, "?"))
        if v & 0x4:
            out.append("level            : %d" % ((ci >> 22) & 0x7))
        if v & 0x20:
            out.append("uncorrected      : %s" % bool((ci >> 26) & 1))
        if v & 0x40:
            out.append("precise IP       : %s" % bool((ci >> 27) & 1))
        if v & 0x80:
            out.append("restartable IP   : %s" % bool((ci >> 28) & 1))
        if v & 0x100:
            out.append("overflow         : %s" % bool((ci >> 29) & 1))
        if g == "1cf3f8b3-c5b1-49a2-aa59-5eef92ffa63c":
            if v & 0x200:
                out.append("participation    : %d" % ((ci >> 30) & 0x3))
            if v & 0x400:
                out.append("timed out        : %s" % bool((ci >> 32) & 1))
            if v & 0x800:
                out.append("address space    : %d" % ((ci >> 33) & 0x3))
    return out


def decode_pcie(b):
    if len(b) < 32:
        return ["      (PCIe 섹션이 32바이트 미만 — 부분 데이터)"]
    valid, port_type = struct.unpack_from("<QI", b, 0)
    lines = ["      validation_bits : 0x%016x" % valid,
             "      port type       : %d" % port_type]
    if len(b) >= 48 and (valid & 0x8):
        vid, did = struct.unpack_from("<HH", b, 32)
        cls = b[36:39]
        func, dev = b[39], b[40]
        seg = struct.unpack_from("<H", b, 41)[0]
        bus, sec_bus = b[43], b[44]
        lines.append("      장치            : %04x:%04x  (vendor:device)" % (vid, did))
        lines.append("      BDF             : %04x:%02x:%02x.%d" % (seg, bus, dev, func))
        lines.append("      class code      : %02x%02x%02x" % (cls[2], cls[1], cls[0]))
        lines.append("      → `lspci -nn -s %04x:%02x:%02x.%d` 로 대조" % (seg, bus, dev, func))
    return lines


def main():
    if len(sys.argv) < 2:
        print("usage: host-bert-decode.py <BERT 데이터 파일>", file=sys.stderr)
        return 2
    with open(sys.argv[1], "rb") as f:
        data = f.read()

    print("=" * 78)
    print("ACPI BERT 영역 디코드 — %d 바이트" % len(data))
    print("=" * 78)

    if len(data) < 20:
        print("영역이 20바이트 미만 — Generic Error Status Block 을 담을 수 없음.")
        print(hexdump(data, len(data)))
        return 1

    off = 0
    blk = 0
    while off + 20 <= len(data):
        (block_status, raw_off, raw_len, data_len, sev) = \
            struct.unpack_from("<5I", data, off)

        if block_status == 0 and data_len == 0:
            break

        blk += 1
        print()
        print("── Generic Error Status Block #%d  (영역 오프셋 0x%x)" % (blk, off))
        print("   block_status  : 0x%08x%s" % (
            block_status,
            "   ← 0 = 커널이 부팅 시 읽고 지운 상태 (레코드 없음이 아님)"
            if block_status == 0 else ""))
        if block_status:
            bits = []
            if block_status & 0x1:
                bits.append("uncorrectable 유효")
            if block_status & 0x2:
                bits.append("correctable 유효")
            if block_status & 0x4:
                bits.append("다중 uncorrectable")
            if block_status & 0x8:
                bits.append("다중 correctable")
            cnt = (block_status >> 4) & 0x3FF
            bits.append("entry count=%d" % cnt)
            print("                   %s" % ", ".join(bits))
        print("   전체 심각도   : %s (%d)" % (SEVERITY.get(sev, "?"), sev))
        print("   data_length   : %d" % data_len)
        if raw_len:
            print("   raw data      : offset %d, length %d" % (raw_off, raw_len))

        if data_len == 0 or off + 20 + data_len > len(data):
            print("   (data_length 가 0 이거나 영역을 벗어남 — 원시 덤프)")
            print(hexdump(data[off + 20:], 512))
            break

        # Generic Error Data Entry 순회
        eoff = off + 20
        end = off + 20 + data_len
        idx = 0
        while eoff + 64 <= end:
            idx += 1
            sect = guid(data[eoff:eoff + 16])
            esev, rev = struct.unpack_from("<IH", data, eoff + 16)
            vbits, flags = data[eoff + 22], data[eoff + 23]
            edlen = struct.unpack_from("<I", data, eoff + 24)[0]
            fru_id = data[eoff + 28:eoff + 44]
            fru_text = data[eoff + 44:eoff + 64].split(b"\x00")[0]

            hdr = 72 if rev >= 0x300 else 64
            ts_str = ts_precise = None
            if hdr == 72 and eoff + 72 <= end:
                ts_str, ts_precise = decode_timestamp(data[eoff + 64:eoff + 72])

            print()
            print("   ┌─ Error Data Entry #%d" % idx)
            print("   │  섹션 유형    : %s" % SECTION_TYPES.get(sect, "알 수 없음"))
            print("   │  섹션 GUID    : %s" % sect)
            print("   │  심각도       : %s (%d)" % (SEVERITY.get(esev, "?"), esev))
            print("   │  revision     : 0x%04x   validation: 0x%02x   flags: 0x%02x"
                  % (rev, vbits, flags))
            if flags:
                fl = []
                if flags & 0x01:
                    fl.append("primary(주 원인)")
                if flags & 0x02:
                    fl.append("containment warning")
                if flags & 0x04:
                    fl.append("reset(리셋 필요)")
                if flags & 0x08:
                    fl.append("threshold exceeded")
                if flags & 0x10:
                    fl.append("resource not accessible")
                if flags & 0x20:
                    fl.append("latent/consequential")
                print("   │                 %s" % ", ".join(fl))
            if ts_str:
                print("   │  ★ 기록 시각  : %s %s"
                      % (ts_str, "(정확)" if ts_precise else "(부정확할 수 있음)"))
            if any(fru_id):
                print("   │  FRU ID       : %s" % guid(fru_id))
            if fru_text:
                print("   │  FRU text     : %s"
                      % fru_text.decode("ascii", "replace"))
            print("   │  섹션 길이    : %d 바이트" % edlen)
            print("   └─")

            sdata = data[eoff + hdr:eoff + hdr + edlen]
            if sect == "a5bc1114-6f64-4ede-b863-3e83ed7c83b1" or \
               sect == "61ec04fc-48e6-d813-25c9-8daa44750b12":
                for ln in decode_memory(sdata):
                    print(ln)
            elif sect == "dc3ea0b0-a144-4797-b95b-53fa242b6e1d":
                for ln in decode_ia32(sdata):
                    print(ln)
            elif sect == "d995e954-bbc1-430f-ad91-b44dcb3c6f35":
                for ln in decode_pcie(sdata):
                    print(ln)
            else:
                print("      (전용 디코더 없음 — 원시 덤프)")
            print("      --- 섹션 원시 데이터 ---")
            print(hexdump(sdata, 256))

            eoff += hdr + edlen

        off = end

    if blk == 0:
        print()
        print("해석 가능한 status block 을 찾지 못했다. 원시 덤프:")
        print(hexdump(data, 512))

    print()
    print("=" * 78)
    print("해석 주의")
    print("  · block_status=0 은 커널이 이미 읽고 지운 흔적이다. 레코드 부재가 아니다.")
    print("  · ★ 기록 시각이 나오면 어느 사건의 기록인지 대조하라.")
    print("    (09-11 16:50 / 09-12 15:58 / 09-12 21:09 중 어느 것인지)")
    print("  · 시각이 세 사건 중 가장 이른 것이면, 같은 레코드가 계속 재보고되는 것이며")
    print("    사건마다 새 하드웨어 오류가 난 것은 아니다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
