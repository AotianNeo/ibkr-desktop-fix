#!/usr/bin/env python3
"""
Builds dist/ibkr-webchannel-fix.jar (ClassFile v61.0 / Java 17).

Supports two interchangeable backends:
  1. Standard JDK `javac --release 17` (used automatically when a working JDK is present).
  2. Built-in zero-dependency Python ClassFile v61.0 bytecode assembler (used automatically
     on stock trader machines where only Python 3 + IBKR Desktop's bundled JRE exist, with
     no external JDK required).
"""

import argparse
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_SRC = os.path.join(REPO_ROOT, "src", "ibkr", "fix", "WebChannelFixAgent.java")
DEFAULT_OUT = os.path.join(REPO_ROOT, "dist", "ibkr-webchannel-fix.jar")

CLS_WEBAPP = "ibkr/webview/QWebAppEngineView"
CLS_LOCALE = "feature/configure/language/LocaleResourcesMgr"
CLS_LANG = "jsetting/LanguageSettings"

BAD_OPT_JS = "window.$app.optionChanged.connect(function(o){ if(window.$app) { window.$app.option = o; } } );"
FIX_OPT_JS = "/* ibkr-webchannel-fix: qwebchannel.js already updates __propertyCache__ */"
WHEEL_PREFIX = "document.body.addEventListener( 'wheel'"

USER_SCRIPT_JS = (
    "(function(){\n"
    "if(window.__ibkrCpuFixInstalled)return;\n"
    "window.__ibkrCpuFixInstalled=true;\n"
    "var root=document.body||document.documentElement;\n"
    "if(root){root.addEventListener('wheel',function(e){if(e.ctrlKey)e.preventDefault();},{capture:true,passive:false});}\n"
    "var CSS='.toggle-pill-label::after,.toggle-pill-input:checked+.toggle-pill-label::after,#instant-trading-switch-label::after,"
    ":root.show-trading-button-notice button[data-name=\"trading\"]>:first-child::after,"
    "[class*=\"blinking-\"]::after,[class*=\"blinking-\"]:after,[class*=\"animated-TJ9ObuLF\"],"
    ".gen-ai-icon__new,.top-level-notify__pulse-dot,.player__muted,.intimpact-settings__ackTip span svg"
    "{animation:none !important;-webkit-animation:none !important;animation-play-state:paused !important;}"
    "html[data-ibkr-hidden=\"1\"],html[data-ibkr-hidden=\"1\"] *,html[data-ibkr-hidden=\"1\"] *::before,html[data-ibkr-hidden=\"1\"] *::after"
    "{animation-play-state:paused !important;-webkit-animation-play-state:paused !important;transition:none !important;}';\n"
    "function ensureStyle(doc){\n"
    "  try{\n"
    "    if(!doc||doc.getElementById('ibkr-cpu-fix'))return;\n"
    "    var s=doc.createElement('style');s.id='ibkr-cpu-fix';s.textContent=CSS;\n"
    "    var h=doc.head||doc.documentElement;if(h)h.appendChild(s);\n"
    "  }catch(e){}\n"
    "}\n"
    "ensureStyle(document);\n"
    "var origRAF=window.requestAnimationFrame?window.requestAnimationFrame.bind(window):null;\n"
    "var origCAF=window.cancelAnimationFrame?window.cancelAnimationFrame.bind(window):null;\n"
    "var RAF_OFF=1073741824;\n"
    "function wrappedCAF(id){\n"
    "  if(id>=RAF_OFF)window.clearTimeout(id-RAF_OFF);\n"
    "  else if(origCAF)origCAF(id);\n"
    "}\n"
    "if(origCAF)window.cancelAnimationFrame=wrappedCAF;\n"
    "var hostHidden=!!window.__ibkrHostHidden;\n"
    "try{if(!hostHidden&&window.parent&&window.parent!==window&&window.parent.__ibkrHostHidden)hostHidden=true;}catch(e){}\n"
    "var hidden=hostHidden||!!document.hidden;\n"
    "function patchFrame(f){\n"
    "  try{\n"
    "    if(!f)return;\n"
    "    var cw=f.contentWindow;\n"
    "    var cd=f.contentDocument||(cw&&cw.document);\n"
    "    if(cd)ensureStyle(cd);\n"
    "    if(cw&&cw!==window){\n"
    "      cw.__ibkrHostHidden=!!window.__ibkrHostHidden;\n"
    "      if(cw.__ibkrSetHidden)cw.__ibkrSetHidden(hidden);\n"
    "      else if(cd&&cd.documentElement){\n"
    "        if(hidden)cd.documentElement.setAttribute('data-ibkr-hidden','1');\n"
    "        else cd.documentElement.removeAttribute('data-ibkr-hidden');\n"
    "      }\n"
    "    }\n"
    "  }catch(e){}\n"
    "}\n"
    "function applyHidden(h){\n"
    "  hidden=!!h;\n"
    "  ensureStyle(document);\n"
    "  var de=document.documentElement;\n"
    "  if(de){if(hidden)de.setAttribute('data-ibkr-hidden','1');else de.removeAttribute('data-ibkr-hidden');}\n"
    "  if(origRAF){\n"
    "    if(hidden){\n"
    "      window.requestAnimationFrame=function(cb){return RAF_OFF+window.setTimeout(function(){cb(performance.now());},250);};\n"
    "    }else{\n"
    "      window.requestAnimationFrame=origRAF;\n"
    "    }\n"
    "  }\n"
    "  var fs=document.getElementsByTagName('iframe');\n"
    "  for(var i=0;i<fs.length;i++)patchFrame(fs[i]);\n"
    "}\n"
    "window.__ibkrSetHidden=function(h){window.__ibkrHostHidden=!!h;applyHidden(!!h||!!document.hidden);};\n"
    "window.addEventListener('load',function(e){\n"
    "  var t=e&&e.target;\n"
    "  if(t&&t.tagName==='IFRAME')patchFrame(t);\n"
    "  else if(t===document)applyHidden(hidden);\n"
    "},true);\n"
    "document.addEventListener('visibilitychange',function(){applyHidden(!!window.__ibkrHostHidden||!!document.hidden);});\n"
    "applyHidden(hidden);\n"
    "})();\n"
)

JS_HIDE = "window.__ibkrHostHidden=true;window.__ibkrSetHidden&&window.__ibkrSetHidden(true);"
JS_SHOW = "window.__ibkrHostHidden=false;window.__ibkrSetHidden&&window.__ibkrSetHidden(false);"

MANIFEST_BYTES = (
    "Manifest-Version: 1.0\r\n"
    "Premain-Class: ibkr.fix.WebChannelFixAgent\r\n"
    "Can-Redefine-Classes: false\r\n"
    "Can-Retransform-Classes: false\r\n"
    "\r\n"
).encode("utf-8")


class ConstantPool:
    def __init__(self):
        self.entries = [None]
        self.map = {}

    def _add(self, key, raw):
        if key in self.map:
            return self.map[key]
        idx = len(self.entries)
        self.entries.append((key, raw))
        self.map[key] = idx
        if key[0] in (5, 6):
            self.entries.append(None)
        return idx

    def utf8(self, s: str) -> int:
        b = s.encode("utf-8")
        assert len(b) <= 65535
        return self._add((1, s), struct.pack(">BH", 1, len(b)) + b)

    def integer(self, v: int) -> int:
        return self._add((3, v), struct.pack(">Bi", 3, v))

    def cls(self, name: str) -> int:
        return self._add((7, name), struct.pack(">BH", 7, self.utf8(name)))

    def string(self, s: str) -> int:
        return self._add((8, s), struct.pack(">BH", 8, self.utf8(s)))

    def nat(self, name: str, desc: str) -> int:
        return self._add((12, name, desc), struct.pack(">BHH", 12, self.utf8(name), self.utf8(desc)))

    def fieldref(self, cname: str, name: str, desc: str) -> int:
        return self._add((9, cname, name, desc), struct.pack(">BHH", 9, self.cls(cname), self.nat(name, desc)))

    def methodref(self, cname: str, name: str, desc: str) -> int:
        return self._add((10, cname, name, desc), struct.pack(">BHH", 10, self.cls(cname), self.nat(name, desc)))

    def imethodref(self, cname: str, name: str, desc: str) -> int:
        return self._add((11, cname, name, desc), struct.pack(">BHH", 11, self.cls(cname), self.nat(name, desc)))

    def serialize(self) -> bytes:
        out = bytearray(struct.pack(">H", len(self.entries)))
        for e in self.entries[1:]:
            if e is not None:
                out.extend(e[1])
        return bytes(out)


def parse_method_desc(desc: str):
    assert desc[0] == "("
    i = 1
    args = []
    while desc[i] != ")":
        start = i
        while desc[i] == "[":
            i += 1
        if desc[i] == "L":
            i = desc.index(";", i) + 1
        else:
            i += 1
        args.append(desc[start:i])
    return args, desc[i + 1:]


def desc_to_vtype(d: str):
    if d in ("Z", "B", "C", "S", "I"): return ("I",)
    if d == "F": return ("F",)
    if d == "J": return ("J",)
    if d == "D": return ("D",)
    if d.startswith("["): return ("O", d)
    if d.startswith("L") and d.endswith(";"): return ("O", d[1:-1])
    raise ValueError(f"Bad descriptor {d}")


SUPER_MAP = {
    "ibkr/fix/WebChannelFixAgent": "java/lang/Object",
    "java/io/ByteArrayOutputStream": "java/io/OutputStream",
    "java/io/DataOutputStream": "java/io/FilterOutputStream",
    "java/io/FilterOutputStream": "java/io/OutputStream",
    "java/io/OutputStream": "java/lang/Object",
    "java/io/IOException": "java/lang/Exception",
    "java/lang/IllegalArgumentException": "java/lang/RuntimeException",
    "java/lang/RuntimeException": "java/lang/Exception",
    "java/lang/Exception": "java/lang/Throwable",
    "java/lang/Throwable": "java/lang/Object",
    "java/lang/StringBuilder": "java/lang/Object",
    "java/lang/String": "java/lang/Object",
    "java/io/PrintStream": "java/lang/Object",
}

IFACE_MAP = {
    "ibkr/fix/WebChannelFixAgent": {"java/lang/instrument/ClassFileTransformer"},
}


def is_subtype(sub: str, sup: str) -> bool:
    if sub == sup or sup == "java/lang/Object":
        return True
    if sub.startswith("[") and sup in ("java/lang/Cloneable", "java/io/Serializable"):
        return True
    cur = sub
    while cur in SUPER_MAP:
        if sup in IFACE_MAP.get(cur, ()):
            return True
        cur = SUPER_MAP[cur]
        if cur == sup:
            return True
    return False


def lub_vtype(t1, t2):
    if t1 == t2: return t1
    if t1 == ("TOP",) or t2 == ("TOP",): return ("TOP",)
    if t1 == ("NULL",): return t2 if t2[0] == "O" else ("TOP",)
    if t2 == ("NULL",): return t1 if t1[0] == "O" else ("TOP",)
    if t1[0] == "O" and t2[0] == "O":
        if is_subtype(t1[1], t2[1]): return ("O", t2[1])
        if is_subtype(t2[1], t1[1]): return ("O", t1[1])
        return ("O", "java/lang/Object")
    return ("TOP",)


def encode_vtype(vt, cp: ConstantPool) -> bytes:
    k = vt[0]
    if k == "TOP": return b"\x00"
    if k == "I": return b"\x01"
    if k == "F": return b"\x02"
    if k == "D": return b"\x03"
    if k == "J": return b"\x04"
    if k == "NULL": return b"\x05"
    if k == "UNINIT_THIS": return b"\x06"
    if k == "O": return struct.pack(">BH", 7, cp.cls(vt[1]))
    if k == "UNINIT": return struct.pack(">BH", 8, vt[1])
    raise ValueError(f"Unknown vtype {vt}")


class MethodBuilder:
    def __init__(self, cp: ConstantPool, this_cls: str, acc: int, name: str, desc: str):
        self.cp = cp
        self.this_cls = this_cls
        self.acc = acc
        self.name = name
        self.desc = desc
        self.insns = []
        self.handlers = []

    def label(self, name: str):
        self.insns.append(("LABEL", name))
        return self

    def try_catch(self, s_lbl: str, e_lbl: str, h_lbl: str, catch_cls: str):
        self.handlers.append((s_lbl, e_lbl, h_lbl, catch_cls))
        return self

    def op(self, opcode_name: str, *args):
        self.insns.append((opcode_name, *args))
        return self

    def assemble(self) -> bytes:
        cp = self.cp
        labels = {}
        pc = 0
        norm = []
        for item in self.insns:
            if item[0] == "LABEL":
                labels[item[1]] = pc
                continue
            sz = self._insn_size(item)
            norm.append((pc, item, sz))
            pc += sz

        bc = bytearray()
        branch_targets = set()
        for ipc, item, sz in norm:
            enc, tgt = self._encode_insn(ipc, item, labels)
            assert len(enc) == sz
            bc.extend(enc)
            if tgt is not None:
                branch_targets.add(tgt)

        ex_table = []
        for s_lbl, e_lbl, h_lbl, ccls in self.handlers:
            sp, ep, hp = labels[s_lbl], labels[e_lbl], labels[h_lbl]
            cidx = cp.cls(ccls) if ccls else 0
            ex_table.append((sp, ep, hp, cidx))
            branch_targets.add(hp)

        args_d, ret_d = parse_method_desc(self.desc)
        init_locals = []
        if not (self.acc & 0x0008):
            init_locals.append(("UNINIT_THIS",) if self.name == "<init>" else ("O", self.this_cls))
        for ad in args_d:
            vt = desc_to_vtype(ad)
            init_locals.append(vt)
            if vt[0] in ("J", "D"):
                init_locals.append(("TOP",))

        pc_to_idx = {ipc: idx for idx, (ipc, _, _) in enumerate(norm)}
        in_frames = {0: (list(init_locals), [])}
        worklist = [0]
        max_stack = 0
        max_locals = len(init_locals)

        def merge_into(target_pc, loc, stk):
            nonlocal max_locals
            if len(loc) > max_locals:
                max_locals = len(loc)
            if target_pc not in in_frames:
                in_frames[target_pc] = (list(loc), list(stk))
                worklist.append(target_pc)
                return
            old_loc, old_stk = in_frames[target_pc]
            changed = False
            new_stk = []
            for a, b in zip(old_stk, stk):
                m = lub_vtype(a, b)
                if m != a:
                    changed = True
                new_stk.append(m)
            mlen = max(len(old_loc), len(loc))
            new_loc = []
            for k in range(mlen):
                a = old_loc[k] if k < len(old_loc) else ("TOP",)
                b = loc[k] if k < len(loc) else ("TOP",)
                m = lub_vtype(a, b)
                if k >= len(old_loc) or m != a:
                    changed = True
                new_loc.append(m)
            if changed:
                in_frames[target_pc] = (new_loc, new_stk)
                worklist.append(target_pc)

        while worklist:
            cur_pc = worklist.pop()
            loc, stk = [list(x) for x in in_frames[cur_pc]]
            if len(stk) > max_stack:
                max_stack = len(stk)
            ipc, item, sz = norm[pc_to_idx[cur_pc]]
            for sp, ep, hp, cidx in ex_table:
                if sp <= ipc < ep:
                    ccls = cp.entries[cidx][0][1] if cidx else "java/lang/Throwable"
                    merge_into(hp, loc, [("O", ccls)])
            fallthrough, targets = self._step_type(ipc, item, loc, stk, labels, ret_d)
            if len(stk) > max_stack:
                max_stack = len(stk)
            if len(loc) > max_locals:
                max_locals = len(loc)
            for tpc in targets:
                merge_into(tpc, loc, stk)
            if fallthrough:
                merge_into(ipc + sz, loc, stk)

        smt_pcs = sorted(p for p in branch_targets if p in in_frames)
        smt_bytes = bytearray()
        if smt_pcs:
            smt_bytes.extend(struct.pack(">H", len(smt_pcs)))
            prev_pc = -1
            for tpc in smt_pcs:
                delta = tpc if prev_pc == -1 else (tpc - prev_pc - 1)
                prev_pc = tpc
                floc, fstk = in_frames[tpc]
                trimmed = list(floc)
                while trimmed and trimmed[-1] == ("TOP",):
                    if len(trimmed) >= 2 and trimmed[-2][0] in ("J", "D"):
                        break
                    trimmed.pop()
                enc_locs = []
                k = 0
                while k < len(trimmed):
                    vt = trimmed[k]
                    enc_locs.append(encode_vtype(vt, cp))
                    k += 2 if vt[0] in ("J", "D") else 1
                enc_stks = [encode_vtype(vt, cp) for vt in fstk]
                smt_bytes.append(255)
                smt_bytes.extend(struct.pack(">HH", delta, len(enc_locs)))
                for e in enc_locs:
                    smt_bytes.extend(e)
                smt_bytes.extend(struct.pack(">H", len(enc_stks)))
                for e in enc_stks:
                    smt_bytes.extend(e)

        code_attr = bytearray()
        code_attr.extend(struct.pack(">HHI", max_stack, max_locals, len(bc)))
        code_attr.extend(bc)
        code_attr.extend(struct.pack(">H", len(ex_table)))
        for sp, ep, hp, cidx in ex_table:
            code_attr.extend(struct.pack(">HHHH", sp, ep, hp, cidx))
        if smt_bytes:
            code_attr.extend(struct.pack(">HHI", 1, cp.utf8("StackMapTable"), len(smt_bytes)))
            code_attr.extend(smt_bytes)
        else:
            code_attr.extend(struct.pack(">H", 0))

        m_out = bytearray()
        m_out.extend(struct.pack(">HHHH", self.acc, cp.utf8(self.name), cp.utf8(self.desc), 1))
        m_out.extend(struct.pack(">HI", cp.utf8("Code"), len(code_attr)))
        m_out.extend(code_attr)
        return bytes(m_out)

    def _insn_size(self, item) -> int:
        k = item[0]
        if k in ("ACONST_NULL", "ICONST_M1", "ICONST_0", "ICONST_1", "ICONST_2", "ICONST_3", "ICONST_4", "ICONST_5",
                 "IALOAD", "AALOAD", "BALOAD", "AASTORE", "IASTORE", "BASTORE", "POP", "DUP",
                 "IADD", "ISUB", "IMUL", "ISHL", "ISHR", "IUSHR", "IAND", "IOR", "IXOR",
                 "I2B", "IRETURN", "ARETURN", "RETURN", "ARRAYLENGTH", "ATHROW"):
            return 1
        if k in ("BIPUSH", "NEWARRAY"):
            return 2
        if k in ("SIPUSH", "IFEQ", "IFNE", "IFLT", "IFGE", "IFGT", "IFLE",
                 "IF_ICMPEQ", "IF_ICMPNE", "IF_ICMPLT", "IF_ICMPGE", "IF_ICMPGT", "IF_ICMPLE",
                 "IF_ACMPEQ", "IF_ACMPNE", "GOTO", "IFNULL", "IFNONNULL",
                 "GETSTATIC", "PUTSTATIC", "GETFIELD", "PUTFIELD",
                 "INVOKEVIRTUAL", "INVOKESPECIAL", "INVOKESTATIC", "NEW", "ANEWARRAY", "CHECKCAST",
                 "LDC_W", "LDC_INT", "IINC"):
            return 3
        if k == "INVOKEINTERFACE":
            return 5
        if k in ("ILOAD", "ISTORE", "ALOAD", "ASTORE"):
            return 1 if item[1] <= 3 else 2
        raise ValueError(f"Unknown insn {item}")

    def _encode_insn(self, pc: int, item, labels):
        cp = self.cp
        k = item[0]
        SIMPLE = {
            "ACONST_NULL": 0x01, "ICONST_M1": 0x02, "ICONST_0": 0x03, "ICONST_1": 0x04,
            "ICONST_2": 0x05, "ICONST_3": 0x06, "ICONST_4": 0x07, "ICONST_5": 0x08,
            "IALOAD": 0x2E, "AALOAD": 0x32, "BALOAD": 0x33,
            "IASTORE": 0x4F, "AASTORE": 0x53, "BASTORE": 0x54,
            "POP": 0x57, "DUP": 0x59,
            "IADD": 0x60, "ISUB": 0x64, "IMUL": 0x68,
            "ISHL": 0x78, "ISHR": 0x7A, "IUSHR": 0x7C, "IAND": 0x7E, "IOR": 0x80, "IXOR": 0x82,
            "I2B": 0x91, "IRETURN": 0xAC, "ARETURN": 0xB0, "RETURN": 0xB1,
            "ARRAYLENGTH": 0xBE, "ATHROW": 0xBF,
        }
        if k in SIMPLE: return bytes([SIMPLE[k]]), None
        if k == "BIPUSH": return struct.pack(">Bb", 0x10, item[1]), None
        if k == "SIPUSH": return struct.pack(">Bh", 0x11, item[1]), None
        if k == "LDC_W": return struct.pack(">BH", 0x13, cp.string(item[1])), None
        if k == "LDC_INT": return struct.pack(">BH", 0x13, cp.integer(item[1])), None
        if k in ("ILOAD", "ALOAD", "ISTORE", "ASTORE"):
            slot = item[1]
            short_base, wide_op = {"ILOAD": (0x1A, 0x15), "ALOAD": (0x2A, 0x19), "ISTORE": (0x3B, 0x36), "ASTORE": (0x4B, 0x3A)}[k]
            return (bytes([short_base + slot]) if slot <= 3 else struct.pack(">BB", wide_op, slot)), None
        if k == "IINC": return struct.pack(">BBb", 0x84, item[1], item[2]), None
        BRANCH = {
            "IFEQ": 0x99, "IFNE": 0x9A, "IFLT": 0x9B, "IFGE": 0x9C, "IFGT": 0x9D, "IFLE": 0x9E,
            "IF_ICMPEQ": 0x9F, "IF_ICMPNE": 0xA0, "IF_ICMPLT": 0xA1, "IF_ICMPGE": 0xA2,
            "IF_ICMPGT": 0xA3, "IF_ICMPLE": 0xA4, "IF_ACMPEQ": 0xA5, "IF_ACMPNE": 0xA6,
            "GOTO": 0xA7, "IFNULL": 0xC6, "IFNONNULL": 0xC7,
        }
        if k in BRANCH:
            tgt = labels[item[1]]
            return struct.pack(">Bh", BRANCH[k], tgt - pc), tgt
        if k == "GETSTATIC": return struct.pack(">BH", 0xB2, cp.fieldref(item[1], item[2], item[3])), None
        if k in ("INVOKEVIRTUAL", "INVOKESPECIAL", "INVOKESTATIC"):
            ops = {"INVOKEVIRTUAL": 0xB6, "INVOKESPECIAL": 0xB7, "INVOKESTATIC": 0xB8}
            return struct.pack(">BH", ops[k], cp.methodref(item[1], item[2], item[3])), None
        if k == "INVOKEINTERFACE":
            args_d, _ = parse_method_desc(item[3])
            cnt = 1 + sum(2 if d in ("J", "D") else 1 for d in args_d)
            return struct.pack(">BHBB", 0xB9, cp.imethodref(item[1], item[2], item[3]), cnt, 0), None
        if k == "NEW": return struct.pack(">BH", 0xBB, cp.cls(item[1])), None
        if k == "NEWARRAY": return struct.pack(">BB", 0xBC, {"Z": 4, "B": 8, "I": 10}[item[1]]), None
        if k == "ANEWARRAY": return struct.pack(">BH", 0xBD, cp.cls(item[1])), None
        if k == "CHECKCAST": return struct.pack(">BH", 0xC0, cp.cls(item[1])), None
        raise ValueError(f"Unhandled encode {item}")

    def _step_type(self, pc: int, item, loc: list, stk: list, labels, ret_d: str):
        k = item[0]

        def set_loc(slot, vt):
            while len(loc) <= slot:
                loc.append(("TOP",))
            if slot > 0 and loc[slot - 1][0] in ("J", "D"):
                loc[slot - 1] = ("TOP",)
            loc[slot] = vt

        def pop_i():
            assert stk.pop() == ("I",)

        def pop_ref():
            t = stk.pop()
            assert t[0] in ("O", "NULL", "UNINIT_THIS", "UNINIT")
            return t

        if k == "ACONST_NULL": stk.append(("NULL",)); return True, []
        if k in ("ICONST_M1", "ICONST_0", "ICONST_1", "ICONST_2", "ICONST_3", "ICONST_4", "ICONST_5", "BIPUSH", "SIPUSH", "LDC_INT"):
            stk.append(("I",)); return True, []
        if k == "LDC_W": stk.append(("O", "java/lang/String")); return True, []
        if k == "ILOAD": stk.append(("I",)); return True, []
        if k == "ALOAD": stk.append(loc[item[1]]); return True, []
        if k == "ISTORE": pop_i(); set_loc(item[1], ("I",)); return True, []
        if k == "ASTORE": set_loc(item[1], pop_ref()); return True, []
        if k == "IINC": return True, []
        if k in ("IALOAD", "BALOAD"): pop_i(); pop_ref(); stk.append(("I",)); return True, []
        if k == "AALOAD":
            pop_i(); arr = pop_ref()
            stk.append(("NULL",) if arr == ("NULL",) else desc_to_vtype(arr[1][1:]))
            return True, []
        if k in ("IASTORE", "BASTORE"): pop_i(); pop_i(); pop_ref(); return True, []
        if k == "AASTORE": pop_ref(); pop_i(); pop_ref(); return True, []
        if k == "POP": stk.pop(); return True, []
        if k == "DUP": stk.append(stk[-1]); return True, []
        if k in ("IADD", "ISUB", "IMUL", "ISHL", "ISHR", "IUSHR", "IAND", "IOR", "IXOR"):
            pop_i(); pop_i(); stk.append(("I",)); return True, []
        if k == "I2B": pop_i(); stk.append(("I",)); return True, []
        if k == "ARRAYLENGTH": pop_ref(); stk.append(("I",)); return True, []
        if k in ("IFEQ", "IFNE", "IFLT", "IFGE", "IFGT", "IFLE"): pop_i(); return True, [labels[item[1]]]
        if k in ("IF_ICMPEQ", "IF_ICMPNE", "IF_ICMPLT", "IF_ICMPGE", "IF_ICMPGT", "IF_ICMPLE"):
            pop_i(); pop_i(); return True, [labels[item[1]]]
        if k in ("IF_ACMPEQ", "IF_ACMPNE"): pop_ref(); pop_ref(); return True, [labels[item[1]]]
        if k in ("IFNULL", "IFNONNULL"): pop_ref(); return True, [labels[item[1]]]
        if k == "GOTO": return False, [labels[item[1]]]
        if k == "IRETURN": pop_i(); return False, []
        if k == "ARETURN": pop_ref(); return False, []
        if k == "RETURN": return False, []
        if k == "ATHROW": pop_ref(); return False, []
        if k == "GETSTATIC": stk.append(desc_to_vtype(item[3])); return True, []
        if k == "NEW": stk.append(("UNINIT", pc)); return True, []
        if k == "NEWARRAY": pop_i(); stk.append(("O", "[" + item[1])); return True, []
        if k == "ANEWARRAY":
            pop_i()
            ed = item[1] if item[1].startswith("[") else f"L{item[1]};"
            stk.append(("O", "[" + ed)); return True, []
        if k in ("INVOKEVIRTUAL", "INVOKESPECIAL", "INVOKESTATIC", "INVOKEINTERFACE"):
            cname, mname, mdesc = item[1], item[2], item[3]
            args_d, mr = parse_method_desc(mdesc)
            for _ in args_d:
                stk.pop()
            if k != "INVOKESTATIC":
                recv = pop_ref()
                if mname == "<init>":
                    init_type = ("O", self.this_cls if recv[0] == "UNINIT_THIS" else cname)
                    for idx in range(len(loc)):
                        if loc[idx] == recv: loc[idx] = init_type
                    for idx in range(len(stk)):
                        if stk[idx] == recv: stk[idx] = init_type
            if mr != "V":
                stk.append(desc_to_vtype(mr))
            return True, []
        raise ValueError(f"Unhandled type step {item}")


def assemble_agent_class_python() -> bytes:
    with open(JAVA_SRC, "r", encoding="utf-8") as f:
        java_text = f.read()
    for s in (BAD_OPT_JS, FIX_OPT_JS, WHEEL_PREFIX, JS_HIDE, JS_SHOW):
        assert s in java_text, f"Source drift detected for {s!r}"

    cp = ConstantPool()
    THIS = "ibkr/fix/WebChannelFixAgent"
    SUPER = "java/lang/Object"
    IFACE = "java/lang/instrument/ClassFileTransformer"

    this_idx = cp.cls(THIS)
    super_idx = cp.cls(SUPER)
    iface_idx = cp.cls(IFACE)
    methods = []

    m = MethodBuilder(cp, THIS, 0x0001, "<init>", "()V")
    m.op("ALOAD", 0); m.op("INVOKESPECIAL", SUPER, "<init>", "()V"); m.op("RETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x0009, "premain", "(Ljava/lang/String;Ljava/lang/instrument/Instrumentation;)V")
    m.op("ALOAD", 1); m.op("NEW", THIS); m.op("DUP"); m.op("INVOKESPECIAL", THIS, "<init>", "()V")
    m.op("INVOKEINTERFACE", "java/lang/instrument/Instrumentation", "addTransformer",
         "(Ljava/lang/instrument/ClassFileTransformer;)V")
    m.op("RETURN")
    methods.append(m.assemble())

    m = MethodBuilder(
        cp, THIS, 0x0001, "transform",
        "(Ljava/lang/ClassLoader;Ljava/lang/String;Ljava/lang/Class;Ljava/security/ProtectionDomain;[B)[B"
    )
    m.op("ALOAD", 2); m.op("IFNULL", "ret_null_0")
    m.op("ALOAD", 5); m.op("IFNONNULL", "try_start")
    m.label("ret_null_0"); m.op("ACONST_NULL"); m.op("ARETURN")
    m.label("try_start")
    m.op("LDC_W", CLS_WEBAPP); m.op("ALOAD", 2)
    m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk_locale")
    m.op("ALOAD", 5); m.op("INVOKESTATIC", THIS, "patchWebApp", "([B)[B"); m.op("ARETURN")
    m.label("chk_locale")
    m.op("LDC_W", CLS_LOCALE); m.op("ALOAD", 2)
    m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk_lang")
    m.op("ALOAD", 5); m.op("ICONST_0"); m.op("INVOKESTATIC", THIS, "patchTranslationMethods", "([BZ)[B"); m.op("ARETURN")
    m.label("chk_lang")
    m.op("LDC_W", CLS_LANG); m.op("ALOAD", 2)
    m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "try_end")
    m.op("ALOAD", 5); m.op("ICONST_1"); m.op("INVOKESTATIC", THIS, "patchTranslationMethods", "([BZ)[B"); m.op("ARETURN")
    m.label("try_end"); m.op("ACONST_NULL"); m.op("ARETURN")
    m.label("catch_err")
    m.op("ASTORE", 6)
    m.op("GETSTATIC", "java/lang/System", "err", "Ljava/io/PrintStream;")
    m.op("NEW", "java/lang/StringBuilder"); m.op("DUP")
    m.op("LDC_W", "[ibkr-webchannel-fix] Failed transforming ")
    m.op("INVOKESPECIAL", "java/lang/StringBuilder", "<init>", "(Ljava/lang/String;)V")
    m.op("ALOAD", 2); m.op("INVOKEVIRTUAL", "java/lang/StringBuilder", "append", "(Ljava/lang/String;)Ljava/lang/StringBuilder;")
    m.op("LDC_W", ": "); m.op("INVOKEVIRTUAL", "java/lang/StringBuilder", "append", "(Ljava/lang/String;)Ljava/lang/StringBuilder;")
    m.op("ALOAD", 6); m.op("INVOKEVIRTUAL", "java/lang/StringBuilder", "append", "(Ljava/lang/Object;)Ljava/lang/StringBuilder;")
    m.op("INVOKEVIRTUAL", "java/lang/StringBuilder", "toString", "()Ljava/lang/String;")
    m.op("INVOKEVIRTUAL", "java/io/PrintStream", "println", "(Ljava/lang/String;)V")
    m.op("ACONST_NULL"); m.op("ARETURN")
    m.try_catch("try_start", "try_end", "catch_err", "java/lang/Throwable")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "u2", "([BI)I")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND"); m.op("BIPUSH", 8); m.op("ISHL")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_1"); m.op("IADD"); m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND")
    m.op("IOR"); m.op("IRETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "i4", "([BI)I")
    for shift, add in ((24, 0), (16, 1), (8, 2), (0, 3)):
        m.op("ALOAD", 0); m.op("ILOAD", 1)
        if add: m.op(f"ICONST_{add}"); m.op("IADD")
        m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND")
        if shift: m.op("BIPUSH", shift); m.op("ISHL")
        if add: m.op("IOR")
    m.op("IRETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "cpEntrySize", "([BI)I")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND"); m.op("ISTORE", 2)
    m.op("ILOAD", 2); m.op("ICONST_1"); m.op("IF_ICMPNE", "not1")
    m.op("ICONST_3"); m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_1"); m.op("IADD")
    m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("IADD"); m.op("IRETURN")
    m.label("not1")
    for t in (3, 4, 9, 10, 11, 12, 17, 18): m.op("ILOAD", 2); m.op("BIPUSH", t); m.op("IF_ICMPEQ", "ret5")
    for t in (5, 6): m.op("ILOAD", 2); m.op("BIPUSH", t); m.op("IF_ICMPEQ", "ret9")
    for t in (7, 8, 16, 19, 20): m.op("ILOAD", 2); m.op("BIPUSH", t); m.op("IF_ICMPEQ", "ret3")
    m.op("ILOAD", 2); m.op("BIPUSH", 15); m.op("IF_ICMPEQ", "ret4")
    m.op("NEW", "java/lang/IllegalArgumentException"); m.op("DUP"); m.op("LDC_W", "Unknown CP tag")
    m.op("INVOKESPECIAL", "java/lang/IllegalArgumentException", "<init>", "(Ljava/lang/String;)V"); m.op("ATHROW")
    m.label("ret5"); m.op("ICONST_5"); m.op("IRETURN")
    m.label("ret9"); m.op("BIPUSH", 9); m.op("IRETURN")
    m.label("ret3"); m.op("ICONST_3"); m.op("IRETURN")
    m.label("ret4"); m.op("ICONST_4"); m.op("IRETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "skipMembers", "([BI)I")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 2)
    m.op("IINC", 1, 2); m.op("ICONST_0"); m.op("ISTORE", 3)
    m.label("outer"); m.op("ILOAD", 3); m.op("ILOAD", 2); m.op("IF_ICMPGE", "done")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("BIPUSH", 6); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 4)
    m.op("IINC", 1, 8); m.op("ICONST_0"); m.op("ISTORE", 5)
    m.label("inner"); m.op("ILOAD", 5); m.op("ILOAD", 4); m.op("IF_ICMPGE", "next_outer")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I"); m.op("ISTORE", 6)
    m.op("ILOAD", 1); m.op("BIPUSH", 6); m.op("IADD"); m.op("ILOAD", 6); m.op("IADD"); m.op("ISTORE", 1)
    m.op("IINC", 5, 1); m.op("GOTO", "inner")
    m.label("next_outer"); m.op("IINC", 3, 1); m.op("GOTO", "outer")
    m.label("done"); m.op("ILOAD", 1); m.op("IRETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "indexOfBytes", "([BII[B)I")
    m.op("ILOAD", 1); m.op("ILOAD", 2); m.op("IADD"); m.op("ALOAD", 3); m.op("ARRAYLENGTH"); m.op("ISUB"); m.op("ISTORE", 4)
    m.op("ILOAD", 1); m.op("ISTORE", 5)
    m.label("outer"); m.op("ILOAD", 5); m.op("ILOAD", 4); m.op("IF_ICMPGT", "not_found")
    m.op("ICONST_0"); m.op("ISTORE", 6)
    m.label("inner"); m.op("ILOAD", 6); m.op("ALOAD", 3); m.op("ARRAYLENGTH"); m.op("IF_ICMPGE", "found")
    m.op("ALOAD", 0); m.op("ILOAD", 5); m.op("ILOAD", 6); m.op("IADD"); m.op("BALOAD")
    m.op("ALOAD", 3); m.op("ILOAD", 6); m.op("BALOAD"); m.op("IF_ICMPNE", "next_i")
    m.op("IINC", 6, 1); m.op("GOTO", "inner")
    m.label("found"); m.op("ILOAD", 5); m.op("IRETURN")
    m.label("next_i"); m.op("IINC", 5, 1); m.op("GOTO", "outer")
    m.label("not_found"); m.op("ICONST_M1"); m.op("IRETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "writeUtf8Entry", "(Ljava/io/DataOutputStream;[B)V")
    m.op("ALOAD", 0); m.op("ICONST_1"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 0); m.op("ALOAD", 1); m.op("ARRAYLENGTH"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ALOAD", 1); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([B)V"); m.op("RETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "writeStringEntry", "(Ljava/io/DataOutputStream;I)V")
    m.op("ALOAD", 0); m.op("BIPUSH", 8); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V"); m.op("RETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "matchTranslationMethod", "(ZLjava/lang/String;Ljava/lang/String;)I")
    m.op("ILOAD", 0); m.op("IFEQ", "locale_cls")
    m.op("LDC_W", "isTranslationConfirmed"); m.op("ALOAD", 1); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "ret0_a")
    m.op("LDC_W", "(Ljava/lang/String;)Z"); m.op("ALOAD", 2); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "ret0_a")
    m.op("ICONST_4"); m.op("IRETURN")
    m.label("ret0_a"); m.op("ICONST_0"); m.op("IRETURN")
    m.label("locale_cls")
    m.op("LDC_W", "confirmTranslationLater"); m.op("ALOAD", 1); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk2")
    m.op("LDC_W", "()V"); m.op("ALOAD", 2); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk2")
    m.op("ICONST_1"); m.op("IRETURN")
    m.label("chk2")
    m.op("LDC_W", "confirmTranslationIfNecessary"); m.op("ALOAD", 1); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk3")
    m.op("LDC_W", "(Ljava/util/Locale;Ljava/lang/String;Ljava/lang/String;Z)V"); m.op("ALOAD", 2); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk3")
    m.op("ICONST_2"); m.op("IRETURN")
    m.label("chk3")
    m.op("LDC_W", "confirmTranslation"); m.op("ALOAD", 1); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "ret0_b")
    m.op("LDC_W", "(Ljava/util/Locale;Ljava/lang/String;Z)V"); m.op("ALOAD", 2); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "ret0_b")
    m.op("ICONST_3"); m.op("IRETURN")
    m.label("ret0_b"); m.op("ICONST_0"); m.op("IRETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "writeStubCode", "(Ljava/io/DataOutputStream;II)V")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ILOAD", 2); m.op("ICONST_4"); m.op("IF_ICMPNE", "void_stub")
    m.op("ALOAD", 0); m.op("BIPUSH", 14); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_1"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_2"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_2"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_4"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 0); m.op("SIPUSH", 0xAC); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("GOTO", "tail")
    m.label("void_stub")
    m.op("ICONST_0"); m.op("ISTORE", 3)
    m.op("ILOAD", 2); m.op("ICONST_2"); m.op("IF_ICMPNE", "chk_k3")
    m.op("ICONST_4"); m.op("ISTORE", 3); m.op("GOTO", "emit_void")
    m.label("chk_k3")
    m.op("ILOAD", 2); m.op("ICONST_3"); m.op("IF_ICMPNE", "emit_void")
    m.op("ICONST_3"); m.op("ISTORE", 3)
    m.label("emit_void")
    m.op("ALOAD", 0); m.op("BIPUSH", 13); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_0"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 3); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_1"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")
    m.op("ALOAD", 0); m.op("SIPUSH", 0xB1); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.label("tail")
    m.op("ALOAD", 0); m.op("ICONST_0"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ICONST_0"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("RETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "patchTranslationMethods", "([BZ)[B")
    m.op("ALOAD", 0); m.op("ARRAYLENGTH"); m.op("BIPUSH", 16); m.op("IF_ICMPLT", "ret_null_early")
    m.op("ALOAD", 0); m.op("ICONST_0"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I")
    m.op("LDC_INT", -889275714); m.op("IF_ICMPEQ", "magic_ok")
    m.label("ret_null_early"); m.op("ACONST_NULL"); m.op("ARETURN")
    m.label("magic_ok")
    m.op("ALOAD", 0); m.op("BIPUSH", 8); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 2)
    m.op("ILOAD", 2); m.op("ANEWARRAY", "java/lang/String"); m.op("ASTORE", 3)
    m.op("BIPUSH", 10); m.op("ISTORE", 4); m.op("ICONST_1"); m.op("ISTORE", 5)
    m.label("cp_loop"); m.op("ILOAD", 5); m.op("ILOAD", 2); m.op("IF_ICMPGE", "cp_done")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND"); m.op("ISTORE", 6)
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("INVOKESTATIC", THIS, "cpEntrySize", "([BI)I"); m.op("ISTORE", 7)
    m.op("ILOAD", 6); m.op("ICONST_1"); m.op("IF_ICMPNE", "cp_not_utf8")
    m.op("ALOAD", 3); m.op("ILOAD", 5); m.op("NEW", "java/lang/String"); m.op("DUP")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("ICONST_3"); m.op("IADD"); m.op("ILOAD", 7); m.op("ICONST_3"); m.op("ISUB")
    m.op("GETSTATIC", "java/nio/charset/StandardCharsets", "UTF_8", "Ljava/nio/charset/Charset;")
    m.op("INVOKESPECIAL", "java/lang/String", "<init>", "([BIILjava/nio/charset/Charset;)V"); m.op("AASTORE")
    m.label("cp_not_utf8")
    m.op("ILOAD", 4); m.op("ILOAD", 7); m.op("IADD"); m.op("ISTORE", 4)
    m.op("ILOAD", 6); m.op("ICONST_5"); m.op("IF_ICMPEQ", "cp_wide")
    m.op("ILOAD", 6); m.op("BIPUSH", 6); m.op("IF_ICMPNE", "cp_next")
    m.label("cp_wide"); m.op("IINC", 5, 1)
    m.label("cp_next"); m.op("IINC", 5, 1); m.op("GOTO", "cp_loop")
    m.label("cp_done")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("BIPUSH", 8); m.op("IADD")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("BIPUSH", 6); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I")
    m.op("ICONST_2"); m.op("IMUL"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "skipMembers", "([BI)I"); m.op("ISTORE", 4)
    m.op("NEW", "java/io/ByteArrayOutputStream"); m.op("DUP"); m.op("ALOAD", 0); m.op("ARRAYLENGTH")
    m.op("INVOKESPECIAL", "java/io/ByteArrayOutputStream", "<init>", "(I)V"); m.op("ASTORE", 8)
    m.op("NEW", "java/io/DataOutputStream"); m.op("DUP"); m.op("ALOAD", 8)
    m.op("INVOKESPECIAL", "java/io/DataOutputStream", "<init>", "(Ljava/io/OutputStream;)V"); m.op("ASTORE", 9)
    m.op("ALOAD", 9); m.op("ALOAD", 0); m.op("ICONST_0"); m.op("ILOAD", 4); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 10)
    m.op("ALOAD", 9); m.op("ILOAD", 10); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("IINC", 4, 2); m.op("ICONST_0"); m.op("ISTORE", 11); m.op("ICONST_0"); m.op("ISTORE", 12)
    m.label("m_loop"); m.op("ILOAD", 12); m.op("ILOAD", 10); m.op("IF_ICMPGE", "m_done")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 13)
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("ICONST_4"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 14)
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("BIPUSH", 6); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 15)
    m.op("ALOAD", 9); m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("BIPUSH", 8); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("IINC", 4, 8)
    m.op("ILOAD", 1); m.op("ALOAD", 3); m.op("ILOAD", 13); m.op("AALOAD"); m.op("ALOAD", 3); m.op("ILOAD", 14); m.op("AALOAD")
    m.op("INVOKESTATIC", THIS, "matchTranslationMethod", "(ZLjava/lang/String;Ljava/lang/String;)I"); m.op("ISTORE", 16)
    m.op("ICONST_0"); m.op("ISTORE", 17)
    m.label("a_loop"); m.op("ILOAD", 17); m.op("ILOAD", 15); m.op("IF_ICMPGE", "a_done")
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 18)
    m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I"); m.op("ISTORE", 19)
    m.op("ILOAD", 16); m.op("IFEQ", "copy_attr")
    m.op("LDC_W", "Code"); m.op("ALOAD", 3); m.op("ILOAD", 18); m.op("AALOAD")
    m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "copy_attr")
    m.op("ALOAD", 9); m.op("ILOAD", 18); m.op("ILOAD", 16); m.op("INVOKESTATIC", THIS, "writeStubCode", "(Ljava/io/DataOutputStream;II)V")
    m.op("IINC", 11, 1); m.op("GOTO", "adv_attr")
    m.label("copy_attr")
    m.op("ALOAD", 9); m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("BIPUSH", 6); m.op("ILOAD", 19); m.op("IADD")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.label("adv_attr")
    m.op("ILOAD", 4); m.op("BIPUSH", 6); m.op("IADD"); m.op("ILOAD", 19); m.op("IADD"); m.op("ISTORE", 4)
    m.op("IINC", 17, 1); m.op("GOTO", "a_loop")
    m.label("a_done"); m.op("IINC", 12, 1); m.op("GOTO", "m_loop")
    m.label("m_done")
    m.op("ALOAD", 9); m.op("ALOAD", 0); m.op("ILOAD", 4); m.op("ALOAD", 0); m.op("ARRAYLENGTH"); m.op("ILOAD", 4); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ILOAD", 11); m.op("IFNE", "log_ret"); m.op("ACONST_NULL"); m.op("ARETURN")
    m.label("log_ret")
    m.op("GETSTATIC", "java/lang/System", "err", "Ljava/io/PrintStream;")
    m.op("LDC_W", "[ibkr-webchannel-fix] Suppressed zh_CN startup translation confirmation popup")
    m.op("INVOKEVIRTUAL", "java/io/PrintStream", "println", "(Ljava/lang/String;)V")
    m.op("ALOAD", 8); m.op("INVOKEVIRTUAL", "java/io/ByteArrayOutputStream", "toByteArray", "()[B"); m.op("ARETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "shiftStackMapTable", "([BII)[B")
    m.op("ILOAD", 1); m.op("IFLT", "empty_smt")
    m.op("ILOAD", 2); m.op("ICONST_2"); m.op("IF_ICMPLT", "empty_smt")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 3)
    m.op("ILOAD", 3); m.op("IFNE", "non_empty")
    m.label("empty_smt")
    m.op("ICONST_4"); m.op("NEWARRAY", "B"); m.op("DUP")
    m.op("ICONST_1"); m.op("ICONST_2"); m.op("BASTORE"); m.op("DUP")
    m.op("ICONST_2"); m.op("BIPUSH", 21); m.op("BASTORE"); m.op("DUP")
    m.op("ICONST_3"); m.op("BIPUSH", 6); m.op("BASTORE"); m.op("ARETURN")
    m.label("non_empty")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_2"); m.op("IADD"); m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND"); m.op("ISTORE", 4)
    m.op("NEW", "java/io/ByteArrayOutputStream"); m.op("DUP"); m.op("ILOAD", 2); m.op("ICONST_4"); m.op("IADD")
    m.op("INVOKESPECIAL", "java/io/ByteArrayOutputStream", "<init>", "(I)V"); m.op("ASTORE", 5)
    m.op("NEW", "java/io/DataOutputStream"); m.op("DUP"); m.op("ALOAD", 5)
    m.op("INVOKESPECIAL", "java/io/DataOutputStream", "<init>", "(Ljava/io/OutputStream;)V"); m.op("ASTORE", 6)
    m.op("ILOAD", 4); m.op("IFNE", "not_ft0")
    m.op("ALOAD", 6); m.op("ILOAD", 3); m.op("ICONST_1"); m.op("IADD"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 21); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 6); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_3"); m.op("IADD"); m.op("ILOAD", 2); m.op("ICONST_3"); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V"); m.op("GOTO", "smt_ret")
    m.label("not_ft0")
    m.op("ILOAD", 4); m.op("BIPUSH", 64); m.op("IF_ICMPNE", "not_ft64")
    m.op("ALOAD", 6); m.op("ILOAD", 3); m.op("ICONST_1"); m.op("IADD"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 21); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 70); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_3"); m.op("IADD"); m.op("ILOAD", 2); m.op("ICONST_3"); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V"); m.op("GOTO", "smt_ret")
    m.label("not_ft64")
    m.op("ILOAD", 4); m.op("BIPUSH", 63); m.op("IF_ICMPLE", "short_frame")
    m.op("ILOAD", 4); m.op("BIPUSH", 65); m.op("IF_ICMPLT", "ext_frame")
    m.op("ILOAD", 4); m.op("BIPUSH", 127); m.op("IF_ICMPGT", "ext_frame")
    m.label("short_frame")
    m.op("ALOAD", 6); m.op("ILOAD", 3); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 21); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 6); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ILOAD", 4); m.op("ICONST_1"); m.op("ISUB"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_3"); m.op("IADD"); m.op("ILOAD", 2); m.op("ICONST_3"); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V"); m.op("GOTO", "smt_ret")
    m.label("ext_frame")
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_3"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 7)
    m.op("ILOAD", 7); m.op("IFNE", "ext_d_pos")
    m.op("ALOAD", 6); m.op("ILOAD", 3); m.op("ICONST_1"); m.op("IADD"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 21); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ILOAD", 4); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 6); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_5"); m.op("IADD"); m.op("ILOAD", 2); m.op("ICONST_5"); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V"); m.op("GOTO", "smt_ret")
    m.label("ext_d_pos")
    m.op("ALOAD", 6); m.op("ILOAD", 3); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 21); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("BIPUSH", 6); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ILOAD", 4); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 6); m.op("ILOAD", 7); m.op("ICONST_1"); m.op("ISUB"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 6); m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("ICONST_5"); m.op("IADD"); m.op("ILOAD", 2); m.op("ICONST_5"); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.label("smt_ret")
    m.op("ALOAD", 5); m.op("INVOKEVIRTUAL", "java/io/ByteArrayOutputStream", "toByteArray", "()[B"); m.op("ARETURN")
    methods.append(m.assemble())

    m = MethodBuilder(
        cp, THIS, 0x000A, "writePatchedSuspendCode",
        "(Ljava/io/DataOutputStream;I[BIIIIII[Ljava/lang/String;)V"
    )
    m.op("ALOAD", 2); m.op("ILOAD", 3); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 10)
    m.op("ALOAD", 2); m.op("ILOAD", 3); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 11)
    m.op("ALOAD", 2); m.op("ILOAD", 3); m.op("ICONST_4"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I"); m.op("ISTORE", 12)
    m.op("ILOAD", 3); m.op("BIPUSH", 8); m.op("IADD"); m.op("ISTORE", 13)
    m.op("ILOAD", 13); m.op("ILOAD", 12); m.op("IADD"); m.op("ISTORE", 14)
    m.op("ALOAD", 2); m.op("ILOAD", 14); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 15)
    m.op("ILOAD", 14); m.op("ICONST_2"); m.op("IADD"); m.op("ISTORE", 16)
    m.op("ILOAD", 16); m.op("BIPUSH", 8); m.op("ILOAD", 15); m.op("IMUL"); m.op("IADD"); m.op("ISTORE", 14)
    m.op("ALOAD", 2); m.op("ILOAD", 14); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 17)
    m.op("IINC", 14, 2); m.op("ICONST_M1"); m.op("ISTORE", 18); m.op("ICONST_0"); m.op("ISTORE", 19); m.op("ICONST_0"); m.op("ISTORE", 20)
    m.label("ca_loop"); m.op("ILOAD", 20); m.op("ILOAD", 17); m.op("IF_ICMPGE", "ca_done")
    m.op("ALOAD", 2); m.op("ILOAD", 14); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 21)
    m.op("ALOAD", 2); m.op("ILOAD", 14); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I"); m.op("ISTORE", 22)
    m.op("LDC_W", "StackMapTable"); m.op("ALOAD", 9); m.op("ILOAD", 21); m.op("AALOAD")
    m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "ca_next")
    m.op("ILOAD", 14); m.op("BIPUSH", 6); m.op("IADD"); m.op("ISTORE", 18); m.op("ILOAD", 22); m.op("ISTORE", 19)
    m.label("ca_next")
    m.op("ILOAD", 14); m.op("BIPUSH", 6); m.op("IADD"); m.op("ILOAD", 22); m.op("IADD"); m.op("ISTORE", 14)
    m.op("IINC", 20, 1); m.op("GOTO", "ca_loop")
    m.label("ca_done")
    m.op("ALOAD", 2); m.op("ILOAD", 18); m.op("ILOAD", 19); m.op("INVOKESTATIC", THIS, "shiftStackMapTable", "([BII)[B"); m.op("ASTORE", 23)
    m.op("ILOAD", 10); m.op("ICONST_2"); m.op("IF_ICMPGE", "ms_ok"); m.op("ICONST_2"); m.op("ISTORE", 10)
    m.label("ms_ok")
    m.op("BIPUSH", 28); m.op("ILOAD", 12); m.op("IADD"); m.op("ISTORE", 24)
    m.op("BIPUSH", 18); m.op("ILOAD", 24); m.op("IADD"); m.op("BIPUSH", 8); m.op("ILOAD", 15); m.op("IMUL"); m.op("IADD")
    m.op("ALOAD", 23); m.op("ARRAYLENGTH"); m.op("IADD"); m.op("ISTORE", 25)
    m.op("ALOAD", 0); m.op("ILOAD", 1); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 25); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 10); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 11); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 24); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")

    def emit_b(val):
        m.op("ALOAD", 0); m.op("SIPUSH", val); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    def emit_s_const(val):
        m.op("ALOAD", 0); m.op("SIPUSH", val); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    def emit_s_loc(slot):
        m.op("ALOAD", 0); m.op("ILOAD", slot); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")

    emit_b(0x1B); emit_b(0x99); emit_s_const(0x0014); emit_b(0x2A); emit_b(0xB6); emit_s_loc(4)
    emit_b(0x9A); emit_s_const(0x000D); emit_b(0x2A); emit_b(0x13); emit_s_loc(6); emit_b(0xB6); emit_s_loc(5)
    emit_b(0xA7); emit_s_const(0x000A); emit_b(0x2A); emit_b(0x13); emit_s_loc(7); emit_b(0xB6); emit_s_loc(5)

    m.op("ALOAD", 0); m.op("ALOAD", 2); m.op("ILOAD", 13); m.op("ILOAD", 12); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ALOAD", 0); m.op("ILOAD", 15); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ICONST_0"); m.op("ISTORE", 20)
    m.label("ex_loop"); m.op("ILOAD", 20); m.op("ILOAD", 15); m.op("IF_ICMPGE", "ex_done")
    m.op("ILOAD", 16); m.op("BIPUSH", 8); m.op("ILOAD", 20); m.op("IMUL"); m.op("IADD"); m.op("ISTORE", 26)
    for add28, off_add in ((True, 0), (True, 2), (True, 4), (False, 6)):
        m.op("ALOAD", 0); m.op("ALOAD", 2); m.op("ILOAD", 26)
        if off_add: m.op("BIPUSH", off_add); m.op("IADD")
        m.op("INVOKESTATIC", THIS, "u2", "([BI)I")
        if add28: m.op("BIPUSH", 28); m.op("IADD")
        m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("IINC", 20, 1); m.op("GOTO", "ex_loop")
    m.label("ex_done")
    m.op("ALOAD", 0); m.op("ICONST_1"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ILOAD", 8); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 0); m.op("ALOAD", 23); m.op("ARRAYLENGTH"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeInt", "(I)V")
    m.op("ALOAD", 0); m.op("ALOAD", 23); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([B)V")
    m.op("RETURN")
    methods.append(m.assemble())

    m = MethodBuilder(cp, THIS, 0x000A, "patchWebApp", "([B)[B")
    m.op("ALOAD", 0); m.op("ARRAYLENGTH"); m.op("BIPUSH", 16); m.op("IF_ICMPLT", "ret_null_0")
    m.op("ALOAD", 0); m.op("ICONST_0"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I")
    m.op("LDC_INT", -889275714); m.op("IF_ICMPEQ", "magic_ok")
    m.label("ret_null_0"); m.op("ACONST_NULL"); m.op("ARETURN")
    m.label("magic_ok")
    m.op("ALOAD", 0); m.op("BIPUSH", 8); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 1)
    m.op("ILOAD", 1); m.op("NEWARRAY", "I"); m.op("ASTORE", 2)
    m.op("ILOAD", 1); m.op("NEWARRAY", "I"); m.op("ASTORE", 3)
    m.op("ILOAD", 1); m.op("NEWARRAY", "I"); m.op("ASTORE", 4)
    m.op("ILOAD", 1); m.op("ANEWARRAY", "java/lang/String"); m.op("ASTORE", 5)
    m.op("BIPUSH", 10); m.op("ISTORE", 6); m.op("ICONST_1"); m.op("ISTORE", 7)
    m.label("cp_parse"); m.op("ILOAD", 7); m.op("ILOAD", 1); m.op("IF_ICMPGE", "cp_parsed")
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("BALOAD"); m.op("SIPUSH", 0xFF); m.op("IAND"); m.op("ISTORE", 8)
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("INVOKESTATIC", THIS, "cpEntrySize", "([BI)I"); m.op("ISTORE", 9)
    m.op("ALOAD", 2); m.op("ILOAD", 7); m.op("ILOAD", 8); m.op("IASTORE")
    m.op("ALOAD", 3); m.op("ILOAD", 7); m.op("ILOAD", 6); m.op("IASTORE")
    m.op("ALOAD", 4); m.op("ILOAD", 7); m.op("ILOAD", 9); m.op("IASTORE")
    m.op("ILOAD", 8); m.op("ICONST_1"); m.op("IF_ICMPNE", "not_u8")
    m.op("ALOAD", 5); m.op("ILOAD", 7); m.op("NEW", "java/lang/String"); m.op("DUP")
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("ICONST_3"); m.op("IADD"); m.op("ILOAD", 9); m.op("ICONST_3"); m.op("ISUB")
    m.op("GETSTATIC", "java/nio/charset/StandardCharsets", "UTF_8", "Ljava/nio/charset/Charset;")
    m.op("INVOKESPECIAL", "java/lang/String", "<init>", "([BIILjava/nio/charset/Charset;)V"); m.op("AASTORE")
    m.label("not_u8")
    m.op("ILOAD", 6); m.op("ILOAD", 9); m.op("IADD"); m.op("ISTORE", 6)
    m.op("ILOAD", 8); m.op("ICONST_5"); m.op("IF_ICMPEQ", "wide1")
    m.op("ILOAD", 8); m.op("BIPUSH", 6); m.op("IF_ICMPNE", "next1")
    m.label("wide1"); m.op("IINC", 7, 1)
    m.label("next1"); m.op("IINC", 7, 1); m.op("GOTO", "cp_parse")
    m.label("cp_parsed")
    m.op("ILOAD", 6); m.op("ISTORE", 10)
    m.op("ALOAD", 0); m.op("ILOAD", 10); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 11)
    m.op("ICONST_0"); m.op("ISTORE", 12); m.op("ICONST_0"); m.op("ISTORE", 13); m.op("ICONST_0"); m.op("ISTORE", 14)
    m.op("ICONST_1"); m.op("ISTORE", 7)
    m.label("scan_cp"); m.op("ILOAD", 7); m.op("ILOAD", 1); m.op("IF_ICMPGE", "scan_done")
    m.op("ALOAD", 2); m.op("ILOAD", 7); m.op("IALOAD"); m.op("ICONST_1"); m.op("IF_ICMPNE", "chk_mref")
    m.op("LDC_W", "StackMapTable"); m.op("ALOAD", 5); m.op("ILOAD", 7); m.op("AALOAD")
    m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "scan_next")
    m.op("ILOAD", 7); m.op("ISTORE", 14); m.op("GOTO", "scan_next")
    m.label("chk_mref")
    m.op("ALOAD", 2); m.op("ILOAD", 7); m.op("IALOAD"); m.op("BIPUSH", 10); m.op("IF_ICMPNE", "scan_next")
    m.op("ALOAD", 0); m.op("ALOAD", 3); m.op("ILOAD", 7); m.op("IALOAD"); m.op("ICONST_1"); m.op("IADD")
    m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ILOAD", 11); m.op("IF_ICMPNE", "scan_next")
    m.op("ALOAD", 0); m.op("ALOAD", 3); m.op("ILOAD", 7); m.op("IALOAD"); m.op("ICONST_3"); m.op("IADD")
    m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 15)
    m.op("ALOAD", 5); m.op("ALOAD", 0); m.op("ALOAD", 3); m.op("ILOAD", 15); m.op("IALOAD"); m.op("ICONST_1"); m.op("IADD")
    m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("AALOAD"); m.op("ASTORE", 16)
    m.op("ALOAD", 5); m.op("ALOAD", 0); m.op("ALOAD", 3); m.op("ILOAD", 15); m.op("IALOAD"); m.op("ICONST_3"); m.op("IADD")
    m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("AALOAD"); m.op("ASTORE", 17)
    m.op("LDC_W", "isVisible"); m.op("ALOAD", 16); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk_exec")
    m.op("LDC_W", "()Z"); m.op("ALOAD", 17); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "chk_exec")
    m.op("ILOAD", 7); m.op("ISTORE", 12); m.op("GOTO", "scan_next")
    m.label("chk_exec")
    m.op("LDC_W", "executeScript"); m.op("ALOAD", 16); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "scan_next")
    m.op("LDC_W", "(Ljava/lang/String;)V"); m.op("ALOAD", 17); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "scan_next")
    m.op("ILOAD", 7); m.op("ISTORE", 13)
    m.label("scan_next"); m.op("IINC", 7, 1); m.op("GOTO", "scan_cp")
    m.label("scan_done")
    m.op("ICONST_0"); m.op("ISTORE", 18)
    m.op("ILOAD", 12); m.op("IFEQ", "no_susp"); m.op("ILOAD", 13); m.op("IFEQ", "no_susp"); m.op("ILOAD", 14); m.op("IFEQ", "no_susp")
    m.op("ICONST_1"); m.op("ISTORE", 18)
    m.label("no_susp")
    m.op("NEW", "java/io/ByteArrayOutputStream"); m.op("DUP"); m.op("ALOAD", 0); m.op("ARRAYLENGTH"); m.op("SIPUSH", 4096); m.op("IADD")
    m.op("INVOKESPECIAL", "java/io/ByteArrayOutputStream", "<init>", "(I)V"); m.op("ASTORE", 19)
    m.op("NEW", "java/io/DataOutputStream"); m.op("DUP"); m.op("ALOAD", 19)
    m.op("INVOKESPECIAL", "java/io/DataOutputStream", "<init>", "(Ljava/io/OutputStream;)V"); m.op("ASTORE", 20)
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ICONST_0"); m.op("BIPUSH", 8); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ALOAD", 20); m.op("ILOAD", 1); m.op("ILOAD", 18); m.op("IFEQ", "no_add4"); m.op("ICONST_4"); m.op("IADD")
    m.label("no_add4"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    for slot, s_const in ((21, BAD_OPT_JS), (22, FIX_OPT_JS), (23, WHEEL_PREFIX), (24, USER_SCRIPT_JS)):
        m.op("LDC_W", s_const); m.op("GETSTATIC", "java/nio/charset/StandardCharsets", "UTF_8", "Ljava/nio/charset/Charset;")
        m.op("INVOKEVIRTUAL", "java/lang/String", "getBytes", "(Ljava/nio/charset/Charset;)[B"); m.op("ASTORE", slot)
    m.op("ICONST_0"); m.op("ISTORE", 25); m.op("ICONST_1"); m.op("ISTORE", 7)
    m.label("emit_cp"); m.op("ILOAD", 7); m.op("ILOAD", 1); m.op("IF_ICMPGE", "emit_cp_done")
    m.op("ALOAD", 2); m.op("ILOAD", 7); m.op("IALOAD"); m.op("ISTORE", 8)
    m.op("ALOAD", 3); m.op("ILOAD", 7); m.op("IALOAD"); m.op("ISTORE", 6)
    m.op("ALOAD", 4); m.op("ILOAD", 7); m.op("IALOAD"); m.op("ISTORE", 9)
    m.op("ILOAD", 8); m.op("ICONST_1"); m.op("IF_ICMPNE", "cp_verbatim")
    m.op("ILOAD", 6); m.op("ICONST_3"); m.op("IADD"); m.op("ISTORE", 26)
    m.op("ILOAD", 9); m.op("ICONST_3"); m.op("ISUB"); m.op("ISTORE", 27)
    m.op("ALOAD", 0); m.op("ILOAD", 26); m.op("ILOAD", 27); m.op("ALOAD", 21); m.op("INVOKESTATIC", THIS, "indexOfBytes", "([BII[B)I"); m.op("ISTORE", 28)
    m.op("ILOAD", 28); m.op("IFLT", "chk_wheel")
    m.op("ALOAD", 20); m.op("ICONST_1"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeByte", "(I)V")
    m.op("ALOAD", 20); m.op("ILOAD", 27); m.op("ALOAD", 21); m.op("ARRAYLENGTH"); m.op("ISUB"); m.op("ALOAD", 22); m.op("ARRAYLENGTH"); m.op("IADD")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 26); m.op("ILOAD", 28); m.op("ILOAD", 26); m.op("ISUB"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ALOAD", 20); m.op("ALOAD", 22); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([B)V")
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 28); m.op("ALOAD", 21); m.op("ARRAYLENGTH"); m.op("IADD")
    m.op("ILOAD", 26); m.op("ILOAD", 27); m.op("IADD"); m.op("ILOAD", 28); m.op("ALOAD", 21); m.op("ARRAYLENGTH"); m.op("IADD"); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V"); m.op("IINC", 25, 1); m.op("GOTO", "cp_step")
    m.label("chk_wheel")
    m.op("ILOAD", 27); m.op("ALOAD", 23); m.op("ARRAYLENGTH"); m.op("IF_ICMPLT", "cp_verbatim")
    m.op("ALOAD", 0); m.op("ILOAD", 26); m.op("ALOAD", 23); m.op("ARRAYLENGTH"); m.op("ALOAD", 23)
    m.op("INVOKESTATIC", THIS, "indexOfBytes", "([BII[B)I"); m.op("ILOAD", 26); m.op("IF_ICMPNE", "cp_verbatim")
    m.op("ALOAD", 20); m.op("ALOAD", 24); m.op("INVOKESTATIC", THIS, "writeUtf8Entry", "(Ljava/io/DataOutputStream;[B)V")
    m.op("IINC", 25, 1); m.op("GOTO", "cp_step")
    m.label("cp_verbatim")
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("ILOAD", 9); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.label("cp_step")
    m.op("ILOAD", 8); m.op("ICONST_5"); m.op("IF_ICMPEQ", "wide2")
    m.op("ILOAD", 8); m.op("BIPUSH", 6); m.op("IF_ICMPNE", "next2")
    m.label("wide2"); m.op("IINC", 7, 1)
    m.label("next2"); m.op("IINC", 7, 1); m.op("GOTO", "emit_cp")
    m.label("emit_cp_done")
    m.op("ILOAD", 18); m.op("IFEQ", "after_extra_cp")
    m.op("ALOAD", 20); m.op("LDC_W", JS_HIDE); m.op("GETSTATIC", "java/nio/charset/StandardCharsets", "UTF_8", "Ljava/nio/charset/Charset;")
    m.op("INVOKEVIRTUAL", "java/lang/String", "getBytes", "(Ljava/nio/charset/Charset;)[B"); m.op("INVOKESTATIC", THIS, "writeUtf8Entry", "(Ljava/io/DataOutputStream;[B)V")
    m.op("ALOAD", 20); m.op("ILOAD", 1); m.op("INVOKESTATIC", THIS, "writeStringEntry", "(Ljava/io/DataOutputStream;I)V")
    m.op("ALOAD", 20); m.op("LDC_W", JS_SHOW); m.op("GETSTATIC", "java/nio/charset/StandardCharsets", "UTF_8", "Ljava/nio/charset/Charset;")
    m.op("INVOKEVIRTUAL", "java/lang/String", "getBytes", "(Ljava/nio/charset/Charset;)[B"); m.op("INVOKESTATIC", THIS, "writeUtf8Entry", "(Ljava/io/DataOutputStream;[B)V")
    m.op("ALOAD", 20); m.op("ILOAD", 1); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "writeStringEntry", "(Ljava/io/DataOutputStream;I)V")
    m.label("after_extra_cp")
    m.op("ALOAD", 0); m.op("ILOAD", 10); m.op("BIPUSH", 8); m.op("IADD")
    m.op("ALOAD", 0); m.op("ILOAD", 10); m.op("BIPUSH", 6); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I")
    m.op("ICONST_2"); m.op("IMUL"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "skipMembers", "([BI)I"); m.op("ISTORE", 6)
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 10); m.op("ILOAD", 6); m.op("ILOAD", 10); m.op("ISUB"); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 29)
    m.op("ALOAD", 20); m.op("ILOAD", 29); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "writeShort", "(I)V")
    m.op("IINC", 6, 2); m.op("ICONST_0"); m.op("ISTORE", 30)
    m.label("wm_loop"); m.op("ILOAD", 30); m.op("ILOAD", 29); m.op("IF_ICMPGE", "wm_done")
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 31)
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("ICONST_4"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 32)
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("BIPUSH", 6); m.op("IADD"); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 33)
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("BIPUSH", 8); m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("IINC", 6, 8); m.op("ICONST_0"); m.op("ISTORE", 34)
    m.op("ILOAD", 18); m.op("IFEQ", "not_susp_m")
    m.op("LDC_W", "sendSuspendResume"); m.op("ALOAD", 5); m.op("ILOAD", 31); m.op("AALOAD"); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "not_susp_m")
    m.op("LDC_W", "(ZLjava/util/Map;)V"); m.op("ALOAD", 5); m.op("ILOAD", 32); m.op("AALOAD"); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "not_susp_m")
    m.op("ICONST_1"); m.op("ISTORE", 34)
    m.label("not_susp_m"); m.op("ICONST_0"); m.op("ISTORE", 35)
    m.label("wa_loop"); m.op("ILOAD", 35); m.op("ILOAD", 33); m.op("IF_ICMPGE", "wa_done")
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("INVOKESTATIC", THIS, "u2", "([BI)I"); m.op("ISTORE", 36)
    m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("ICONST_2"); m.op("IADD"); m.op("INVOKESTATIC", THIS, "i4", "([BI)I"); m.op("ISTORE", 37)
    m.op("ILOAD", 34); m.op("IFEQ", "wa_copy")
    m.op("LDC_W", "Code"); m.op("ALOAD", 5); m.op("ILOAD", 36); m.op("AALOAD"); m.op("INVOKEVIRTUAL", "java/lang/String", "equals", "(Ljava/lang/Object;)Z"); m.op("IFEQ", "wa_copy")
    m.op("ALOAD", 20); m.op("ILOAD", 36); m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("BIPUSH", 6); m.op("IADD")
    m.op("ILOAD", 12); m.op("ILOAD", 13); m.op("ILOAD", 1); m.op("ICONST_1"); m.op("IADD"); m.op("ILOAD", 1); m.op("ICONST_3"); m.op("IADD")
    m.op("ILOAD", 14); m.op("ALOAD", 5)
    m.op("INVOKESTATIC", THIS, "writePatchedSuspendCode", "(Ljava/io/DataOutputStream;I[BIIIIII[Ljava/lang/String;)V")
    m.op("IINC", 25, 1); m.op("GOTO", "wa_adv")
    m.label("wa_copy")
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("BIPUSH", 6); m.op("ILOAD", 37); m.op("IADD")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.label("wa_adv")
    m.op("ILOAD", 6); m.op("BIPUSH", 6); m.op("IADD"); m.op("ILOAD", 37); m.op("IADD"); m.op("ISTORE", 6)
    m.op("IINC", 35, 1); m.op("GOTO", "wa_loop")
    m.label("wa_done"); m.op("IINC", 30, 1); m.op("GOTO", "wm_loop")
    m.label("wm_done")
    m.op("ALOAD", 20); m.op("ALOAD", 0); m.op("ILOAD", 6); m.op("ALOAD", 0); m.op("ARRAYLENGTH"); m.op("ILOAD", 6); m.op("ISUB")
    m.op("INVOKEVIRTUAL", "java/io/DataOutputStream", "write", "([BII)V")
    m.op("ILOAD", 25); m.op("IFNE", "w_ret"); m.op("ACONST_NULL"); m.op("ARETURN")
    m.label("w_ret")
    m.op("GETSTATIC", "java/lang/System", "err", "Ljava/io/PrintStream;")
    m.op("LDC_W", "[ibkr-webchannel-fix] Patched QWebAppEngineView (optionLoop + idleCssRafFix + hiddenTabSuspend)")
    m.op("INVOKEVIRTUAL", "java/io/PrintStream", "println", "(Ljava/lang/String;)V")
    m.op("ALOAD", 19); m.op("INVOKEVIRTUAL", "java/io/ByteArrayOutputStream", "toByteArray", "()[B"); m.op("ARETURN")
    methods.append(m.assemble())

    out = bytearray()
    out.extend(struct.pack(">IHH", 0xCAFEBABE, 0, 61))
    out.extend(cp.serialize())
    out.extend(struct.pack(">HHHHH", 0x0031, this_idx, super_idx, 1, iface_idx))
    out.extend(struct.pack(">H", 0))
    out.extend(struct.pack(">H", len(methods)))
    for mb in methods:
        out.extend(mb)
    out.extend(struct.pack(">H", 0))
    return bytes(out)


def try_compile_with_javac() -> dict:
    javac = shutil.which("javac")
    if not javac:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        res = subprocess.run(
            [javac, "--release", "17", "-d", tmp, JAVA_SRC],
            capture_output=True, text=True
        )
        if res.returncode != 0:
            return {}
        classes = {}
        for root, _, files in os.walk(tmp):
            for fn in sorted(files):
                if fn.endswith(".class"):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, tmp).replace(os.sep, "/")
                    with open(full, "rb") as f:
                        classes[rel] = f.read()
        return classes


def build_jar(out_path: str, force_javac: bool = False, force_python: bool = False) -> str:
    classes = {}
    backend = "python-classfile-v61-assembler"
    if not force_python:
        classes = try_compile_with_javac()
        if classes:
            backend = "javac-release-17"
        elif force_javac:
            raise RuntimeError("javac --release 17 compilation failed")

    if not classes:
        classes = {"ibkr/fix/WebChannelFixAgent.class": assemble_agent_class_python()}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", MANIFEST_BYTES)
        for rel, data in sorted(classes.items()):
            zf.writestr(rel, data)
    return backend


def main():
    ap = argparse.ArgumentParser(description="Build ibkr-webchannel-fix.jar")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output JAR path")
    ap.add_argument("--force-javac", action="store_true", help="Require JDK javac --release 17")
    ap.add_argument("--force-python", action="store_true", help="Force zero-JDK Python ClassFile v61.0 assembler")
    args = ap.parse_args()
    backend = build_jar(args.out, force_javac=args.force_javac, force_python=args.force_python)
    print(f"[build_jar] Built {args.out} ({os.path.getsize(args.out)} bytes) via {backend}")


if __name__ == "__main__":
    main()
