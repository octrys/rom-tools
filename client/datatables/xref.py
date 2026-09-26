"""Code xrefs: where the game's code reads each table column, and in what context.

The UI trace only sees what the screen shows; flags, ids and rules never
reach it. The code does read them, often in methods whose names survived the
obfuscator (Unity messages, properties, UI handlers): `CMapManager.get_IsNotPet`
is a one-line getter of one Map_Data field. This walks every method of the
unpacked image (code.py) and records, for each field of every type a table
row reaches, the methods reading it, the string literals next to the read and
the calls right after it.

Tracking is linear (branches ignored) and per method, by type propagation
over registers and stack slots:

  mov r, [rip+slot]        class K (resolved slot)        -> klass(K)
  mov r, [klass(K)+0xb8]   K's static fields              -> statics(K)
  mov r, [statics(K)+off]  a static field of ref type T   -> obj(T)
  mov r, [obj(K)+off]      a field of ref type T          -> obj(T)
  lea r, [obj(K)+off]      into an embedded struct S      -> vt(S, delta)
  [obj(K)+off], [vt(S)+d]  a READ of that field (recorded)
  call returning a big struct: the stack buffer passed as retbuf (rcx, before
    `this` — il2cpp methods are plain C functions) becomes a vt(S) region, as
    do `out S&` arguments; rax = the buffer
  the MethodInfo* loaded from a slot before a call names the callee, which
    types calls into shared generic code (Dictionary<K,Row>.TryGetValue...)
  virtual row getters: a table object in rdx (call reg) or r8 (the
    VirtualFuncInvoker thunks below the first managed method: rcx=retbuf,
    dx=slot, r8=obj) with a stack buffer in rcx -> that table's row
  il2cpp_object_new(klass(K)) -> obj(K) (closures that capture a row)
  movups copies of a known struct into the stack propagate its type, through
    the unrolled copy loops' incremented pointers
  `mov r64, [x]; shr r64, 32` (MSVC) reads the dword field at x+4

Accesses wider than their field (struct copies) are not reads. Entry state
comes from the signature: `this`, ref-typed params, struct params by pointer.
Many obfuscated methods are decoy copies stuffed with random strings; plaintext
names (method or class) carry the evidence.
"""

from __future__ import annotations

import bisect
import collections
import os
import re
from multiprocessing import Pool

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_OP_REG

from .code import PRIM_SIZE, Image, Method, Model, obfuscated, parse_sig
from .dump import Dump, elem_type

MAX_METHOD_BYTES = 0x8000
STRING_WINDOW = 0x180  # string literals this close to a read are its context
CALL_WINDOW = 0x90  # calls this soon after a read are its context
MAX_READERS = 60  # per field, plaintext names first
MAX_CONTEXT = 8
GETTER_MAX_INSNS = 4  # `mov/movzx eax, [rcx+off]; ret`
SETTER_LOOKAHEAD = 4  # instructions between a read and the Set<Name>(value) call taking it
UNITY_NAMESPACES = ("UnityEngine.", "System.", "TMPro.", "DG.", "Unity.")

FAM: dict[str, str] = {}
for _big, _subs in {
    "rax": "eax ax al ah", "rbx": "ebx bx bl bh", "rcx": "ecx cx cl ch", "rdx": "edx dx dl dh",
    "rsi": "esi si sil", "rdi": "edi di dil", "rbp": "ebp bp bpl", "rsp": "esp sp spl",
    **{f"r{i}": f"r{i}d r{i}w r{i}b" for i in range(8, 16)},
}.items():
    FAM[_big] = _big
    for _s in _subs.split():
        FAM[_s] = _big
for _i in range(32):
    FAM[f"xmm{_i}"] = FAM[f"ymm{_i}"] = f"xmm{_i}"
VOLATILE = {"rax", "rcx", "rdx", "r8", "r9", "r10", "r11"} | {f"xmm{i}" for i in range(6)}
ARGREGS = ["rcx", "rdx", "r8", "r9"]
COPY_MN = {"movups", "movaps", "movdqu", "movdqa", "vmovups", "vmovaps", "vmovdqu", "vmovdqa"}
LOAD_MN = {"mov", "movzx", "movsx", "movsxd"}


class Context:
    """Everything a worker needs; built once, inherited by forked workers."""

    def __init__(self, model: Model, image: Image, dump: Dump):
        self.m = model
        self.img = image
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)
        self.md.detail = True
        # Inflated generic methods, known only through the slots that name them.
        self.minfo: dict[int, Method] = {}
        for slot, v in image.slots.items():
            if v["u"] in (3, 6) and v.get("sig") and (p := parse_sig(v["sig"])):
                meth = Method(v["c"], *p)
                self.minfo[slot] = meth
                if v.get("rva") and int(v["rva"], 16) not in model.methods:
                    model.add_method(int(v["rva"], 16), meth)
        self.starts = sorted(model.methods)
        self.invoker_limit = self.starts[0]  # VirtualFuncInvoker thunks precede managed code
        self.table_row = {meta["tableClass"]: meta["rowType"] for meta in dump.tables.values()}
        self.table_types = self._reachable(dump)

    def _reachable(self, dump: Dump) -> set[str]:
        """Every type a table row reaches through its fields (and list elements)."""
        todo = [meta["rowType"] for meta in dump.tables.values()]
        seen: set[str] = set()
        while todo:
            t = todo.pop()
            if t in seen or t not in self.m.classes:
                continue
            seen.add(t)
            for _, ft, _ in self.m.fields_of(t):
                inner = elem_type(ft) or ft
                if self.m.kind(inner) in ("struct", "class"):
                    todo.append(inner)
        return seen

    def disasm(self, rva: int) -> list:
        i = bisect.bisect_right(self.starts, rva)
        end = min(self.starts[i] if i < len(self.starts) else rva + MAX_METHOD_BYTES, rva + MAX_METHOD_BYTES)
        return list(self.md.disasm(self.img.data[rva:end], rva))


def arg_types(meth: Method) -> list[tuple[str, str]]:
    """(kind, type) by argument position: retbuf, this, params."""
    out = []
    if CTX.m.is_big_struct(meth.ret):
        out.append(("retbuf", meth.ret))
    if not meth.static:
        out.append(("this", meth.cls))
    return out + [("param", p) for p in meth.params]


def entry_state(meth: Method | None) -> dict:
    state = {}
    if not meth:
        return state
    m = CTX.m
    for i, (k, t) in enumerate(arg_types(meth)[:4]):
        base = t.rstrip("&")
        if k == "retbuf" or (m.is_struct(base) and (t.endswith("&") or m.is_big_struct(base))):
            state[ARGREGS[i]] = ("vt", base, 0)
        elif m.is_ref(base) and not t.endswith("&"):
            state[ARGREGS[i]] = ("obj", base)
    return state


def analyse(rva: int):
    """-> (hits, strings, calls, code). hits: (type, off, obj, how, addr, index)."""
    m = CTX.m
    code = CTX.disasm(rva)
    st = entry_state(m.method(rva))
    sp, rbpv = 0, None
    regions: dict[int, str] = {}  # entry-relative stack address -> struct type
    pend: dict[str, int] = {}  # register -> entry-relative stack address it points at
    xmm: dict[str, tuple | None] = {}
    hits, strings, calls = [], [], []

    def stack_addr(mem, ins):
        if not mem.base or mem.index:
            return None
        b = FAM.get(ins.reg_name(mem.base))
        if b == "rsp":
            return sp + mem.disp
        if b == "rbp" and rbpv is not None:
            return rbpv + mem.disp
        if b in pend:
            return pend[b] + mem.disp
        return None

    def region_of(a):
        for s, t in regions.items():
            if s <= a < s + m.size(t):
                return s, t
        return None

    def resolve(mem, ins):
        if not mem.base or mem.index:
            return None
        s = st.get(FAM.get(ins.reg_name(mem.base)))
        if s:
            if s[0] == "obj":
                return ("obj", s[1], mem.disp)
            if s[0] == "vt":
                return ("vt", s[1], mem.disp + s[2])
            if s[0] in ("statics", "klass"):
                return (s[0], s[1], mem.disp)
            return None
        a = stack_addr(mem, ins)
        if a is not None:
            r = region_of(a)
            return ("vt", r[1], a - r[0]) if r else ("stack", None, a)
        return None

    for k, ins in enumerate(code):
        mn, ops = ins.mnemonic, ins.operands
        if mn == "int3":
            break
        # ---- stack pointer bookkeeping
        if mn == "push":
            sp -= 8
        elif mn == "pop":
            sp += 8
        elif mn in ("sub", "add") and ops[0].type == X86_OP_REG and ins.reg_name(ops[0].reg) == "rsp" \
                and ops[1].type == X86_OP_IMM:
            sp += -ops[1].imm if mn == "sub" else ops[1].imm
            continue
        if mn == "lea" and ins.reg_name(ops[0].reg) == "rbp" and ops[1].mem.base \
                and ins.reg_name(ops[1].mem.base) == "rsp":
            rbpv = sp + ops[1].mem.disp
            continue
        if mn == "mov" and ops[0].type == X86_OP_REG and ins.reg_name(ops[0].reg) == "rbp" \
                and ops[1].type == X86_OP_REG and ins.reg_name(ops[1].reg) == "rsp":
            rbpv = sp
            continue
        # ---- field accesses
        for i, op in enumerate(ops):
            if op.type != X86_OP_MEM:
                continue
            if op.mem.base and ins.reg_name(op.mem.base) == "rip":
                v = CTX.img.slots.get(ins.address + ins.size + op.mem.disp)
                if v and v["u"] == 5:
                    strings.append((ins.address, v["s"]))
                continue
            r = resolve(op.mem, ins)
            if not r:
                continue
            size = op.size
            if r[0] in ("obj", "vt") and size == 8 and mn == "mov" and ops[0].type == X86_OP_REG:
                reg = ins.reg_name(ops[0].reg)
                for nxt in code[k + 1:k + 4]:  # `mov r64,[x]; shr r64,32` reads the dword at x+4
                    o = nxt.operands
                    if nxt.mnemonic == "shr" and len(o) == 2 and o[0].type == X86_OP_REG \
                            and nxt.reg_name(o[0].reg) == reg and o[1].type == X86_OP_IMM and o[1].imm == 0x20:
                        r, size = (r[0], r[1], r[2] + 4), 4
                        break
            if r[0] in ("obj", "vt"):
                obj = r[0] == "obj"
                if mn == "lea":
                    how = "lea"
                elif i == 0 and len(ops) > 1 and mn.startswith("mov"):
                    how = "write"
                else:
                    how = "read"
                if how != "lea" and (mn in COPY_MN or size > m.leaf_size(r[1], r[2], obj)):
                    continue  # part of a struct copy
                hits.append((r[1], r[2], obj, how, ins.address, k))
        # ---- calls
        if mn == "call":
            tgt = ops[0].imm if ops[0].type == X86_OP_IMM else None
            # A MethodInfo* loaded from a slot names the exact callee; the code at
            # tgt may be shared by several (folded bodies, shared generics).
            info = next((st[r][1] for r in ("r9", "r8", "rdx", "rcx") if st.get(r, ("",))[0] == "method"), None)
            named = info.full if info else None
            if info is None and tgt is not None:
                info = m.method(tgt)
                named = m.display(tgt) if info else None
            newrax = None
            if info:
                calls.append((ins.address, named))
                for i, (kind, t) in enumerate(arg_types(info)[:4]):
                    base = t.rstrip("&")
                    if (kind == "retbuf" or (t.endswith("&") and m.is_struct(base))) and ARGREGS[i] in pend:
                        regions[pend[ARGREGS[i]]] = base
                        if kind == "retbuf":
                            newrax = ("vt", base, 0)
                if newrax is None and m.is_ref(info.ret):
                    newrax = ("obj", info.ret)
            elif st.get("rcx", ("",))[0] == "klass" and m.is_ref(st["rcx"][1]):
                newrax = ("obj", st["rcx"][1])  # il2cpp_object_new(klass)
            if newrax is None and info is None:
                # virtual row getter: through a VirtualFuncInvoker thunk (obj in r8) or `call reg` (obj in rdx)
                obj = st.get("r8") if tgt is not None and tgt < CTX.invoker_limit else st.get("rdx") if tgt is None else None
                if obj and obj[0] == "obj" and obj[1] in CTX.table_row and "rcx" in pend:
                    row = CTX.table_row[obj[1]]
                    regions[pend["rcx"]] = row
                    newrax = ("vt", row, 0)
                    calls.append((ins.address, f"{obj[1]}::<row getter>"))
            for v in [v for v in st if v in VOLATILE]:
                del st[v]
            for v in [v for v in pend if v in VOLATILE]:
                del pend[v]
            for v in [v for v in xmm if v in VOLATILE]:
                del xmm[v]
            if newrax:
                st["rax"] = newrax
            continue
        if not ops:
            continue
        dst = ops[0]
        # ---- struct copies through xmm registers
        if mn in COPY_MN and len(ops) == 2:
            if dst.type == X86_OP_REG and ops[1].type == X86_OP_MEM:
                x = FAM.get(ins.reg_name(dst.reg))
                r = resolve(ops[1].mem, ins)
                if r and r[0] == "vt":
                    xmm[x] = (r[1], r[2])
                elif r and r[0] == "obj":
                    f = m.field_at(r[1], r[2])
                    xmm[x] = (f[1], r[2] - f[0]) if f and m.is_struct(f[1]) else None
                else:
                    xmm[x] = None
                continue
            if dst.type == X86_OP_MEM and ops[1].type == X86_OP_REG:
                x = xmm.get(FAM.get(ins.reg_name(ops[1].reg)))
                a = stack_addr(dst.mem, ins)
                if x and a is not None and not region_of(a):
                    regions[a - x[1]] = x[0]
                continue
        if dst.type != X86_OP_REG:
            continue
        d = FAM.get(ins.reg_name(dst.reg))
        if d is None or d == "rsp" or d.startswith("xmm") or mn in ("cmp", "test", "push"):
            continue
        new = None
        if mn == "mov" and len(ops) == 2 and ops[1].type == X86_OP_REG:
            src = FAM.get(ins.reg_name(ops[1].reg))
            new = st.get(src)
            if src in pend:
                pend[d] = pend[src]
                if new is None:
                    st.pop(d, None)
                    continue
        elif mn == "mov" and len(ops) == 2 and ops[1].type == X86_OP_MEM:
            mem = ops[1].mem
            if mem.base and ins.reg_name(mem.base) == "rip":
                slot = ins.address + ins.size + mem.disp
                v = CTX.img.slots.get(slot)
                if v and v["u"] == 1:
                    new = ("klass", v["c"])
                elif slot in CTX.minfo:
                    new = ("method", CTX.minfo[slot])
            else:
                r = resolve(mem, ins)
                if r and r[0] == "klass" and r[2] == 0xb8:
                    new = ("statics", r[1])
                elif r and r[0] == "statics":
                    c = m.classes.get(r[1])
                    f = c.statics.get(r[2]) if c else None
                    if f and m.is_ref(f[0]):
                        new = ("obj", f[0])
                elif r and r[0] == "obj":
                    f = m.field_at(r[1], r[2])
                    if f and f[0] == r[2] and m.is_ref(f[1]):
                        new = ("obj", f[1])
                elif r and r[0] == "vt" and r[1] in m.classes:
                    f = m.field_at(r[1], r[2] + 0x10)
                    if f and f[0] == r[2] + 0x10 and m.is_ref(f[1]):
                        new = ("obj", f[1])
        elif mn == "lea":
            mem = ops[1].mem
            r = resolve(mem, ins)
            if r and r[0] == "obj":
                f = m.field_at(r[1], r[2])
                if f and m.is_struct(f[1]):
                    new = ("vt", f[1], r[2] - f[0])
            elif r and r[0] == "vt":
                new = ("vt", r[1], r[2])
            elif r and r[0] == "stack":
                st.pop(d, None)
                pend[d] = r[2]
                continue
            elif r is None and mem.base and not mem.index and FAM.get(ins.reg_name(mem.base)) in pend:
                pend[d] = pend[FAM[ins.reg_name(mem.base)]] + mem.disp
                st.pop(d, None)
                continue
        st.pop(d, None)
        pend.pop(d, None)
        if new:
            st[d] = new
    return hits, strings, calls, code


# ---------------------------------------------------------------- naming patterns

def camel_from_method(name: str) -> str:
    """'get_IsNotPet' -> 'isNotPet', 'SetMapSkill' -> 'mapSkill'."""
    for prefix in ("get_", "Get", "set_", "Set"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
            break
    return name[:1].lower() + name[1:]


def getter_draft(rva: int, code, hits) -> str | None:
    """A plaintext method whose whole body loads this one field and returns it."""
    body = []
    for ins in code:
        body.append(ins)
        if ins.mnemonic == "ret":
            break
    if len(body) > GETTER_MAX_INSNS or body[-1].mnemonic != "ret" or len(hits) != 1:
        return None
    if any(ins.mnemonic not in LOAD_MN | {"ret"} for ins in body):
        return None
    # The linker folds identical bodies across classes: only a name from the
    # class whose `this` was tracked names this field.
    tracked = CTX.m.method(rva).cls
    names = [mt.name for mt in CTX.m.methods.get(rva, [])
             if mt.cls == tracked and not mt.static and not obfuscated(mt.name)
             and not (mt.cls or "").startswith(UNITY_NAMESPACES)]
    return camel_from_method(names[0]) if names else None


def setter_of(code, k: int) -> str | None:
    """The plaintext Set<Name>(one scalar arg) the value read at code[k] goes straight into."""
    ops = code[k].operands
    if not ops or ops[0].type != X86_OP_REG:
        return None
    held = {FAM.get(code[k].reg_name(ops[0].reg))}
    for ins in code[k + 1:k + 1 + SETTER_LOOKAHEAD]:
        o = ins.operands
        if ins.mnemonic == "call":
            if o[0].type != X86_OP_IMM:
                return None
            meth = CTX.m.method(o[0].imm)
            if not meth or obfuscated(meth.name) or not meth.name.startswith("Set") or len(meth.params) != 1 \
                    or (meth.cls or "").startswith(UNITY_NAMESPACES):
                return None
            p = meth.params[0]
            if p not in PRIM_SIZE and p != "System.String" and CTX.m.kind(p) != "enum":
                return None  # SetData(obj) takes the whole object, not this field
            arg = ARGREGS[len(arg_types(meth)) - 1] if len(arg_types(meth)) <= 4 else None
            return meth.full if arg in held else None
        if ins.mnemonic == "mov" and len(o) == 2 and o[0].type == X86_OP_REG:
            src = FAM.get(ins.reg_name(o[1].reg)) if o[1].type == X86_OP_REG else None
            dst = FAM.get(ins.reg_name(o[0].reg))
            if src in held:
                held.add(dst)
            else:
                held.discard(dst)
    return None


# ---------------------------------------------------------------- building

CTX: Context


def _work(rvas: list[int]) -> dict:
    """Readers per (owner type, field) for a batch of methods."""
    m = CTX.m
    out: dict = collections.defaultdict(dict)
    for rva in rvas:
        try:
            hits, strings, calls, code = analyse(rva)
        except Exception:  # noqa: BLE001 — heuristic pass: skip code it can't follow
            continue
        getter = None
        for t, off, obj, how, addr, k in hits:
            pairs = [p for p in m.chain(t, off, obj) if p[0] in CTX.table_types]
            if not pairs:
                continue
            if getter is None:
                getter = getter_draft(rva, code, hits) or ""
            setter = setter_of(code, k) if how == "read" else None
            for owner, fname in pairs:
                r = out[(owner, fname)].get(rva)
                if r is None:
                    r = out[(owner, fname)][rva] = {"how": set(), "at": [], "s": [], "c": [], "getter": "", "setter": ""}
                r["how"].add(how)
                if len(r["at"]) < MAX_CONTEXT:
                    r["at"].append(addr)
                for a, s in strings:
                    if abs(a - addr) < STRING_WINDOW and s not in r["s"] and len(r["s"]) < MAX_CONTEXT:
                        r["s"].append(s)
                for a, c in calls:
                    if 0 < a - addr < CALL_WINDOW and c not in r["c"] and len(r["c"]) < MAX_CONTEXT:
                        r["c"].append(c)
                if (owner, fname) == pairs[-1]:
                    r["getter"] = r["getter"] or getter
                    r["setter"] = r["setter"] or (setter or "")
    return out


def reader_rank(entry: dict) -> tuple:
    """Plaintext method names first, then plaintext classes, then evidence size."""
    cls, _, meth = entry["m"].rpartition("::")
    return (not entry.get("getter"), not entry.get("setter"), obfuscated(meth),
            obfuscated(cls.rsplit(".", 1)[-1]), -len(entry["s"]) - len(entry["c"]))


def build(model: Model, image: Image, dump: Dump, workers: int | None = None) -> dict:
    global CTX
    CTX = Context(model, image, dump)
    rvas = CTX.starts
    workers = workers or os.cpu_count() or 4
    size = max(1, len(rvas) // (workers * 8))
    batches = [rvas[i:i + size] for i in range(0, len(rvas), size)]
    merged: dict = collections.defaultdict(dict)
    with Pool(workers) as pool:  # fork: workers inherit CTX
        for part in pool.imap_unordered(_work, batches):
            for key, readers in part.items():
                merged[key].update(readers)
    fields: dict = collections.defaultdict(dict)
    for (owner, fname), readers in merged.items():
        entries = [{"m": model.display(rva), "rva": hex(rva), "how": sorted(r["how"]),
                    "at": [hex(a) for a in r["at"]], "s": r["s"], "c": r["c"],
                    **({"getter": r["getter"]} if r["getter"] else {}),
                    **({"setter": r["setter"]} if r["setter"] else {})}
                   for rva, r in readers.items()]
        entries.sort(key=reader_rank)
        fields[owner][fname] = {"readers": len(entries), "top": entries[:MAX_READERS]}
    return {"methods": len(rvas), "types": len(fields), "fields": fields}


# ---------------------------------------------------------------- reports

def columns(dump: Dump, model: Model, table: str) -> list[tuple[str, str, str]]:
    """(column path, owner type, field) for a table's row, depth first."""
    out = []

    def walk(t, prefix, seen):
        for _, ft, fname in model.fields_of(t) if t in model.classes else []:
            path = prefix + fname
            out.append((path, t, fname))
            inner = elem_type(ft) or ft
            if model.is_struct(inner) and inner not in seen:
                walk(inner, path + ("[]." if elem_type(ft) else "."), seen | {inner})

    walk(dump.row_type(table), "", frozenset())
    return out


def report(xref: dict, dump: Dump, model: Model, target: str, show_all: bool, limit: int) -> list[str]:
    table, _, sub = target.partition(".")
    if not dump.row_type(table):
        raise SystemExit(f"unknown table: {table}")
    lines = []
    for path, owner, fname in columns(dump, model, table):
        if sub and not (path == sub or path.startswith(sub + ".") or path.startswith(sub + "[]")):
            continue
        info = xref["fields"].get(owner, {}).get(fname)
        if not info:
            lines.append(f"{path}: no reads")
            continue
        top = [r for r in info["top"] if show_all or not obfuscated(r["m"].rpartition("::")[2])]
        lines.append(f"{path}: {info['readers']} methods, {len(top)} shown")
        for r in top[:limit]:
            tag = f" GETTER->{r['getter']}" if r.get("getter") else f" -> {r['setter']}" if r.get("setter") else ""
            lines.append(f"    {r['m']} {r['how']}{tag}")
            if r["s"]:
                lines.append(f"        strings {r['s']}")
            if r["c"]:
                lines.append(f"        calls   {r['c']}")
    return lines


def asm(model: Model, image: Image, dump: Dump, xref: dict, target: str, show_all: bool, limit: int,
        before: int = 10, after: int = 20) -> list[str]:
    """Annotated disassembly around the reads of one column."""
    global CTX
    CTX = Context(model, image, dump)
    table, _, sub = target.partition(".")
    cols = [c for c in columns(dump, model, table) if c[0] == sub]
    if not cols:
        raise SystemExit(f"no column {sub!r} in {table} (give TABLE.COLUMN, e.g. Map_Data.AECKPHOEHHL.GENDBGDEGLL)")
    _, owner, fname = cols[0]
    info = xref["fields"].get(owner, {}).get(fname)
    if not info:
        return [f"{target}: no reads"]
    readers = [r for r in info["top"] if show_all or not obfuscated(r["m"].rpartition("::")[2])][:limit]
    lines = []
    for r in readers:
        rva = int(r["rva"], 16)
        hits, _, _, code = analyse(rva)
        marks = collections.defaultdict(list)
        for t, off, obj, how, addr, _k in hits:
            marks[addr].append(".".join(f for _, f in model.chain(t, off, obj)) + f" [{how}]")
        want = {int(a, 16) for a in r["at"]}
        index = {ins.address: i for i, ins in enumerate(code)}
        shown: set[int] = set()
        lines.append(f"===== {r['m']} @ {r['rva']}")
        for a in sorted(want):
            if a not in index:
                continue
            for j in range(max(0, index[a] - before), min(len(code), index[a] + after)):
                if j in shown:
                    continue
                shown.add(j)
                ins = code[j]
                if ins.mnemonic == "call" and ins.operands[0].type == X86_OP_IMM and ins.operands[0].imm == image.init:
                    continue
                note = ""
                for op in ins.operands:
                    if op.type == X86_OP_MEM and op.mem.base and ins.reg_name(op.mem.base) == "rip":
                        note = image.slot_text(ins.address + ins.size + op.mem.disp)
                if ins.mnemonic == "call" and ins.operands[0].type == X86_OP_IMM and ins.operands[0].imm in model.methods:
                    note = model.display(ins.operands[0].imm)
                mark = ">>" if ins.address in want else "  "
                field_note = " ".join(marks.get(ins.address, []))
                lines.append(f"{mark}{ins.address:x}  {ins.mnemonic} {ins.op_str:44} ; {note} {field_note}".rstrip())
            lines.append("    ...")
    return lines
