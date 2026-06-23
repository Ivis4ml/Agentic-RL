#!/usr/bin/env python3
"""check_src_drift.py — 文档 Src 源码行引用的确定性漂移检查工具。

docs/ 下的 HTML 用多种写法渲染指向子模块源码的可点击链接。子模块跟 main 浮动后，
上游改动会让这些行号漂移。本工具做确定性（无 LLM）扫描、分类与基线比对。

支持的链接写法（dialect）：
  A 标准 h(Src)：const SLIME="vscode://file/…/slime/"; srcUrl(p,l){return SLIME+p…}
                 + h(Src,{label,path:"slime/ray/x.py",line:120})
  B 字符串常量别名：const FA="slime/rollout/x.py" + h(Src,{label,path:FA,line:53})
  C base 拼接：const VS="vscode://file/…/slime/" + href:VS+"examples/x.py:100"
  D 数据驱动节点：const REPO='/…/AReaL' + vscodeHref + 节点 f:'areal/x.py',l:105
  以及内联字面量 href:"vscode://file<绝对路径>:<行>"。

分类（符号审计：label 若为干净标识符且源文件有同名 def/class，比对声明行是否落在定义 ±TOL）：
  confirmed / drift_suspected / undecidable / missing_file / line_out_of_range / no_line

可移植：源码路径用 `git rev-parse --show-toplevel` 解析（锚定脚本所在仓库），不依赖 HTML
里硬编码的本机绝对路径（那只用于推断子模块名），因此云端不同 checkout 路径下同样工作。

用法：
  python3 scripts/check_src_drift.py --report
  python3 scripts/check_src_drift.py --submodules slime verl --report      # 仅引用了这些子模块的 HTML
  python3 scripts/check_src_drift.py --write-baseline scripts/.src-drift-baseline.json
  python3 scripts/check_src_drift.py --baseline scripts/.src-drift-baseline.json --report
  退出码：有基线→有“回归”则 2；无基线→有硬伤则 2；否则 0。
"""
import argparse, glob, json, os, re, subprocess, sys

IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
RE_VSCONST = re.compile(r'const\s+(\w+)\s*=\s*["\']vscode://file([^"\']+)["\']')
RE_STRCONST = re.compile(r'const\s+(\w+)\s*=\s*["\']([^"\']+)["\']')
RE_SRCURL = re.compile(r'function\s+srcUrl\s*\(p,l\)\s*\{return\s+(\w+)\+p')
RE_SRC = re.compile(r'h\(Src,\{([^}]*)\}\)')
RE_INLINE = re.compile(r'href:"vscode://file([^"]+)"')
RE_NODE = re.compile(r'\bf:\s*["\']([^"\']+)["\']\s*,\s*l:\s*(\d+)')
RE_DATAL = re.compile(r'data-f="([^"]+)"\s+data-l="(\d+)"[^>]*>([^<]*)<')
RE_HREF_BASE = re.compile(r"vscode://file['\"]\s*\+\s*(\w+)\s*\+")  # 'vscode://file'+REPO+'/'+f
DEF_TOL = 2
NEEDS_REVIEW = {"drift_suspected", "undecidable", "missing_file", "line_out_of_range"}
HARD = {"missing_file", "line_out_of_range"}


def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, cwd=here)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return os.path.dirname(here)


def known_submodules(root):
    subs = set()
    try:
        with open(os.path.join(root, ".gitmodules")) as f:
            for line in f:
                m = re.match(r'\s*path\s*=\s*(.+)', line)
                if m:
                    subs.add(m.group(1).strip())
    except Exception:
        pass
    return subs or {"AReaL", "slime", "miles", "verl", "ProRL-Agent-Server"}


def _field(obj, key):
    m = re.search(r'(?:^|[,{])\s*' + re.escape(key) + r':"((?:[^"\\]|\\.)*)"', obj)
    return m.group(1) if m else None


def _field_num(obj, key):
    m = re.search(r'(?:^|[,{])\s*' + re.escape(key) + r':(\d+)', obj)
    return int(m.group(1)) if m else None


def _trailing_submodule(userabs, subs):
    """从硬编码本机绝对路径里取子模块名（末尾目录，或路径中首个已知子模块）。"""
    parts = [p for p in userabs.split('/') if p]
    if parts and parts[-1] in subs:
        return parts[-1]
    for p in parts:
        if p in subs:
            return p
    return parts[-1] if parts else None


def _line_count(path):
    try:
        with open(path, 'rb') as f:
            return sum(1 for _ in f)
    except Exception:
        return None


def _candidate_idents(label):
    if not label:
        return []
    out, seen = [], set()
    for tok in IDENT.findall(label):
        if len(tok) >= 3 and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _def_lines(path, ident):
    out = []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for i, l in enumerate(f, 1):
                if re.match(r'\s*(async\s+def|def|class)\s+' + re.escape(ident) + r'\b', l):
                    out.append(i)
    except Exception:
        pass
    return out


def _mkref(kind, label, relpath, line, root, sub, subs):
    abs_ = os.path.join(root, sub, relpath) if (sub in subs and relpath) else None
    return {"kind": kind, "label": label, "path": relpath, "line": line,
            "submodule": sub if sub in subs else None, "abs": abs_}


def _classify(ref):
    """就地填 exists/total/in_range/cls(/hint)。"""
    if ref.get("abs") is None:
        ref.update(exists=None, total=None, in_range=None, cls="unresolved_base")
        return
    # 目录链接（path 以 / 结尾）：存在即合法、无行号。
    if (ref.get("path") or "").endswith("/") or os.path.isdir(ref["abs"]):
        isdir = os.path.isdir(ref["abs"])
        ref.update(exists=isdir, total=None, in_range=True,
                   cls="no_line" if isdir else "missing_file")
        return
    tot = _line_count(ref["abs"])
    ln = ref["line"]
    ref["total"] = tot
    ref["exists"] = tot is not None
    ref["in_range"] = (ln is None) or (ref["exists"] and ln <= tot)
    if not ref["exists"]:
        ref["cls"] = "missing_file"
        return
    if ln is not None and not ref["in_range"]:
        ref["cls"] = "line_out_of_range"
        return
    if ln is None:
        ref["cls"] = "no_line"
        return
    matched, hint = False, None
    for ident in _candidate_idents(ref.get("label")):
        dls = _def_lines(ref["abs"], ident)
        if not dls:
            continue
        if any(abs(ln - d) <= DEF_TOL for d in dls):
            matched = True
            break
        hint = {"ident": ident, "def_at": dls}
    if matched:
        ref["cls"] = "confirmed"
    elif hint:
        ref["cls"] = "drift_suspected"
        ref["hint"] = hint
    else:
        ref["cls"] = "undecidable"


def scan_file(htmlpath, root, subs):
    with open(htmlpath, encoding='utf-8') as f:
        txt = f.read()
    vsconsts = {n: a for n, a in RE_VSCONST.findall(txt)}                    # base 常量（含 vscode://）
    strconsts = {n: v for n, v in RE_STRCONST.findall(txt)
                 if not v.startswith('vscode://') and not v.startswith('/')}  # 相对路径字符串常量（FA/DL）
    absconsts = {n: v for n, v in RE_STRCONST.findall(txt) if v.startswith('/')}  # 绝对路径常量（REPO）
    m = RE_SRCURL.search(txt)
    base_sub = _trailing_submodule(vsconsts[m.group(1)], subs) if (m and m.group(1) in vsconsts) else None

    refs = []
    # A/B: h(Src,{...})，path 为字面量或字符串常量标识符
    for mo in RE_SRC.finditer(txt):
        o = mo.group(1)
        path = _field(o, "path")
        if path is None:
            idm = re.search(r'(?:^|[,{])\s*path:(\w+)\b', o)
            if idm:
                path = strconsts.get(idm.group(1))
        if path is None or base_sub is None:
            continue
        refs.append(_mkref("src", _field(o, "label"), path, _field_num(o, "line"), root, base_sub, subs))
    # C: base 拼接 NAME+"rel(:line)?"（对每个 vscode base 常量）
    for name, userabs in vsconsts.items():
        sub = _trailing_submodule(userabs, subs)
        for mo in re.finditer(r'\b' + re.escape(name) + r'\+["\']([^"\']+)["\']', txt):
            rel = mo.group(1)
            line = None
            lm = re.match(r'(.+?):(\d+)$', rel)
            if lm:
                rel, line = lm.group(1), int(lm.group(2))
            refs.append(_mkref("concat", None, rel, line, root, sub, subs))
    # D: 数据驱动节点 f:'rel',l:NN（仅当存在 vscodeHref + REPO 绝对路径基址）
    hm = RE_HREF_BASE.search(txt)
    repo_sub = _trailing_submodule(absconsts[hm.group(1)], subs) if (hm and hm.group(1) in absconsts) else None
    if repo_sub:
        for mo in RE_NODE.finditer(txt):
            refs.append(_mkref("node", None, mo.group(1), int(mo.group(2)), root, repo_sub, subs))
        # E: 预渲染静态链接 <a class="src" data-f="rel" data-l="NN">可见文本</a>（label 取文本）
        for mo in RE_DATAL.finditer(txt):
            refs.append(_mkref("datal", mo.group(3).strip() or None, mo.group(1), int(mo.group(2)), root, repo_sub, subs))
    # 内联字面量 href:"vscode://file<abs>"
    for mo in RE_INLINE.finditer(txt):
        raw = mo.group(1)
        lm = re.match(r'(.+?):(\d+)$', raw)
        userabs, line = (lm.group(1), int(lm.group(2))) if lm else (raw, None)
        if userabs.endswith('/'):
            continue
        sub = _trailing_submodule(userabs, subs)
        parts = [p for p in userabs.split('/') if p]
        rel = '/'.join(parts[parts.index(sub) + 1:]) if sub in parts else None
        if rel is None:
            continue
        refs.append(_mkref("inline", None, rel, line, root, sub, subs))

    for r in refs:
        _classify(r)
    return {"file": os.path.relpath(htmlpath, root),
            "base_submodule": base_sub,
            "submodules": sorted({r["submodule"] for r in refs if r["submodule"]}),
            "has_vscode": "vscode://file" in txt,
            "n_extracted": len(refs),
            "refs": refs}


def _dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def _load(path):
    with open(path) as f:
        return json.load(f)


def _keyed(scanned):
    """给每条引用一个稳定的出现序号（同 (file,kind,path,label,line) 第几次出现），用于基线比对去歧义。"""
    counter = {}
    out = []
    for s in scanned:
        for r in s["refs"]:
            k = (s["file"], r["kind"], r.get("path"), r.get("label"), r.get("line"))
            occ = counter.get(k, 0)
            counter[k] = occ + 1
            out.append((s, r, k + (occ,)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", nargs="*", help="只查这些 HTML（仓库相对或绝对路径）")
    ap.add_argument("--submodules", nargs="*", help="只查引用了这些子模块的 HTML（事件驱动用）")
    ap.add_argument("--out", help="把 manifest.json/worklist.json/per-file 切片写到该目录")
    ap.add_argument("--report", action="store_true", help="打印人类可读摘要")
    ap.add_argument("--write-baseline", metavar="PATH", help="把当前每条引用的分类写为基线")
    ap.add_argument("--baseline", metavar="PATH", help="与基线比对，只报回归（confirmed→非confirmed）与新增硬伤")
    args = ap.parse_args()

    root = repo_root()
    subs = known_submodules(root)
    if args.files:
        files = [f if os.path.isabs(f) else os.path.join(root, f) for f in args.files]
    else:
        files = sorted(glob.glob(os.path.join(root, "docs", "**", "*.html"), recursive=True))
    scanned = [scan_file(f, root, subs) for f in files]
    if args.submodules:
        want = set(args.submodules)
        scanned = [s for s in scanned if want & set(s["submodules"])]

    counts = {}
    for s in scanned:
        for r in s["refs"]:
            counts[r["cls"]] = counts.get(r["cls"], 0) + 1
    hard = sum(counts.get(k, 0) for k in HARD)
    n_drift = counts.get("drift_suspected", 0)
    n_und = counts.get("undecidable", 0)
    # 诊断：含 vscode 链接却 0 引用 → 可能有未支持的新写法，显式提示而非静默当“干净”。
    silent_gaps = [s["file"] for s in scanned if s["has_vscode"] and s["n_extracted"] == 0]

    keyed = _keyed(scanned)
    if args.write_baseline:
        records = [{"file": s["file"], "kind": r["kind"], "path": r.get("path"),
                    "label": r.get("label"), "line": r.get("line"), "occ": k[-1], "cls": r["cls"]}
                   for (s, r, k) in keyed]
        _dump({"records": records}, args.write_baseline)
        print(f"baseline written: {len(records)} refs -> {args.write_baseline}")

    regressions = []
    if args.baseline:
        bmap = {(x["file"], x["kind"], x["path"], x["label"], x["line"], x["occ"]): x["cls"]
                for x in _load(args.baseline)["records"]}
        for (s, r, k) in keyed:
            old, new = bmap.get(k), r["cls"]
            if (old == "confirmed" and new != "confirmed") or (new in HARD and old not in HARD):
                regressions.append({"file": s["file"], "label": r.get("label"), "path": r.get("path"),
                                    "line": r.get("line"), "abs": r.get("abs"), "kind": r["kind"],
                                    "cls": new, "old": old, "hint": r.get("hint")})

    worklist = []
    if args.baseline:
        byfile = {}
        for g in regressions:
            byfile.setdefault(g["file"], []).append(
                {kk: g[kk] for kk in ("label", "path", "line", "abs", "kind", "cls", "hint")})
        worklist = [{"file": f, "n_unconfirmed": len(v), "refs": v} for f, v in byfile.items()]
    else:
        for s in scanned:
            items = [{"label": r.get("label"), "path": r.get("path"), "line": r.get("line"),
                      "abs": r.get("abs"), "kind": r["kind"], "cls": r["cls"], "hint": r.get("hint")}
                     for r in s["refs"] if r["cls"] in NEEDS_REVIEW]
            if items:
                worklist.append({"file": s["file"], "n_unconfirmed": len(items), "refs": items})
    total_unconfirmed = sum(f["n_unconfirmed"] for f in worklist)

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        _dump({"root": root, "files": scanned}, os.path.join(args.out, "manifest.json"))
        _dump({"root": root, "files": worklist,
               "totals": {"files_with_work": len(worklist), "unconfirmed": total_unconfirmed,
                          "hard": hard, "drift_suspected": n_drift, "undecidable": n_und}},
              os.path.join(args.out, "worklist.json"))
        sl = os.path.join(args.out, "worklist")
        os.makedirs(sl, exist_ok=True)
        for f in worklist:
            _dump(f, os.path.join(sl, re.sub(r'[^A-Za-z0-9]+', '_', f["file"]) + ".json"))

    if args.report or not args.out:
        print(f"root={root}")
        print(f"scanned_files={len(scanned)}  refs={sum(len(s['refs']) for s in scanned)}")
        for k in ("confirmed", "drift_suspected", "undecidable", "no_line",
                  "missing_file", "line_out_of_range", "unresolved_base"):
            if counts.get(k):
                print(f"  {k:18s} {counts[k]}")
        if silent_gaps:
            print(f"  WARN 含 vscode 链接但 0 引用（可能有未支持写法）: {', '.join(silent_gaps)}")
        if args.baseline:
            print(f"baseline_regressions={len(regressions)}")
            for g in regressions:
                print(f"    [{g['old']}->{g['cls']}] {g['file']}  {g['path']}:{g['line']}  label={g['label']}")
        else:
            print(f"files_with_work={len(worklist)}  unconfirmed={total_unconfirmed}  "
                  f"(hard={hard}, drift_suspected={n_drift}, undecidable={n_und})")
            if hard:
                print("  HARD ISSUES:")
                for s in scanned:
                    for r in s["refs"]:
                        if r["cls"] in HARD:
                            print(f"    [{r['cls']}] {s['file']}  {r.get('path')}:{r.get('line')}  label={r.get('label')}")

    sys.exit(2 if (regressions if args.baseline else hard) else 0)


if __name__ == "__main__":
    main()
