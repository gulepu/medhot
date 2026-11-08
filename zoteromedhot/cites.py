#!/usr/bin/env python3
"""ZoteroMedHOT 新施用文献监测
反查库内文献（主题收藏 + 库内热榜）近期被谁新引用（OpenAlex），
按「临床价值 / 高分」筛选后导入 Zotero 库（有 API key 走 Web API 自动入库，否则生成 .bib 兜底），
macOS 系统通知提示。状态存 cites_state.json（已导入 DOI 去重）。
"""
import json, re, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import build  # 复用 open_db / THEME_PATTERNS / hot_scores / THEME_COLLECTIONS

HERE = Path(__file__).parent
STATE = HERE / "cites_state.json"
KEYF = HERE / "zotero_api_key.txt"
BIB_DIR = HERE / "new_imports"

ZLIB = 13522303            # zotero.org 账号（itemRelations 里的 users/13522303）
WATCH_HOT = 100            # 热榜参与监测条数
WATERMARK_DEFAULT = 7      # 首次运行回看天数
MAX_IMPORT = 10            # 每日导入上限（适中）
THRESH = 3.0               # 导入阈值
CHUNK = 25                 # 每请求反查多少个 W-id
RESOLVE_CHUNK = 50         # 每请求解析多少个 DOI

# ---- 顶刊名单（期刊名子串匹配，大小写不敏感）----
TOP_JOURNAL_NAMES = [
    "new england journal of medicine", "n engl j med", "the lancet", "lancet",
    "jama", "bmj", "annals of neurology", "jama neurology", "movement disorders",
    "j neurology", "neurol neurosurg psychiatry", "jnnnp",
    "nature medicine", "nature neuroscience", "nature reviews",
    "new England", "science", "cell ",
]
DESIGN_RCT = re.compile(r"\bRCT\b|randomi[sz]ed|double[- ]?blind|clinical trial|随机(对照|双盲)", re.I)
DESIGN_META = re.compile(r"meta-?analysis|systematic review|网络 meta|荟萃|guideline|指南|expert consensus|专家共识", re.I)
DESIGN_COHORT = re.compile(r"\bcohort\b|prospective|注册队列|large[- ]sample|多中心", re.I)

# 主题 → 导入目标收藏（候选文献自身主题匹配到的库内主题收藏）
THEME_TO_COLL = {
    "帕金森": "PD", "运动神经元病": "运动神经元病", "亨廷顿": "恶化及加重",
    "PSP/MSA": "恶化及加重", "威尔逊病": "WD中医研究", "肌张力障碍": "肌张力障碍",
    "癫痫": "癫痫", "吞咽障碍": "吞咽障碍", "震颤": "震颤", "HNPP": "HNPP",
}

def theme_to_collection(text: str):
    for name, p in build.THEME_PATTERNS:
        if p.search(text):
            return THEME_TO_COLL.get(name)
    return None

def oa_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "zoteromedhot/1.0 (daily lit-monitor)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def reconstruct_abstract(inv):
    if not inv: return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs: pos[i] = word
    return " ".join(pos.get(i, "") for i in range(max(pos) + 1))

# ---------------- 监测集：主题收藏 + 热榜 ----------------
def watch_set(db):
    """itemID → DOI（只留带 DOI 的，OpenAlex 只能反查有 DOI 的）"""
    ph = ",".join("?" * len(build.THEME_COLLECTIONS))
    rows = db.execute(f"""SELECT ci.itemID FROM collectionItems ci
        JOIN collections c ON c.collectionID=ci.collectionID
        WHERE c.libraryID=1 AND c.collectionName IN ({ph})""", tuple(build.THEME_COLLECTIONS)).fetchall()
    ids = {r["itemID"] for r in rows}
    hot, _ = build.hot_scores(db, top=WATCH_HOT)
    ids |= {r["itemID"] for _, r in hot}
    ph2 = ",".join("?" * len(ids))
    out = {}
    for r in db.execute(f"""SELECT i.itemID, MAX(CASE WHEN fc.fieldName='DOI' THEN v.value END) doi
        FROM items i LEFT JOIN itemData d ON d.itemID=i.itemID
        LEFT JOIN itemDataValues v ON d.valueID=v.valueID
        LEFT JOIN fieldsCombined fc ON d.fieldID=fc.fieldID
        WHERE i.itemID IN ({ph2}) GROUP BY i.itemID""", tuple(sorted(ids))):
        doi = (r["doi"] or "").strip()
        if doi: out[r["itemID"]] = doi
    return out

def local_dois(db):
    fid = db.execute("SELECT fieldID FROM fields WHERE fieldName='DOI'").fetchone()[0]
    return {r[0].lower() for r in db.execute(
        "SELECT v.value FROM itemData d JOIN itemDataValues v ON d.valueID=v.valueID "
        "JOIN items i ON i.itemID=d.itemID WHERE i.libraryID=1 AND d.fieldID=? AND v.value", (fid,))}

# ---------------- OpenAlex ----------------
def resolve_wids(dois):
    """DOI → OpenAlex W-id（50 个/请求，OR 同字段）"""
    doi2w, pool = {}, list(dict.fromkeys(dois))
    for i in range(0, len(pool), RESOLVE_CHUNK):
        ch = pool[i:i + RESOLVE_CHUNK]
        f = "doi:" + "|".join(ch)
        url = ("https://api.openalex.org/works?filter=" + urllib.parse.quote(f, safe=":|,")
               + "&per-page=50&select=id,doi")
        try:
            data = oa_get(url)
        except Exception as e:
            print(f"[cites] DOI 解析失败（{len(ch)} 个）: {e}", file=sys.stderr); continue
        for w in data.get("results", []):
            d = norm_doi(w.get("doi")).lower()
            if d: doi2w[d] = w["id"]
    return doi2w

def new_citing(wids, since):
    """反查 who cites these works, 发表日期 >= since（25 个 W-id/请求）"""
    out = []
    for i in range(0, len(wids), CHUNK):
        ch = wids[i:i + CHUNK]
        f = "cites:" + "|".join(ch) + f",from_publication_date:{since}"
        url = ("https://api.openalex.org/works?filter=" + urllib.parse.quote(f, safe=":|,")
               + "&per-page=50&sort=publication_date:desc&select=doi,title,publication_date,"
                 "cited_by_count,type,authorships,primary_location,abstract_inverted_index")
        try:
            data = oa_get(url)
        except Exception as e:
            print(f"[cites] 反查失败（{len(ch)} 个）: {e}", file=sys.stderr); continue
        out.extend(data.get("results", []))
    return out

# ---------------- 筛选打分（适中：顶刊或 临床设计+主题 即导入）----------------
def score_candidate(w):
    title = w.get("title") or ""
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    text = title + " " + reconstruct_abstract(w.get("abstract_inverted_index"))
    s, why = 0.0, []
    vl = venue.lower()
    if any(k in vl for k in TOP_JOURNAL_NAMES):
        s += 3.0; why.append(f"顶刊 {venue}")
    if DESIGN_RCT.search(text): s += 1.5; why.append("随机/临床试验")
    if DESIGN_META.search(text): s += 1.5; why.append("Meta/指南级证据")
    if DESIGN_COHORT.search(text): s += 1.0; why.append("队列/多中心")
    cb = w.get("cited_by_count") or 0
    if cb >= 5: s += 1.0; why.append(f"已被引 {cb}")
    th = sum(1 for _, p in build.THEME_PATTERNS if p.search(text))
    if th >= 2: s += 1.5; why.append(f"主题命中 {th}")
    elif th == 1: s += 0.75
    return s, why, venue, title, text

# ---------------- 导入 ----------------
def coll_keys(db):
    """收藏名 → key（重名取条目多的那个）"""
    ph = ",".join("?" * len(build.THEME_COLLECTIONS))
    rows = db.execute(f"""SELECT c.collectionName, c.key, COUNT(ci.itemID) n FROM collections c
        LEFT JOIN collectionItems ci ON ci.collectionID=c.collectionID
        WHERE c.libraryID=1 AND c.collectionName IN ({ph})
        GROUP BY c.collectionID""", tuple(build.THEME_COLLECTIONS)).fetchall()
    best = {}
    for r in rows:
        k = best.get(r["collectionName"])
        if k is None or r["n"] > k[1]: best[r["collectionName"]] = (r["key"], r["n"])
    return {name: v[0] for name, v in best.items()}

def zotero_authors(w):
    auths = []
    for a in (w.get("authorships") or [])[:20]:
        d = (a.get("author") or {}).get("display_name") or ""
        if not d: continue
        parts = d.rsplit(" ", 1)
        auths.append({"lastName": parts[0], "firstName": parts[1]} if len(parts) == 2 else {"name": d})
    return auths

def norm_doi(doi):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip())

def zotero_item(w, coll_name):
    doi = norm_doi(w.get("doi"))
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    it = {
        "itemType": "journalArticle",
        "title": w.get("title") or "",
        "DOI": doi,
        "publicationTitle": venue,
        "date": (w.get("publication_date") or "")[:4],
        "abstractNote": reconstruct_abstract(w.get("abstract_inverted_index")),
        "url": f"https://doi.org/{doi}" if doi else (w.get("id") or ""),
    }
    au = zotero_authors(w)
    if au: it["author"] = au
    if coll_name:
        it["tags"] = [{"name": "ZoteroMedHOT 新施用"}]
    return it

def import_api(items):
    """items: [(zitem, coll_key)]；返回成功导入的条数"""
    key = KEYF.read_text().strip()
    by_coll = {}
    for it, ck in items:
        by_coll.setdefault(ck, []).append(it)
    n = 0
    for ck, its in by_coll.items():
        url = f"https://api.zotero.org/users/{ZLIB}/items" + (f"?top=collections:{ck}" if ck else "")
        req = urllib.request.Request(
            url, data=json.dumps([{"item": x} for x in its]).encode(),
            headers={"Zotero-API-Key": key, "Content-Type": "application/json",
                     "zotero-version": "3", "zotero-language": "en"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                n += len(json.load(r))
        except Exception as e:
            print(f"[cites] Web API 导入失败（{len(its)} 条）: {e}", file=sys.stderr)
            return n if n else -1  # -1 = 全部失败，调用方退回 .bib
    return n

def bib_escape(s):
    return re.sub(r"[{}&]", lambda m: "\\" + m.group(0), s or "")

def import_bib(items, coll_names):
    BIB_DIR.mkdir(exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    path = BIB_DIR / f"{day}.bib"
    entries = []
    for it, cname in zip(items, coll_names):
        doi = (it.get("DOI") or "").replace("10.", "").replace("/", "_")
        key = f"zm2026_{re.sub(r'[^A-Za-z0-9]', '', doi)[:18] or 'x'}_{len(entries)}"
        entries.append(
            f"@article{{{key},\n"
            f"  title = {{{bib_escape(it['title'])}}},\n"
            f"  author = {{{bib_escape(' and '.join(a.get('firstName','') + ' ' + a.get('lastName','') for a in it.get('author', [])))}}},\n"
            f"  journal = {{{bib_escape(it.get('publicationTitle',''))}}},\n"
            f"  year = {{{it.get('date','')}}},\n"
            f"  doi = {{{it.get('DOI','')}}},\n"
            f"  url = {{{it.get('url','')}}},\n"
            f"  abstract = {{{bib_escape(it.get('abstractNote',''))}}},\n"
            f"  note = {{ZoteroMedHOT 新施用文献 · 建议放入收藏「{cname or '库根'}」}}\n"
            "}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(entries) + "\n")
    return path

# ---------------- macOS 通知 ----------------
def mac_notify(title, body):
    def esc(s): return s.replace('"', '\\"').replace("\n", " ")
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(body)}" with title "{esc(title)}"'], check=False)

# ---------------- main ----------------
def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    imported = state.get("imported", {})
    since = state.get("last_run") or (datetime.now() - timedelta(days=WATERMARK_DEFAULT)).strftime("%Y-%m-%d")
    print(f"[cites] 监测窗口 since={since}", file=sys.stderr)

    db, tmp = build.open_db()
    ckeys = coll_keys(db)
    wset = watch_set(db)
    ldois = local_dois(db)
    wids = list(resolve_wids(list(wset.values())).values())
    print(f"[cites] 监测集 {len(wset)} 条（带 DOI {len(set(wset.values()))}），OpenAlex 命中 {len(wids)}", file=sys.stderr)

    cands = new_citing(wids, since)
    fresh, seen = [], set()
    for w in cands:
        doi = norm_doi(w.get("doi")).lower()
        if not doi or doi in imported or doi in ldois or doi in seen: continue
        s, why, venue, title, _ = score_candidate(w)
        if s < THRESH: continue
        seen.add(doi)
        coll = theme_to_collection(title)
        fresh.append((s, why, w, doi, coll))
    fresh.sort(key=lambda x: -x[0])
    fresh = fresh[:MAX_IMPORT]
    print(f"[cites] 候选 {len(cands)} 条新引用，达标 {len(fresh)} 条", file=sys.stderr)

    if fresh:
        items, coll_names, api_targets = [], [], []
        for s, why, w, doi, coll in fresh:
            it = zotero_item(w, coll)
            items.append(it); coll_names.append(coll)
            api_targets.append((it, ckeys.get(coll)))
        via = "api" if KEYF.exists() else None
        if via:
            ok = import_api(api_targets)
            via = "api" if ok > 0 else "bib"
        if via == "bib" or via is None:
            path = import_bib(items, coll_names)
            print(f"[cites] 已生成 {path}（{len(items)} 条，拖入 Zotero 并选择目标收藏）", file=sys.stderr)
        for s, why, w, doi, coll in fresh:
            imported[doi] = {"date": datetime.now().strftime("%Y-%m-%d"), "title": w.get("title") or "",
                             "score": round(s, 1), "collection": coll or "", "via": via or "bib",
                             "why": " · ".join(why)}
        if via == "api":
            mac_notify("ZoteroMedHOT · 新施用文献",
                       f"今日导入 {len(fresh)} 条：《{fresh[0][2].get('title','')[:40]}》等，已入库")
        else:
            mac_notify("ZoteroMedHOT · 新施用文献",
                       f"今日发现 {len(fresh)} 条新施用文献，已生成 .bib 待导入")

    state["last_run"] = datetime.now().strftime("%Y-%m-%d")
    state["imported"] = imported
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    db.close()
    print(f"[cites] 完成：导入/待导 {len(fresh)} 条", file=sys.stderr)

if __name__ == "__main__":
    main()
