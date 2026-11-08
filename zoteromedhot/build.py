#!/usr/bin/env python3
"""ZoteroMedHOT 构建管道
读取本地 Zotero 数据库（~/Zotero/zotero.sqlite），生成 data.json：
  A. 订阅新品：62 个订阅近 24h（不足则 72h/7d 兜底）入库的文献，按订阅权重 + 主题命中排序
  B. 库内热榜：主库出版物按「引用及施用关系」打分（库内关联、笔记、收藏、标签、新鲜度）
"""
import json, os, re, shutil, sqlite3, sys, tempfile, html
from datetime import datetime, timedelta
from pathlib import Path

DB_DIR = Path.home() / "Zotero"
DB_MAIN = DB_DIR / "zotero.sqlite"
DB_WAL = DB_DIR / "zotero.sqlite-wal"
REF_CACHE = Path.home() / "Library/Application Support/Zotero/Profiles/yiz28nyj.default/zoteroreference.json"
OUT = Path(__file__).parent / "data.json"

WINDOW_HOURS = 24          # 订阅新品窗口，逐档放宽到 7 天
FEED_TOP = 8               # 订阅新品条数
LIB_TOP = 6                # 库内热榜条数

# ---- 用户主题收藏（用于加权与分类）----
THEME_COLLECTIONS = {"PD", "运动神经元病", "WD中医研究", "HNPP", "吞咽障碍",
                    "恶化及加重", "癫痫", "基底节不对称", "震颤", "肌张力障碍"}
TCM_KEYWORDS = re.compile(r"中医|中药|针灸|方剂|经方|中西医结合|民族药|藏医|蒙医|"
                          r"traditional\s+chinese\s+medicine|\bTCM\b|chinese\s+herbal|acupuncture", re.I)
NEURO_JOURNAL = re.compile(r"神经|卒中|癫痫|脑血管|脑与神经|临床神经|脑病|神经电", re.I)

# 主题关键词命中（来自用户收藏 + 订阅）
THEME_PATTERNS = [
    ("帕金森", re.compile(r"帕金森|parkinson|LRRK2|Parkin|α-?synuclein|alpha-?synuclein|GPR56|GLD2|PARK\d+", re.I)),
    ("运动神经元病", re.compile(r"运动神经元|motor neuron|\bALS\b|\bMND\b|肌萎缩侧索硬化|sod1|\bSOD1\b|\bFUS\b|\bTDP-43", re.I)),
    ("亨廷顿", re.compile(r"亨廷|huntington|\bHTT\b|polyglutamine", re.I)),
    ("威尔逊病", re.compile(r"威尔逊|wilson\b|ATP7B|肝豆状核", re.I)),
    ("PSP/MSA", re.compile(r"progressive supranuclear|\bPSP\b|multiple system atrophy|\bMSA\b|进行性核上性", re.I)),
    ("肌张力障碍", re.compile(r"肌张力障碍|dystonia|\bDYT\d+|GCH1|TH\b|多巴反应性", re.I)),
    ("癫痫", re.compile(r"癫痫|epilepsy|epileptic|SEIZURE", re.I)),
    ("吞咽障碍", re.compile(r"吞咽|dysphagia", re.I)),
    ("震颤", re.compile(r"震颤|tremor", re.I)),
    ("HNPP", re.compile(r"\bHNPP\b|压力性神经病|hereditary neuropathy", re.I)),
]

def feed_weight(name: str) -> float:
    n = name
    if n.startswith("Wiley:"): return 3.5
    if "CNKI" in n:
        return 3.0 if NEURO_JOURNAL.search(n) else (2.5 if TCM_KEYWORDS.search(n) else 2.0)
    if n.startswith("ClinicalTrials.gov"): return 2.0
    if any(k in n for k in ["tremor","parkinson","dystonia","epilepsy","wilson",
                            "multiple system atrophy","Progressive supranuclear","Motor neuron"]):
        return 3.0
    if "ScienceDaily" in n or "BMC" in n: return 2.0
    return 1.0  # 社区类订阅（Zotero 中文社区 / 信息差 / Bensz / Docear / ZotWatcher）

def theme_hits(text: str) -> int:
    return sum(1 for _, p in THEME_PATTERNS if p.search(text))

def clean_abstract(raw, limit=220):
    """去 HTML（含 &lt;正&gt; 转义标记）、优先取【摘要翻译】中文段"""
    if not raw: return ""
    raw = html.unescape(raw)  # 先解转义，再剥标签（CNKI 摘要形如 &lt;正&gt;...）
    m = re.search(r"【摘要翻译】\s*(.+?)(?:\n\n|【|$)", raw, re.S)
    t = m.group(1) if m else raw
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"#br", " ", t)
    t = re.sub(r"[（(]\s*[）)]", "", t)  # 去掉 CNKI 残留空括号
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")

# ---------------- 打开数据库 ----------------
def open_db():
    tmp = Path(tempfile.mkdtemp(prefix="zotero-zm-"))
    shutil.copy2(DB_MAIN, tmp / "zotero.sqlite")
    if DB_WAL.exists() and DB_WAL.stat().st_size > 0:
        shutil.copy2(DB_WAL, tmp / "zotero.sqlite-wal")
    db = sqlite3.connect(f"file:{tmp/'zotero.sqlite'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db, tmp

PIVOT = """
  MAX(CASE WHEN fc.fieldName='title' THEN v.value END) AS title,
  MAX(CASE WHEN fc.fieldName='abstractNote' THEN v.value END) AS abstract,
  MAX(CASE WHEN fc.fieldName='url' THEN v.value END) AS url,
  MAX(CASE WHEN fc.fieldName='DOI' THEN v.value END) AS doi,
  MAX(CASE WHEN fc.fieldName='publication' THEN v.value END) AS publication,
  MAX(CASE WHEN fc.fieldName='date' THEN v.value END) AS pubdate
"""

def fetch_rows(db, where, params=()):
    q = f"""SELECT i.itemID, i.dateAdded, i.key, i.libraryID, it.typeName, {PIVOT}
      FROM items i
      LEFT JOIN itemData d ON d.itemID=i.itemID
      LEFT JOIN itemDataValues v ON d.valueID=v.valueID
      LEFT JOIN fieldsCombined fc ON d.fieldID=fc.fieldID
      JOIN itemTypes it ON i.itemTypeID=it.itemTypeID
      WHERE {where}
      GROUP BY i.itemID
      HAVING it.typeName NOT IN ('attachment','note','annotation')"""
    return db.execute(q, params).fetchall()

# ---------------- A. 订阅新品 ----------------
def build_feed_items(db):
    for hours in (WINDOW_HOURS, 72, 24 * 7):
        rows = fetch_rows(db, "i.libraryID IN (SELECT libraryID FROM feeds) AND i.dateAdded >= datetime('now', ?)",
                          (f"-{hours} hours",))
        feeds = {r["itemID"]: None for r in rows}
        fmap = {}
        for r in db.execute("SELECT libraryID, name FROM feeds"):
            fmap[r["libraryID"]] = r["name"]
        # 按 (feed权重, 主题命中, 时间) 评分，DOI 去重
        seen, scored = set(), []
        for r in rows:
            title, abstract, doi = r["title"] or "", r["abstract"] or "", r["doi"] or ""
            key = doi.lower() if doi else re.sub(r"\W+", "", title)[:60]
            if key in seen: continue
            seen.add(key)
            feed = fmap.get(r["libraryID"], "订阅")
            score = feed_weight(feed) + min(theme_hits(title + " " + abstract), 3)
            scored.append((score, r["dateAdded"], feed, r))
        if len(seen) >= 6:
            break
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, added, feed, r in scored[:FEED_TOP]:
        title, abstract = r["title"] or "", r["abstract"] or ""
        pub = r["publication"] or ""
        text = title + " " + abstract
        if feed.startswith("ClinicalTrials.gov"):
            cat = "临床研究"
        elif re.search(r"guideline|指南", text, re.I):
            cat = "指南更新"
        elif re.search(r"FDA|NMPA|获批|approved for|批准上市", text, re.I):
            cat = "药物审批"
        elif TCM_KEYWORDS.search(title) or (TCM_KEYWORDS.search(feed) and "CNKI" in feed):
            cat = "中西医结合"
        else:
            cat = "神经病学"
        out.append({
            "title_zh": title,
            "summary": clean_abstract(abstract) or "（订阅入库，暂无摘要）",
            "category": cat,
            "source": f"订阅 · {feed}" + (f" · {pub}" if pub else ""),
            "url": r["url"] or (f"https://doi.org/{r['doi']}" if r["doi"] else "https://zotero.org"),
            "date": added[:10],
            "why_hot": f"订阅「{feed}」今日入库 · 主题命中 {min(theme_hits(text),3)} · 订阅权重 {feed_weight(feed):.1f}"
                       if score > 1 else f"订阅「{feed}」今日入库",
            "heat": {"feed": feed, "score": round(score, 1)},
            "origin": "feed",
        })
    return out, len(seen)

# ---------------- B. 库内热榜 ----------------
def hot_scores(db, top=LIB_TOP, min_score=3.0):
    """主库施用热度打分：返回 ([(score, row)] 降序, signals)，供 cites.py 监测集复用"""
    rows = fetch_rows(db, "i.libraryID=1")
    # 库内关联：itemRelations 指向 zotero 库内 key
    key2id = {r["key"]: r["itemID"] for r in rows}
    rel_out, rel_in = {}, {}
    for r in db.execute("""SELECT ir.itemID, ir.object, rp.predicate FROM itemRelations ir
                           JOIN relationPredicates rp ON ir.predicateID=rp.predicateID"""):
        m = re.search(r"/items/([A-Z0-9]{8})$", r["object"] or "")
        tgt = key2id.get(m.group(1)) if m else None
        if tgt is not None:
            rel_out[r["itemID"]] = rel_out.get(r["itemID"], 0) + 1
            rel_in[tgt] = rel_in.get(tgt, 0) + 1
    # 笔记数 + AI 解读笔记
    notes, ai_notes = {}, {}
    for r in db.execute("SELECT parentItemID, COUNT(*) c FROM itemNotes WHERE parentItemID IS NOT NULL GROUP BY parentItemID"):
        notes[r["parentItemID"]] = r["c"]
    for r in db.execute("""SELECT n.itemID, n.parentItemID FROM itemNotes n
        JOIN itemTags t ON t.itemID=n.itemID JOIN tags tg ON tg.tagID=t.tagID
        WHERE tg.name IN ('/AI文献解读','/注释笔记') AND n.parentItemID IS NOT NULL"""):
        ai_notes[r["parentItemID"]] = ai_notes.get(r["parentItemID"], 0) + 1
    # 收藏数 + 主题收藏
    coll, theme_coll = {}, {}
    for r in db.execute("""SELECT ci.itemID, c.collectionName FROM collectionItems ci
        JOIN collections c ON c.collectionID=ci.collectionID WHERE c.libraryID=1"""):
        coll[r["itemID"]] = coll.get(r["itemID"], 0) + 1
        if r["collectionName"] in THEME_COLLECTIONS:
            theme_coll[r["itemID"]] = theme_coll.get(r["itemID"], 0) + 1
    tags_n = {}
    for r in db.execute("""SELECT itag.itemID, COUNT(*) c FROM itemTags itag
        JOIN items i ON i.itemID=itag.itemID WHERE i.libraryID=1 GROUP BY itag.itemID"""):
        tags_n[r["itemID"]] = r["c"]
    # zoteroreference 缓存：用户取过引文列表 = 施用信号
    ref_items = set()
    try:
        ref_items = set(json.load(open(REF_CACHE)).keys())
    except Exception:
        pass

    GENERIC_TITLES = {"addon item", "untitled document", "untitled", "document", "web page", "webpage"}
    def norm_date(s):
        # 原始值形如 "2016-07-00 2016/7"（归一化值 + 原值），取第一段
        s = ((s or "").strip().split() or [""])[0]
        m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", s)
        if not m: return ""
        return f"{m.group(1)}-{m.group(2)}" if not m.group(3) or m.group(3) == "00" else s

    scored = []
    for r in rows:
        iid = r["itemID"]
        if (r["title"] or "").strip().lower() in GENERIC_TITLES:
            continue
        if len((r["title"] or "").strip()) < 8:
            continue
        n_notes = notes.get(iid, 0)
        n_ai = ai_notes.get(iid, 0)
        n_rel = rel_out.get(iid, 0) + rel_in.get(iid, 0)
        n_col = coll.get(iid, 0)
        n_theme = theme_coll.get(iid, 0)
        n_tags = tags_n.get(iid, 0)
        added = r["dateAdded"]
        recent = 2.0 if added >= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S") \
                 else (1.0 if added >= (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S") else 0.0)
        ref_bonus = 1.5 if r["key"] in ref_items else 0.0
        score = n_notes * 2 + n_ai * 3 + n_rel * 3 + n_theme * 1.5 + n_col * 0.5 + n_tags * 0.3 + recent + ref_bonus
        if score < min_score:  # 低于阈值的不上榜
            continue
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return scored[:top], {"notes": notes, "ai_notes": ai_notes, "rel_out": rel_out,
                          "rel_in": rel_in, "coll": coll, "theme_coll": theme_coll,
                          "norm_date": norm_date}

def build_lib_items(db):
    scored, signals = hot_scores(db, top=LIB_TOP)
    notes, ai_notes = signals["notes"], signals["ai_notes"]
    rel_out, rel_in, coll, theme_coll = signals["rel_out"], signals["rel_in"], signals["coll"], signals["theme_coll"]
    norm_date = signals["norm_date"]

    # 主题收藏名用于 source 与分类
    ph = ",".join("?" * len(THEME_COLLECTIONS))
    def primary_coll(r):
        name = db.execute(f"""SELECT c.collectionName FROM collectionItems ci
            JOIN collections c ON c.collectionID=ci.collectionID
            WHERE ci.itemID=? AND c.libraryID=1
            ORDER BY (CASE WHEN c.collectionName IN ({ph}) THEN 0 ELSE 1 END) ASC LIMIT 1""",
            (r["itemID"], *THEME_COLLECTIONS)).fetchone()
        return name["collectionName"] if name else None
    out = []
    for score, r in scored:
        title, abstract = r["title"] or "", r["abstract"] or ""
        pub = r["publication"] or ""
        coll_name = primary_coll(r)
        n_notes = notes.get(r["itemID"], 0); n_ai = ai_notes.get(r["itemID"], 0)
        n_rel = rel_out.get(r["itemID"], 0) + rel_in.get(r["itemID"], 0)
        n_theme = theme_coll.get(r["itemID"], 0); n_col = coll.get(r["itemID"], 0)
        cat = "中西医结合" if (coll_name and TCM_KEYWORDS.search(coll_name)) or TCM_KEYWORDS.search(title) else "神经病学"
        pubdate = norm_date(r["pubdate"])
        why = []
        if n_rel: why.append(f"库内手联 {n_rel}")
        if n_ai: why.append(f"AI 解读/注释 {n_ai}")
        elif n_notes: why.append(f"笔记 {n_notes}")
        if n_theme: why.append(f"主题收藏 {n_theme}")
        if pubdate: why.append(f"发表 {pubdate[:4]}")
        out.append({
            "title_zh": title,
            "summary": clean_abstract(abstract) or "（库内文献，暂无摘要）",
            "category": cat,
            "source": "我的文献库" + (f" · {coll_name}" if coll_name else "") + (f" · {pub}" if pub else ""),
            "url": r["url"] or (f"https://doi.org/{r['doi']}" if r["doi"] else "https://zotero.org"),
            "date": pubdate or r["dateAdded"][:10],
            "why_hot": "库内热榜：" + " · ".join(why) if why else "库内热榜：施用信号最高",
            "heat": {"notes": n_notes, "ai": n_ai, "related": n_rel, "collections": n_col, "theme": n_theme, "score": round(score, 1)},
            "origin": "library",
        })
    return out

# ---------------- main ----------------
def main():
    print(f"[zoteromedhot] 复制数据库 {DB_MAIN} …", file=sys.stderr)
    db, tmp = open_db()
    lib_count = db.execute("SELECT COUNT(*) c FROM items WHERE libraryID=1").fetchone()["c"]
    feeds = db.execute("SELECT COUNT(*) c FROM feeds").fetchone()["c"]
    print(f"[zoteromedhot] 主库 {lib_count} 条 / 订阅 {feeds} 个", file=sys.stderr)
    feed_items, feed_seen = build_feed_items(db)
    lib_items = build_lib_items(db)
    items = feed_items + lib_items
    data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "source": f"本地 Zotero 文献库（~/Zotero）· 主库 {lib_count} 条 · 订阅 {feeds} 个 · 纯脚本构建",
        "stats": {"feed_items_window": len(feed_items), "feed_candidates": feed_seen,
                  "lib_hot_items": len(lib_items), "total": len(items)},
        "items": items,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    db.close(); shutil.rmtree(tmp, ignore_errors=True)
    print(f"[zoteromedhot] 写出 {OUT}（{len(items)} 条：订阅新品 {len(feed_items)} + 库内热榜 {len(lib_items)}）", file=sys.stderr)

if __name__ == "__main__":
    main()
