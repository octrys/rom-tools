"""python3 -m datatables <command> [ARG ...]  (paths and settings: datatables.toml)

  infer    learn each table's row layout against tables_runtime.json -> layouts.json
  verify   check the saved layouts (and the text tables) against tables_runtime.json
  decode   decode the bundle with the saved layouts -> tables/<Table>.json
  schema   annotated column schema (types, enums, fk, ...) -> schema.json
  uitrace  [TRACE ...] import UI traces, match them against the tables -> evidence.json
  xref     where the game's code reads each column -> xref.json
  xref     TABLE[.COLUMN] ...  report it per column (--asm: annotated disassembly)
  suggest  names.toml: re-key to this build, regenerate suggestions from the evidence
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import time
from pathlib import Path

from . import config as cfg
from . import names, uitrace
from .bundle import TEXT_TABLES, Bundle
from .codec import describe
from .dump import parse as parse_dump
from .infer import check_all, infer_table


def load_json(path: Path, what: str):
    if not path.is_file():
        sys.exit(f"{what} not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value, indent=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=indent), encoding="utf-8")
    tmp.replace(path)


def is_row_table(rt) -> bool:
    return isinstance(rt, dict) and bool(rt.get("rows")) and isinstance(rt["rows"][0]["row"], dict)


def check_text_table(bundle: Bundle, name: str, rt: dict) -> tuple[bool, str]:
    got = bundle.text_table(name)
    bad = [r["key"] for r in rt["rows"] if r["key"] in got and got[r["key"]] != r["row"]]
    extra = sum(r["key"] not in got for r in rt["rows"])
    if bad:
        return False, f"{len(bad)}/{len(rt['rows'])} keys differ, e.g. {bad[:3]}"
    note = f" ({extra} runtime keys not in the bundle)" if extra else ""
    return True, f"text table, {len(got)} keys{note}"


def run_checked(conf: cfg.Config, bundle: Bundle, tables: list[str], infer: bool) -> None:
    """infer / verify: both walk the runtime tables and report per table."""
    dump = parse_dump(conf.dump_cs)
    runtime = load_json(conf.runtime, "runtime dump")
    layouts = load_json(conf.layouts, "layouts") if conf.layouts.is_file() else {}
    if not infer and not layouts:
        sys.exit(f"no layouts yet: run `python3 -m datatables infer` first ({conf.layouts})")
    tables = tables or sorted(runtime)
    ok = 0
    for name in tables:
        rt = runtime.get(name)
        if rt is None:
            print(f"SKIP  {name}: not in the runtime dump")
            continue
        if name in TEXT_TABLES:
            good, msg = check_text_table(bundle, name, rt)
            ok += good
            print(f"{'OK  ' if good else 'FAIL'}  {name}: {msg}")
            continue
        asset = bundle.asset(name)
        idx = bundle.index(asset) if asset else None
        if not is_row_table(rt) or idx is None:
            print(f"SKIP  {name}: {'no rows' if idx is not None else 'no indexed asset'}")
            continue
        data = bundle.assets[asset]
        extra = sum(r["key"] not in idx for r in rt["rows"])
        note = f" ({extra} runtime rows have no binary row)" if extra else ""
        if infer:
            t0 = time.monotonic()
            layout, why = infer_table(dump, dump.row_type(name), data, idx, rt["rows"], bundle.localname, conf.limits)
            took = time.monotonic() - t0
            if layout is None:
                print(f"FAIL  {name}: {why} [{took:.1f}s]")
                continue
            layouts[name] = {"asset": asset, "layout": layout}
            write_json(conf.layouts, layouts)  # after every table: an interrupted run keeps its progress
            print(f"OK    {name}: {len(idx)} rows, {describe(layout)}{note} [{took:.1f}s]")
        else:
            if name not in layouts:
                print(f"SKIP  {name}: no saved layout")
                continue
            bad, why = check_all(layouts[name]["layout"], data, idx, rt["rows"], bundle.localname)
            if bad:
                print(f"FAIL  {name}: key {bad['key']}: {why}")
                continue
            print(f"OK    {name}{note}")
        ok += 1
    print(f"\n{ok}/{len(tables)} tables")


def decode_all(bundle: Bundle, layouts: dict) -> dict[str, dict]:
    """Every row table, raw values ({table: {key: row}})."""
    return {name: bundle.rows(entry["asset"], entry["layout"]) for name, entry in sorted(layouts.items())}


def decode(conf: cfg.Config, bundle: Bundle, tables: list[str]) -> None:
    layouts = load_json(conf.layouts, "layouts (run `python3 -m datatables infer` first)")
    dump = parse_dump(conf.dump_cs) if conf.enum_names else None
    renames = {} if conf.apply_names == "none" else \
        names.confirmed_names(names.load(conf.names), include_suggested=conf.apply_names == "all")
    out_dir = conf.tables
    tables = tables or sorted(layouts) + list(TEXT_TABLES)
    written = 0
    for name in tables:
        text = bundle.text_table(name)
        if text is not None:
            rows = {str(k): v for k, v in text.items()}
        elif name in layouts:
            row_type = dump.row_type(name) if dump else None
            rows = {}
            for k, row in bundle.rows(layouts[name]["asset"], layouts[name]["layout"]).items():
                if row_type:
                    row = dump.name_enums(row, row_type)
                rows[str(k)] = names.rename(row, renames) if renames else row
        else:
            print(f"SKIP  {name}: no saved layout")
            continue
        write_json(out_dir / f"{name}.json", rows, indent=1)
        written += 1
    print(f"{written} tables -> {out_dir} ({len(renames)} identifiers named, {conf.apply_names})")


def import_traces(conf: cfg.Config, files: list[str]) -> None:
    """Copy new trace files into traces/ (named by date + content hash; a file
    already there is skipped)."""
    conf.traces.mkdir(parents=True, exist_ok=True)
    known = {p.stem.rsplit("_", 1)[-1] for p in conf.traces.glob("*.jsonl")}
    for f in map(Path, files):
        if not f.is_file():
            sys.exit(f"trace not found: {f}")
        data = f.read_bytes()
        digest = hashlib.sha1(data).hexdigest()[:10]
        if digest in known:
            print(f"      {f.name}: already imported")
            continue
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(f.stat().st_mtime))
        target = conf.traces / f"{stamp}_{digest}.jsonl"
        target.write_bytes(data)
        known.add(digest)
        print(f"      {f.name} -> {target.name}")


def run_uitrace(conf: cfg.Config, bundle: Bundle, files: list[str]) -> None:
    import_traces(conf, files)
    paths = sorted(conf.traces.glob("*.jsonl"))
    if not paths:
        sys.exit(f"no traces in {conf.traces}: pass rom-frida's storage/ui_trace_*.jsonl files")
    layouts = load_json(conf.layouts, "layouts (run `python3 -m datatables infer` first)")
    evidence = uitrace.Evidence(uitrace.TableIndex(decode_all(bundle, layouts)))
    for p in paths:
        evidence.add(p.name, uitrace.load(p))
    result = evidence.result()
    write_json(conf.evidence, result, indent=1)
    kinds = collections.Counter(c["kind"] for c in result["columns"])
    print(f"{len(paths)} traces, {result['texts']} texts, {result['records']} records, "
          f"{result['states']} state snapshots -> "
          f"{dict(kinds)} findings -> {conf.evidence}")
    print("next: python3 -m datatables suggest")


def run_xref(conf: cfg.Config, targets: list[str], show_asm: bool, show_all: bool, limit: int) -> None:
    from . import xref
    from .code import Image, Model

    dump = parse_dump(conf.dump_cs)
    if not targets:
        for path, what in ((conf.image, "image"), (conf.slots, "slots")):
            if not path.is_file():
                sys.exit(f"{what} not found: {path} (rom-frida: python spawn.py dump_code.js)")
        t0 = time.monotonic()
        result = xref.build(Model(conf.dump_cs), Image(conf.image, conf.slots), dump)
        write_json(conf.xref, result)
        read = sum(len(f) for f in result["fields"].values())
        print(f"{result['methods']} methods -> {read} fields of {result['types']} table types read "
              f"[{time.monotonic() - t0:.0f}s] -> {conf.xref}")
        print("next: python3 -m datatables xref Map_Data  (report), or suggest")
        return
    data = load_json(conf.xref, "xref (run `python3 -m datatables xref` first)")
    model = Model(conf.dump_cs)
    image = Image(conf.image, conf.slots) if show_asm else None
    for target in targets:
        lines = xref.asm(model, image, dump, data, target, show_all, limit) if show_asm \
            else xref.report(data, dump, model, target, show_all, limit)
        print("\n".join(lines))


def run_suggest(conf: cfg.Config, bundle: Bundle) -> None:
    dump = parse_dump(conf.dump_cs)
    layouts = load_json(conf.layouts, "layouts (run `python3 -m datatables infer` first)")
    evidence = load_json(conf.evidence, "evidence") if conf.evidence.is_file() else {}
    if not evidence:
        print(f"no {conf.evidence.name} yet (run uitrace): suggesting from row keys only")
    code = load_json(conf.xref, "xref") if conf.xref.is_file() else {}
    if not code:
        print(f"no {conf.xref.name} yet (run xref): no code evidence")
    entries, stats = names.suggest(dump, names.load(conf.names), evidence, decode_all(bundle, layouts), code)
    names.save(conf.names, entries)
    for old, new in stats["moved"]:
        print(f"re-keyed {old} -> {new} (new build)")
    for key in stats["stale"]:
        print(f"[!] {key} ({entries[key].name}): anchor no longer resolves — check it by hand")
    print(f"{stats['confirmed']} confirmed, {stats['tentative']} tentative, {stats['suggested']} suggested"
          f" -> {conf.names}")
    top = sorted((e for e in entries.values() if e.status == "suggested"), key=lambda e: -e.score)
    for e in top[:25]:
        print(f"  {e.score:7.1f}  {e.name:24} {e.evidence[0][:110] if e.evidence else ''}")


def schema(conf: cfg.Config) -> None:
    from .schema import build

    dump = parse_dump(conf.dump_cs)
    runtime = load_json(conf.runtime, "runtime dump")
    result, stats, missing = build(dump, runtime, names.load(conf.names))
    write_json(conf.schema, result, indent=1)
    print(f"{len(result)} tables ({len(dump.tables)} in dump, {len(runtime)} in runtime) -> {conf.schema}")
    if missing:
        print(f"not in rom_dump (different build?): {', '.join(missing)}")
    print(dict(stats.most_common()))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python3 -m datatables", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["infer", "verify", "decode", "schema", "uitrace", "xref", "suggest"])
    ap.add_argument("args", nargs="*", help="tables to limit infer/verify/decode to (default: all); "
                                            "for uitrace, trace files to import first; "
                                            "for xref, TABLE[.COLUMN] to report (none: build xref.json)")
    ap.add_argument("--config", type=Path, default=cfg.DEFAULT_PATH)
    ap.add_argument("--asm", action="store_true", help="xref: disassembly around the reads of TABLE.COLUMN")
    ap.add_argument("--all", action="store_true", help="xref: include methods with obfuscated names")
    ap.add_argument("--limit", type=int, default=8, help="xref: methods shown per column (default 8)")
    a = ap.parse_args()
    conf = cfg.load(a.config)
    if a.command == "schema":
        schema(conf)
        return
    if a.command == "xref":
        run_xref(conf, a.args, a.asm, a.all, a.limit)
        return
    if not conf.bundle.is_file():
        sys.exit(f"bundle not found: {conf.bundle}")
    try:
        bundle = Bundle(conf.bundle)
    except ValueError as e:
        sys.exit(str(e))
    if a.command == "decode":
        decode(conf, bundle, a.args)
    elif a.command == "uitrace":
        run_uitrace(conf, bundle, a.args)
    elif a.command == "suggest":
        run_suggest(conf, bundle)
    else:
        run_checked(conf, bundle, a.args, infer=a.command == "infer")


if __name__ == "__main__":
    main()
