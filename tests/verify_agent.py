#!/usr/bin/env python3
"""
Verification suite for ibkr-desktop-fix:
  1. Independent JVMS §4.10.1 ClassFile + StackMapTable structural verifier.
  2. Standalone MiniJVM bytecode interpreter executing WebChannelFixAgent.class against
     synthetic ClassFile v61.0 targets (and optionally against a local IBKR Desktop
     `ntws4launch-*.jar` via `--ibkr-jar`).
  3. Node.js V8 runtime verification of the injected WebEngine script (iframe propagation,
     CSS injection idempotency, 250 ms hidden-tab rAF throttling, and disjoint timer IDs).
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
import build_jar as bj


def parse_raw_classfile(data: bytes):
    assert len(data) >= 16
    magic, minor, major, cp_count = struct.unpack_from(">IHHH", data, 0)
    assert magic == 0xCAFEBABE, f"Bad magic 0x{magic:08x}"
    off = 10
    cp = [None] * cp_count
    i = 1
    while i < cp_count:
        tag = data[off]; off += 1
        if tag == 1:
            ln = struct.unpack_from(">H", data, off)[0]; off += 2
            cp[i] = (1, data[off:off + ln]); off += ln
        elif tag in (3, 4):
            cp[i] = (tag, struct.unpack_from(">i", data, off)[0]); off += 4
        elif tag in (5, 6):
            cp[i] = (tag, struct.unpack_from(">Q", data, off)[0]); off += 8; i += 1
        elif tag in (7, 8, 16, 19, 20):
            cp[i] = (tag, struct.unpack_from(">H", data, off)[0]); off += 2
        elif tag in (9, 10, 11, 12, 17, 18):
            cp[i] = (tag, struct.unpack_from(">HH", data, off)); off += 4
        elif tag == 15:
            cp[i] = (tag, struct.unpack_from(">BH", data, off)); off += 3
        else:
            raise AssertionError(f"Invalid CP tag {tag} at {i}")
        i += 1

    def u8(idx):
        assert 0 < idx < cp_count and cp[idx][0] == 1
        return cp[idx][1].decode("utf-8", "replace")

    acc, this_cls, super_cls, if_cnt = struct.unpack_from(">HHHH", data, off)
    off += 8
    ifaces = [u8(cp[struct.unpack_from(">H", data, off + 2 * k)[0]][1]) for k in range(if_cnt)]
    off += 2 * if_cnt

    f_cnt = struct.unpack_from(">H", data, off)[0]; off += 2
    for _ in range(f_cnt):
        _, fn, fd, fac = struct.unpack_from(">HHHH", data, off); off += 8
        u8(fn); u8(fd)
        for _ in range(fac):
            an, al = struct.unpack_from(">HI", data, off); off += 6 + al
            u8(an)

    m_cnt = struct.unpack_from(">H", data, off)[0]; off += 2
    methods = {}
    for _ in range(m_cnt):
        ma, mn, md, mac = struct.unpack_from(">HHHH", data, off); off += 8
        mname, mdesc = u8(mn), u8(md)
        attrs = {}
        for _ in range(mac):
            an, al = struct.unpack_from(">HI", data, off); off += 6
            attrs[u8(an)] = data[off:off + al]
            off += al
        methods[(mname, mdesc)] = (ma, attrs)

    ca_cnt = struct.unpack_from(">H", data, off)[0]; off += 2
    for _ in range(ca_cnt):
        an, al = struct.unpack_from(">HI", data, off); off += 6 + al
        u8(an)
    assert off == len(data), f"ClassFile size mismatch {off} != {len(data)}"
    return major, minor, cp, u8(cp[this_cls][1]), (u8(cp[super_cls][1]) if super_cls else None), ifaces, methods


OP_FIXED_LEN = {
    **{op: 1 for op in (
        0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,
        0x1a,0x1b,0x1c,0x1d,0x1e,0x1f,0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x29,
        0x2a,0x2b,0x2c,0x2d,0x2e,0x2f,0x30,0x31,0x32,0x33,0x34,0x35,
        0x3b,0x3c,0x3d,0x3e,0x3f,0x40,0x41,0x42,0x43,0x44,0x45,0x46,0x47,0x48,0x49,0x4a,
        0x4b,0x4c,0x4d,0x4e,0x4f,0x50,0x51,0x52,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,
        0x5b,0x5c,0x5d,0x5e,0x5f,
        *range(0x60, 0x84), *range(0x85, 0x99),
        0xac,0xad,0xae,0xaf,0xb0,0xb1,0xbe,0xbf,0xc2,0xc3
    )},
    **{op: 2 for op in (0x10,0x12,0x15,0x16,0x17,0x18,0x19,0x36,0x37,0x38,0x39,0x3a,0xbc)},
    **{op: 3 for op in (
        0x11,0x13,0x14,0x84,*range(0x99, 0xa8),0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xbb,0xbd,0xc0,0xc1,0xc6,0xc7
    )},
    0xc5: 4,
    0xb9: 5, 0xba: 5, 0xc8: 5,
}


def decode_smt_vtype(buf: bytes, pos: int, cp):
    tag = buf[pos]; pos += 1
    if tag <= 6:
        names = {0: ("TOP",), 1: ("I",), 2: ("F",), 3: ("D",), 4: ("J",), 5: ("NULL",), 6: ("UNINIT_THIS",)}
        return names[tag], pos
    if tag == 7:
        cidx = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        assert cp[cidx][0] == 7
        return ("O", cp[cp[cidx][1]][1].decode("utf-8", "replace")), pos
    if tag == 8:
        off = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        return ("UNINIT", off), pos
    raise AssertionError(f"Bad StackMap vtype tag {tag}")


def verify_classfile(data: bytes, label: str):
    major, minor, cp, this_cls, _, _, methods = parse_raw_classfile(data)
    cp_count = len(cp)
    for idx, item in enumerate(cp):
        if item is None:
            continue
        t, val = item
        if t in (7, 8, 16, 19, 20):
            assert 0 < val < cp_count and cp[val][0] == 1
        elif t == 12:
            assert cp[val[0]][0] == 1 and cp[val[1]][0] == 1
        elif t in (9, 10, 11):
            assert cp[val[0]][0] == 7 and cp[val[1]][0] == 12

    verified_methods = 0
    for (mname, mdesc), (macc, attrs) in methods.items():
        if "Code" not in attrs:
            continue
        c = attrs["Code"]
        ms, ml, clen = struct.unpack_from(">HHI", c, 0)
        assert clen > 0
        bc = c[8:8 + clen]
        valid_pcs = set()
        branch_targets = set()
        pc = 0
        while pc < clen:
            valid_pcs.add(pc)
            op = bc[pc]
            if op in OP_FIXED_LEN:
                sz = OP_FIXED_LEN[op]
                if 0x99 <= op <= 0xA7 or op in (0xC6, 0xC7):
                    branch_targets.add(pc + struct.unpack_from(">h", bc, pc + 1)[0])
                elif op == 0xC8:
                    branch_targets.add(pc + struct.unpack_from(">i", bc, pc + 1)[0])
                elif op == 0x12:
                    cidx = bc[pc + 1]
                    assert 0 < cidx < cp_count and cp[cidx] is not None
                elif op in (0x13, 0x14, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBB, 0xBD, 0xC0, 0xC1):
                    cidx = struct.unpack_from(">H", bc, pc + 1)[0]
                    assert 0 < cidx < cp_count and cp[cidx] is not None
                pc += sz
            elif op == 0xAA:
                pad = (4 - ((pc + 1) % 4)) % 4
                base = pc + 1 + pad
                dflt, lo, hi = struct.unpack_from(">iii", bc, base)
                branch_targets.add(pc + dflt)
                cnt = hi - lo + 1
                for k in range(cnt):
                    branch_targets.add(pc + struct.unpack_from(">i", bc, base + 12 + 4 * k)[0])
                pc = base + 12 + 4 * cnt
            elif op == 0xAB:
                pad = (4 - ((pc + 1) % 4)) % 4
                base = pc + 1 + pad
                dflt, npairs = struct.unpack_from(">ii", bc, base)
                branch_targets.add(pc + dflt)
                for k in range(npairs):
                    branch_targets.add(pc + struct.unpack_from(">i", bc, base + 8 + 8 * k + 4)[0])
                pc = base + 8 + 8 * npairs
            elif op == 0xC4:
                pc += 6 if bc[pc + 1] == 0x84 else 4
            else:
                raise AssertionError(f"{label}.{mname}@{pc}: illegal opcode 0x{op:02x}")
        assert pc == clen
        for tpc in branch_targets:
            assert tpc in valid_pcs

        p = 8 + clen
        ex_cnt = struct.unpack_from(">H", c, p)[0]; p += 2
        ex_handlers = set()
        for _ in range(ex_cnt):
            sp, ep, hp, ct = struct.unpack_from(">HHHH", c, p); p += 8
            assert sp in valid_pcs and (ep in valid_pcs or ep == clen) and hp in valid_pcs and sp < ep
            if ct: assert cp[ct][0] == 7
            ex_handlers.add(hp)
        ca_cnt = struct.unpack_from(">H", c, p)[0]; p += 2
        sub_attrs = {}
        for _ in range(ca_cnt):
            can, cal = struct.unpack_from(">HI", c, p); p += 6
            sub_attrs[cp[can][1].decode("utf-8", "replace")] = c[p:p + cal]
            p += cal
        assert p == len(c)

        smt_pcs = set()
        args_d, _ = bj.parse_method_desc(mdesc)
        cur_locals = []
        if not (macc & 0x0008):
            cur_locals.append(("UNINIT_THIS",) if mname == "<init>" else ("O", this_cls))
        for ad in args_d:
            cur_locals.append(bj.desc_to_vtype(ad))
        assert sum(2 if v[0] in ("J", "D") else 1 for v in cur_locals) <= ml

        if "StackMapTable" in sub_attrs:
            sb = sub_attrs["StackMapTable"]
            n_entries = struct.unpack_from(">H", sb, 0)[0]
            sp_pos = 2
            prev_offset = -1
            for _ in range(n_entries):
                ft = sb[sp_pos]; sp_pos += 1
                if ft <= 63:
                    delta = ft; stk = []
                elif ft <= 127:
                    delta = ft - 64; vt, sp_pos = decode_smt_vtype(sb, sp_pos, cp); stk = [vt]
                elif ft == 247:
                    delta = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2
                    vt, sp_pos = decode_smt_vtype(sb, sp_pos, cp); stk = [vt]
                elif 248 <= ft <= 250:
                    delta = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2
                    chop = 251 - ft
                    cur_locals = cur_locals[:-chop] if chop <= len(cur_locals) else []
                    stk = []
                elif ft == 251:
                    delta = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2; stk = []
                elif 252 <= ft <= 254:
                    delta = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2
                    for _ in range(ft - 251):
                        vt, sp_pos = decode_smt_vtype(sb, sp_pos, cp)
                        cur_locals.append(vt)
                    stk = []
                elif ft == 255:
                    delta = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2
                    nl = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2
                    cur_locals = []
                    for _ in range(nl):
                        vt, sp_pos = decode_smt_vtype(sb, sp_pos, cp)
                        cur_locals.append(vt)
                    ns = struct.unpack_from(">H", sb, sp_pos)[0]; sp_pos += 2
                    stk = []
                    for _ in range(ns):
                        vt, sp_pos = decode_smt_vtype(sb, sp_pos, cp)
                        stk.append(vt)
                else:
                    raise AssertionError(f"Reserved frame_type {ft}")
                f_pc = delta if prev_offset == -1 else (prev_offset + delta + 1)
                prev_offset = f_pc
                assert f_pc in valid_pcs
                assert sum(2 if v[0] in ("J", "D") else 1 for v in cur_locals) <= ml
                assert sum(2 if v[0] in ("J", "D") else 1 for v in stk) <= ms
                smt_pcs.add(f_pc)
            assert sp_pos == len(sb)

        if major >= 51:
            for req_pc in (branch_targets | ex_handlers):
                if req_pc != 0:
                    assert req_pc in smt_pcs, f"{label}.{mname}: PC {req_pc} missing from StackMapTable"
        verified_methods += 1

    print(f"  [VERIFIED] {label}: ClassFile v{major}.{minor}, {len(data)} bytes, CP={cp_count}, methods={verified_methods}")
    return cp, methods


def build_synthetic_fixtures():
    """Creates standalone ClassFile v61.0 mocks matching IBKR Desktop's exact method signatures."""
    # 1. Mock QWebAppEngineView
    cp = bj.ConstantPool()
    THIS = "ibkr/webview/QWebAppEngineView"
    SUPER = "java/lang/Object"
    t_idx, s_idx = cp.cls(THIS), cp.cls(SUPER)
    obj_init = cp.methodref(SUPER, "<init>", "()V")
    f_vis = cp.fieldref(THIS, "visible", "Z")
    f_last = cp.fieldref(THIS, "lastScript", "Ljava/lang/String;")
    str_cls = cp.cls("java/lang/String")
    s_wc = cp.string("prefix\n" + bj.BAD_OPT_JS + "\n\x01;\nsuffix")
    s_wh = cp.string(bj.WHEEL_PREFIX + ", e=>{ if( e.ctrlKey ) e.preventDefault(); }, { capture: true, passive: false } );\n")
    mref_vis = cp.methodref(THIS, "isVisible", "()Z")
    mref_exec = cp.methodref(THIS, "executeScript", "(Ljava/lang/String;)V")
    smt_u8 = cp.utf8("StackMapTable")
    code_u8 = cp.utf8("Code")

    f_list = [
        struct.pack(">HHHH", 0x0001, cp.utf8("visible"), cp.utf8("Z"), 0),
        struct.pack(">HHHH", 0x0001, cp.utf8("lastScript"), cp.utf8("Ljava/lang/String;"), 0),
    ]

    def raw_method(acc, name, desc, ms, ml, bc, smt=None):
        ca = bytearray(struct.pack(">HHI", ms, ml, len(bc)) + bc + struct.pack(">H", 0))
        if smt is not None:
            ca.extend(struct.pack(">HHI", 1, smt_u8, len(smt)) + smt)
        else:
            ca.extend(struct.pack(">H", 0))
        return struct.pack(">HHHHHI", acc, cp.utf8(name), cp.utf8(desc), 1, code_u8, len(ca)) + bytes(ca)

    m_list = [
        raw_method(0x0001, "<init>", "()V", 1, 1, b"\x2a" + struct.pack(">BH", 0xB7, obj_init) + b"\xb1"),
        raw_method(0x0001, "isVisible", "()Z", 1, 1, b"\x2a" + struct.pack(">BH", 0xB4, f_vis) + b"\xac"),
        raw_method(0x0002, "executeScript", "(Ljava/lang/String;)V", 2, 2,
                   b"\x2a\x2b" + struct.pack(">BH", 0xB5, f_last) + b"\xb1"),
        raw_method(0x0001, "scriptsRef", "()[Ljava/lang/String;", 4, 1,
                   b"\x05" + struct.pack(">BH", 0xBD, str_cls)
                   + b"\x59\x03" + struct.pack(">BH", 0x13, s_wc) + b"\x53"
                   + b"\x59\x04" + struct.pack(">BH", 0x13, s_wh) + b"\x53\xb0"),
        # 35-byte sendSuspendResume matching IBKR's exact StackMapTable (00 02 10 0b)
        raw_method(
            0x0001, "sendSuspendResume", "(ZLjava/util/Map;)V", 3, 3,
            b"\x1b\x99\x00\x0f\x2a" + struct.pack(">BH", 0xB6, mref_vis)
            + b"\x00\x00\x00\x00\x9a\x00\x04\xb1"
            + b"\x1b\x9a\x00\x0b\x2a" + struct.pack(">BH", 0xB6, mref_vis)
            + b"\x9a\x00\x04\xb1"
            + b"\x00\x00\x00\x00\x00\x00\xb1",
            smt=bytes.fromhex("0002100b")
        ),
    ]
    w_cls = (
        struct.pack(">IHH", 0xCAFEBABE, 0, 61)
        + cp.serialize()
        + struct.pack(">HHHHH", 0x0021, t_idx, s_idx, 0, len(f_list))
        + b"".join(f_list)
        + struct.pack(">H", len(m_list))
        + b"".join(m_list)
        + struct.pack(">H", 0)
    )

    # 2. Mock LocaleResourcesMgr (unpatched methods throw AssertionError so JvmSmokeTest proves suppression)
    cp2 = bj.ConstantPool()
    t2, s2 = cp2.cls(bj.CLS_LOCALE), cp2.cls("java/lang/Object")
    obj_init2 = cp2.methodref("java/lang/Object", "<init>", "()V")
    ae_cls = cp2.cls("java/lang/AssertionError")
    ae_init = cp2.methodref("java/lang/AssertionError", "<init>", "()V")
    c2_u8 = cp2.utf8("Code")

    def rm2(acc, n, d, ms, ml, bc):
        ca = struct.pack(">HHI", ms, ml, len(bc)) + bc + struct.pack(">HH", 0, 0)
        return struct.pack(">HHHHHI", acc, cp2.utf8(n), cp2.utf8(d), 1, c2_u8, len(ca)) + ca

    throw_bc = struct.pack(">BH", 0xBB, ae_cls) + b"\x59" + struct.pack(">BH", 0xB7, ae_init) + b"\xbf"
    m2 = [
        rm2(0x0001, "<init>", "()V", 1, 1, b"\x2a" + struct.pack(">BH", 0xB7, obj_init2) + b"\xb1"),
        rm2(0x0009, "confirmTranslationLater", "()V", 2, 0, throw_bc),
        rm2(0x000A, "confirmTranslationIfNecessary", "(Ljava/util/Locale;Ljava/lang/String;Ljava/lang/String;Z)V", 2, 4, throw_bc),
        rm2(0x000A, "confirmTranslation", "(Ljava/util/Locale;Ljava/lang/String;Z)V", 2, 3, throw_bc),
    ]
    loc_cls = (
        struct.pack(">IHH", 0xCAFEBABE, 0, 61) + cp2.serialize()
        + struct.pack(">HHHHHH", 0x0021, t2, s2, 0, 0, len(m2)) + b"".join(m2) + struct.pack(">H", 0)
    )

    # 3. Mock LanguageSettings
    cp3 = bj.ConstantPool()
    t3, s3 = cp3.cls(bj.CLS_LANG), cp3.cls("java/lang/Object")
    obj_init3 = cp3.methodref("java/lang/Object", "<init>", "()V")
    c3_u8 = cp3.utf8("Code")

    def rm3(acc, n, d, ms, ml, bc):
        ca = struct.pack(">HHI", ms, ml, len(bc)) + bc + struct.pack(">HH", 0, 0)
        return struct.pack(">HHHHHI", acc, cp3.utf8(n), cp3.utf8(d), 1, c3_u8, len(ca)) + ca

    m3 = [
        rm3(0x0001, "<init>", "()V", 1, 1, b"\x2a" + struct.pack(">BH", 0xB7, obj_init3) + b"\xb1"),
        rm3(0x0001, "isTranslationConfirmed", "(Ljava/lang/String;)Z", 1, 2, b"\x03\xac"),
    ]
    lng_cls = (
        struct.pack(">IHH", 0xCAFEBABE, 0, 61) + cp3.serialize()
        + struct.pack(">HHHHHH", 0x0021, t3, s3, 0, 0, len(m3)) + b"".join(m3) + struct.pack(">H", 0)
    )

    # 4. Variant QWebAppEngineView with 41-byte sendSuspendResume and full_frame (ft=255, d0=0) at PC=0
    cp4 = bj.ConstantPool()
    t4, s4 = cp4.cls(THIS), cp4.cls(SUPER)
    map_cls4 = cp4.cls("java/util/Map")
    obj_init4 = cp4.methodref(SUPER, "<init>", "()V")
    f_vis4 = cp4.fieldref(THIS, "visible", "Z")
    f_last4 = cp4.fieldref(THIS, "lastScript", "Ljava/lang/String;")
    str_cls4 = cp4.cls("java/lang/String")
    s_wc4 = cp4.string("prefix\n" + bj.BAD_OPT_JS + "\n\x01;\nsuffix")
    s_wh4 = cp4.string(bj.WHEEL_PREFIX + ", e=>{ if( e.ctrlKey ) e.preventDefault(); }, { capture: true, passive: false } );\n")
    mref_vis4 = cp4.methodref(THIS, "isVisible", "()Z")
    cp4.methodref(THIS, "executeScript", "(Ljava/lang/String;)V")
    smt_u8_4 = cp4.utf8("StackMapTable")
    code_u8_4 = cp4.utf8("Code")

    f_list4 = [
        struct.pack(">HHHH", 0x0001, cp4.utf8("visible"), cp4.utf8("Z"), 0),
        struct.pack(">HHHH", 0x0001, cp4.utf8("lastScript"), cp4.utf8("Ljava/lang/String;"), 0),
    ]

    def raw_m4(acc, name, desc, ms, ml, bc, smt=None):
        ca = bytearray(struct.pack(">HHI", ms, ml, len(bc)) + bc + struct.pack(">H", 0))
        if smt is not None:
            ca.extend(struct.pack(">HHI", 1, smt_u8_4, len(smt)) + smt)
        else:
            ca.extend(struct.pack(">H", 0))
        return struct.pack(">HHHHHI", acc, cp4.utf8(name), cp4.utf8(desc), 1, code_u8_4, len(ca)) + bytes(ca)

    # 41-byte body: backward loop edge goto -8 -> PC=0 (full_frame ft=255, d0=0), plus frames at PC=11 and PC=19
    sr_full_bc = (
        b"\x1b\x99\x00\x0a\x03\x3c\x00\x00\xa7\xff\xf8"
        + b"\x2a" + struct.pack(">BH", 0xB6, mref_vis4)
        + b"\x99\x00\x04\xb1"
        + (b"\x00" * 21) + b"\xb1"
    )
    assert len(sr_full_bc) == 41
    sr_full_smt = (
        struct.pack(">HBHH", 3, 255, 0, 3)
        + struct.pack(">BH", 7, t4)
        + b"\x01"
        + struct.pack(">BH", 7, map_cls4)
        + struct.pack(">HBB", 0, 10, 7)
    )
    m4 = [
        raw_m4(0x0001, "<init>", "()V", 1, 1, b"\x2a" + struct.pack(">BH", 0xB7, obj_init4) + b"\xb1"),
        raw_m4(0x0001, "isVisible", "()Z", 1, 1, b"\x2a" + struct.pack(">BH", 0xB4, f_vis4) + b"\xac"),
        raw_m4(0x0002, "executeScript", "(Ljava/lang/String;)V", 2, 2,
               b"\x2a\x2b" + struct.pack(">BH", 0xB5, f_last4) + b"\xb1"),
        raw_m4(0x0001, "scriptsRef", "()[Ljava/lang/String;", 4, 1,
               b"\x05" + struct.pack(">BH", 0xBD, str_cls4)
               + b"\x59\x03" + struct.pack(">BH", 0x13, s_wc4) + b"\x53"
               + b"\x59\x04" + struct.pack(">BH", 0x13, s_wh4) + b"\x53\xb0"),
        raw_m4(0x0001, "sendSuspendResume", "(ZLjava/util/Map;)V", 3, 3, sr_full_bc, smt=sr_full_smt),
    ]
    w_full_cls = (
        struct.pack(">IHH", 0xCAFEBABE, 0, 61)
        + cp4.serialize()
        + struct.pack(">HHHHH", 0x0021, t4, s4, 0, len(f_list4))
        + b"".join(f_list4)
        + struct.pack(">H", len(m4))
        + b"".join(m4)
        + struct.pack(">H", 0)
    )
    return w_cls, loc_cls, lng_cls, w_full_cls


class MiniJVM:
    """Executes WebChannelFixAgent.class bytecode directly in Python (handles both javac & Python-assembled bytecodes)."""
    def __init__(self, class_bytes: bytes):
        self.major, self.minor, self.cp, self.this_cls, _, _, self.methods = parse_raw_classfile(class_bytes)
        self.logs = []

    def u8(self, idx: int) -> str:
        return self.cp[idx][1].decode("utf-8")

    def resolve_member(self, idx: int):
        _, (cidx, ntidx) = self.cp[idx]
        cname = self.u8(self.cp[cidx][1])
        nidx, didx = self.cp[ntidx][1]
        return cname, self.u8(nidx), self.u8(didx)

    def invoke(self, mname: str, mdesc: str, args: list):
        _, attrs = self.methods[(mname, mdesc)]
        c = attrs["Code"]
        _, ml, clen = struct.unpack_from(">HHI", c, 0)
        bc = c[8:8 + clen]
        locals_arr = [None] * ml
        for idx, val in enumerate(args):
            locals_arr[idx] = val
        stack = []
        pc = 0

        def i32(x):
            x &= 0xFFFFFFFF
            return x - 0x100000000 if x & 0x80000000 else x

        def i8(x):
            x &= 0xFF
            return x - 0x100 if x & 0x80 else x

        while True:
            op = bc[pc]
            if op == 0x00: pc += 1
            elif op == 0x01: stack.append(None); pc += 1
            elif 0x02 <= op <= 0x08: stack.append(op - 3); pc += 1
            elif op == 0x10: stack.append(struct.unpack_from(">b", bc, pc + 1)[0]); pc += 2
            elif op == 0x11: stack.append(struct.unpack_from(">h", bc, pc + 1)[0]); pc += 3
            elif op in (0x12, 0x13):
                cidx = bc[pc + 1] if op == 0x12 else struct.unpack_from(">H", bc, pc + 1)[0]
                ctag, cval = self.cp[cidx]
                stack.append(self.u8(cval) if ctag == 8 else cval)
                pc += 2 if op == 0x12 else 3
            elif op == 0x15: stack.append(locals_arr[bc[pc + 1]]); pc += 2
            elif 0x1A <= op <= 0x1D: stack.append(locals_arr[op - 0x1A]); pc += 1
            elif op == 0x19: stack.append(locals_arr[bc[pc + 1]]); pc += 2
            elif 0x2A <= op <= 0x2D: stack.append(locals_arr[op - 0x2A]); pc += 1
            elif op in (0x2E, 0x32):
                idx = stack.pop(); arr = stack.pop(); stack.append(arr[idx]); pc += 1
            elif op == 0x33:
                idx = stack.pop(); arr = stack.pop(); stack.append(i8(arr[idx])); pc += 1
            elif op == 0x36: locals_arr[bc[pc + 1]] = stack.pop(); pc += 2
            elif 0x3B <= op <= 0x3E: locals_arr[op - 0x3B] = stack.pop(); pc += 1
            elif op == 0x3A: locals_arr[bc[pc + 1]] = stack.pop(); pc += 2
            elif 0x4B <= op <= 0x4E: locals_arr[op - 0x4B] = stack.pop(); pc += 1
            elif op in (0x4F, 0x53):
                val = stack.pop(); idx = stack.pop(); arr = stack.pop(); arr[idx] = val; pc += 1
            elif op == 0x54:
                val = stack.pop(); idx = stack.pop(); arr = stack.pop(); arr[idx] = val & 0xFF; pc += 1
            elif op == 0x57: stack.pop(); pc += 1
            elif op == 0x59: stack.append(stack[-1]); pc += 1
            elif op == 0x60: b = stack.pop(); a = stack.pop(); stack.append(i32(a + b)); pc += 1
            elif op == 0x64: b = stack.pop(); a = stack.pop(); stack.append(i32(a - b)); pc += 1
            elif op == 0x68: b = stack.pop(); a = stack.pop(); stack.append(i32(a * b)); pc += 1
            elif op == 0x78: b = stack.pop(); a = stack.pop(); stack.append(i32(a << (b & 0x1F))); pc += 1
            elif op == 0x7A: b = stack.pop(); a = stack.pop(); stack.append(i32(a >> (b & 0x1F))); pc += 1
            elif op == 0x7C: b = stack.pop(); a = stack.pop(); stack.append(i32((a & 0xFFFFFFFF) >> (b & 0x1F))); pc += 1
            elif op == 0x7E: b = stack.pop(); a = stack.pop(); stack.append(i32(a & b)); pc += 1
            elif op == 0x80: b = stack.pop(); a = stack.pop(); stack.append(i32(a | b)); pc += 1
            elif op == 0x82: b = stack.pop(); a = stack.pop(); stack.append(i32(a ^ b)); pc += 1
            elif op == 0x84:
                slot = bc[pc + 1]; inc = struct.unpack_from(">b", bc, pc + 2)[0]
                locals_arr[slot] = i32(locals_arr[slot] + inc); pc += 3
            elif op == 0x91: stack.append(i8(stack.pop())); pc += 1
            elif 0x99 <= op <= 0x9E:
                rel = struct.unpack_from(">h", bc, pc + 1)[0]; v = stack.pop()
                cond = {0x99: v == 0, 0x9A: v != 0, 0x9B: v < 0, 0x9C: v >= 0, 0x9D: v > 0, 0x9E: v <= 0}[op]
                pc = (pc + rel) if cond else (pc + 3)
            elif 0x9F <= op <= 0xA4:
                rel = struct.unpack_from(">h", bc, pc + 1)[0]; b = stack.pop(); a = stack.pop()
                cond = {0x9F: a == b, 0xA0: a != b, 0xA1: a < b, 0xA2: a >= b, 0xA3: a > b, 0xA4: a <= b}[op]
                pc = (pc + rel) if cond else (pc + 3)
            elif op == 0xA7:
                pc += struct.unpack_from(">h", bc, pc + 1)[0]
            elif op == 0xAA:
                pad = (4 - ((pc + 1) % 4)) % 4
                base = pc + 1 + pad
                dflt, lo, hi = struct.unpack_from(">iii", bc, base)
                key = stack.pop()
                if lo <= key <= hi:
                    pc += struct.unpack_from(">i", bc, base + 12 + 4 * (key - lo))[0]
                else:
                    pc += dflt
            elif op == 0xAB:
                pad = (4 - ((pc + 1) % 4)) % 4
                base = pc + 1 + pad
                dflt, npairs = struct.unpack_from(">ii", bc, base)
                key = stack.pop()
                jmp = dflt
                for k in range(npairs):
                    mk, mo = struct.unpack_from(">ii", bc, base + 8 + 8 * k)
                    if mk == key:
                        jmp = mo; break
                pc += jmp
            elif op in (0xC6, 0xC7):
                rel = struct.unpack_from(">h", bc, pc + 1)[0]; v = stack.pop()
                pc = (pc + rel) if ((v is None) if op == 0xC6 else (v is not None)) else (pc + 3)
            elif op in (0xAC, 0xB0):
                return stack.pop()
            elif op == 0xB1:
                return None
            elif op == 0xBE:
                stack.append(len(stack.pop())); pc += 1
            elif op == 0xB2:
                cname, fn, _ = self.resolve_member(struct.unpack_from(">H", bc, pc + 1)[0])
                stack.append("utf-8" if fn == "UTF_8" else ("STDERR",))
                pc += 3
            elif op == 0xBB:
                cname = self.u8(self.cp[struct.unpack_from(">H", bc, pc + 1)[0]][1])
                stack.append({"__cls__": cname}); pc += 3
            elif op == 0xBC:
                ln = stack.pop(); stack.append(bytearray(ln) if bc[pc + 1] == 8 else [0] * ln); pc += 2
            elif op == 0xBD:
                stack.append([None] * stack.pop()); pc += 3
            elif op == 0xBA:
                raise RuntimeError("Unexpected invokedynamic in happy path")
            elif op in (0xB6, 0xB7, 0xB8, 0xB9):
                cidx = struct.unpack_from(">H", bc, pc + 1)[0]
                cname, mn, md = self.resolve_member(cidx)
                args_d, ret_d = bj.parse_method_desc(md)
                call_args = [stack.pop() for _ in range(len(args_d))][::-1]
                recv = None if op == 0xB8 else stack.pop()
                if cname == self.this_cls:
                    if mn != "<init>":
                        res = self.invoke(mn, md, call_args if op == 0xB8 else ([recv] + call_args))
                        if ret_d != "V":
                            stack.append(res)
                else:
                    res = self._host_call(cname, mn, md, recv, call_args)
                    if ret_d != "V":
                        stack.append(res)
                pc += 5 if op == 0xB9 else 3
            else:
                raise RuntimeError(f"Unhandled opcode 0x{op:02x} at PC {pc} in {mname}")

    def _host_call(self, cname, mn, md, recv, args):
        if cname == "java/lang/Object" and mn == "<init>": return None
        if cname == "java/lang/instrument/Instrumentation" and mn == "addTransformer":
            recv["transformer"] = args[0]; return None
        if cname == "java/lang/String":
            if mn == "<init>" and md == "([BIILjava/nio/charset/Charset;)V":
                b, off, ln, cs = args
                recv["val"] = bytes(b[off:off + ln]).decode(cs, "replace"); return None
            s = recv["val"] if isinstance(recv, dict) else recv
            if mn == "equals":
                other = args[0]
                return 0 if other is None else (1 if s == (other["val"] if isinstance(other, dict) else other) else 0)
            if mn == "getBytes":
                return bytearray(s.encode(args[0]))
        if cname == "java/io/ByteArrayOutputStream":
            if mn == "<init>": recv["buf"] = bytearray(); return None
            if mn == "toByteArray": return bytearray(recv["buf"])
        if cname == "java/io/DataOutputStream":
            if mn == "<init>": recv["out"] = args[0]["buf"]; return None
            out = recv["out"]
            if mn == "writeByte": out.append(args[0] & 0xFF); return None
            if mn == "writeShort": out.extend(struct.pack(">H", args[0] & 0xFFFF)); return None
            if mn == "writeInt": out.extend(struct.pack(">I", args[0] & 0xFFFFFFFF)); return None
            if mn == "write" and md == "([B)V": out.extend(args[0]); return None
            if mn == "write" and md == "([BII)V":
                b, o, l = args; out.extend(b[o:o + l]); return None
        if cname == "java/io/PrintStream" and mn == "println":
            msg = args[0]["val"] if isinstance(args[0], dict) else args[0]
            self.logs.append(msg); return None
        raise RuntimeError(f"Unhandled host call {cname}.{mn}{md}")


def run_node_js_test():
    node = shutil.which("node")
    if not node:
        print("  [SKIP] node not found on PATH; skipping JS runtime test")
        return
    js_test = f"""
const styles = [];
function makeDoc(name) {{
  const map = {{}}, attrs = {{}}, listeners = {{}}, iframes = [];
  return {{
    name, hidden: false,
    head: {{ appendChild(el) {{ map[el.id] = el; styles.push(name + ':' + el.id); }} }},
    documentElement: {{
      setAttribute(k, v) {{ attrs[k] = v; }},
      removeAttribute(k) {{ delete attrs[k]; }},
      appendChild(el) {{ map[el.id] = el; }}
    }},
    body: {{ addEventListener(ev, fn, opts) {{ listeners[ev] = {{fn, opts}}; }} }},
    createElement(tag) {{ return {{ tag }}; }},
    getElementById(id) {{ return map[id] || null; }},
    getElementsByTagName(tag) {{ return tag === 'iframe' ? iframes : []; }},
    addEventListener(ev, fn) {{ listeners[ev] = fn; }},
    _attrs: attrs, _listeners: listeners, _iframes: iframes,
  }};
}}
let nativeRaf = 0, nativeCaf = 0, clearCalls = 0;
globalThis.performance = {{ now: () => 100 }};
globalThis.document = makeDoc('top');
globalThis.window = globalThis;
window.parent = window;
window.requestAnimationFrame = () => (++nativeRaf, 7);
window.cancelAnimationFrame = (id) => {{ nativeCaf++; if (id !== 7) throw new Error('Bad CAF ' + id); }};
window.setTimeout = (fn, ms) => {{ if (ms !== 250) throw new Error('Bad ms'); return 42; }};
window.clearTimeout = (id) => {{ clearCalls++; if (id !== 42) throw new Error('Bad clear ' + id); }};
window.addEventListener = (ev, fn) => {{ document._listeners['win_' + ev] = fn; }};
const childDoc = makeDoc('tv_iframe');
document._iframes.push({{ tagName: 'IFRAME', contentWindow: {{ document: childDoc }}, contentDocument: childDoc }});
{bj.USER_SCRIPT_JS}
{bj.USER_SCRIPT_JS}
if (styles.length !== 2) throw new Error('Idempotency failed: ' + styles);
{bj.JS_HIDE}
if (document._attrs['data-ibkr-hidden'] !== '1' || childDoc._attrs['data-ibkr-hidden'] !== '1') throw new Error('Hide failed');
const hidId = window.requestAnimationFrame(() => {{}});
{bj.JS_SHOW}
window.cancelAnimationFrame(hidId);
if (clearCalls !== 1 || nativeCaf !== 0) throw new Error('Disjoint handle isolation failed');
"""
    res = subprocess.run([node, "-e", js_test], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"Node JS test failed:\n{res.stderr}")
    print("  [VERIFIED] Node.js V8 injected script DOM/iframe/rAF runtime test passed")


def verify_against_classes(jvm: MiniJVM, agent_obj, orig_w: bytes, orig_loc: bytes, orig_lng: bytes, tag: str):
    TDESC = "(Ljava/lang/ClassLoader;Ljava/lang/String;Ljava/lang/Class;Ljava/security/ProtectionDomain;[B)[B"
    assert jvm.invoke("transform", TDESC, [agent_obj, None, "java/lang/String", None, None, bytearray(b"\xca\xfe\xba\xbe")]) is None

    out_w = bytes(jvm.invoke("transform", TDESC, [agent_obj, None, bj.CLS_WEBAPP, None, None, bytearray(orig_w)]))
    out_loc = bytes(jvm.invoke("transform", TDESC, [agent_obj, None, bj.CLS_LOCALE, None, None, bytearray(orig_loc)]))
    out_lng = bytes(jvm.invoke("transform", TDESC, [agent_obj, None, bj.CLS_LANG, None, None, bytearray(orig_lng)]))

    _, m_w = verify_classfile(out_w, f"[{tag}] Transformed QWebAppEngineView.class")
    _, m_loc = verify_classfile(out_loc, f"[{tag}] Transformed LocaleResourcesMgr.class")
    _, m_lng = verify_classfile(out_lng, f"[{tag}] Transformed LanguageSettings.class")

    assert bj.BAD_OPT_JS.encode("utf-8") not in out_w
    assert bj.FIX_OPT_JS.encode("utf-8") in out_w
    assert b"ibkr-cpu-fix" in out_w and b"toggle-pill-label::after" in out_w
    assert bj.JS_HIDE.encode("utf-8") in out_w and bj.JS_SHOW.encode("utf-8") in out_w

    _, _, _, _, _, _, orig_m_w = parse_raw_classfile(orig_w)
    orig_sr_code = orig_m_w[("sendSuspendResume", "(ZLjava/util/Map;)V")][1]["Code"]
    _, _, orig_sr_clen = struct.unpack_from(">HHI", orig_sr_code, 0)

    sr_code = m_w[("sendSuspendResume", "(ZLjava/util/Map;)V")][1]["Code"]
    _, _, sr_clen = struct.unpack_from(">HHI", sr_code, 0)
    assert sr_clen == orig_sr_clen + 28, f"Expected sendSuspendResume code_length {orig_sr_clen + 28}, got {sr_clen}"
    assert sr_code[8:13] == b"\x1b\x99\x00\x14\x2a", "Missing 28-byte prologue header"
    assert sr_code[36:36 + orig_sr_clen] == orig_sr_code[8:8 + orig_sr_clen], "Original method body mismatch after 28-byte prologue"

    for mn, md in (
        ("confirmTranslationLater", "()V"),
        ("confirmTranslationIfNecessary", "(Ljava/util/Locale;Ljava/lang/String;Ljava/lang/String;Z)V"),
        ("confirmTranslation", "(Ljava/util/Locale;Ljava/lang/String;Z)V"),
    ):
        assert m_loc[(mn, md)][1]["Code"][8:9] == b"\xb1"
    assert m_lng[("isTranslationConfirmed", "(Ljava/lang/String;)Z")][1]["Code"][8:10] == b"\x04\xac"


def dump_fixtures(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    syn_w, syn_loc, syn_lng, syn_w_full = build_synthetic_fixtures()
    paths = [
        (os.path.join(out_dir, "QWebAppEngineView.orig.class"), syn_w),
        (os.path.join(out_dir, "LocaleResourcesMgr.orig.class"), syn_loc),
        (os.path.join(out_dir, "LanguageSettings.orig.class"), syn_lng),
        (os.path.join(out_dir, "QWebAppEngineViewFullFrame.orig.class"), syn_w_full),
    ]
    for p, b in paths:
        with open(p, "wb") as f:
            f.write(b)
    print(f"  [DUMPED] Wrote 4 synthetic ClassFile v61.0 targets to {out_dir}")


def run_verification(jar_path: str, ibkr_jar: str = None, dump_dir: str = None):
    with zipfile.ZipFile(jar_path, "r") as zf:
        mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8")
        assert "Premain-Class: ibkr.fix.WebChannelFixAgent" in mf
        agent_bytes = zf.read("ibkr/fix/WebChannelFixAgent.class")

    verify_classfile(agent_bytes, "WebChannelFixAgent.class")

    jvm = MiniJVM(agent_bytes)
    inst = {}
    jvm.invoke("premain", "(Ljava/lang/String;Ljava/lang/instrument/Instrumentation;)V", ["", inst])
    agent_obj = inst["transformer"]

    syn_w, syn_loc, syn_lng, syn_w_full = build_synthetic_fixtures()
    verify_classfile(syn_w, "Original Mock QWebAppEngineView.class")
    verify_classfile(syn_w_full, "Original Mock QWebAppEngineViewFullFrame.class")
    verify_classfile(syn_loc, "Original Mock LocaleResourcesMgr.class")
    verify_classfile(syn_lng, "Original Mock LanguageSettings.class")
    verify_against_classes(jvm, agent_obj, syn_w, syn_loc, syn_lng, "synthetic-target")
    verify_against_classes(jvm, agent_obj, syn_w_full, syn_loc, syn_lng, "synthetic-fullframe-pc0")

    if dump_dir:
        dump_fixtures(dump_dir)

    if ibkr_jar and os.path.isfile(ibkr_jar):
        with zipfile.ZipFile(ibkr_jar, "r") as zf:
            rw = zf.read("ibkr/webview/QWebAppEngineView.class")
            rloc = zf.read("feature/configure/language/LocaleResourcesMgr.class")
            rlng = zf.read("jsetting/LanguageSettings.class")
        verify_against_classes(jvm, agent_obj, rw, rloc, rlng, os.path.basename(ibkr_jar))

    run_node_js_test()
    print("[verify_agent] ALL CHECKS PASSED")


def main():
    ap = argparse.ArgumentParser(description="Verify ibkr-webchannel-fix.jar")
    ap.add_argument("--jar", default=bj.DEFAULT_OUT, help="Agent JAR to verify")
    ap.add_argument("--ibkr-jar", default=None, help="Optional path to IBKR ntws4launch-*.jar")
    ap.add_argument("--dump-fixtures", default=None, help="Optional directory to write synthetic .orig.class files")
    args = ap.parse_args()
    run_verification(args.jar, args.ibkr_jar, args.dump_fixtures)


if __name__ == "__main__":
    main()
