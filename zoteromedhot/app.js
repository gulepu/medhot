const CATEGORIES = ["全部", "神经病学", "中西医结合", "指南更新", "药物审批", "临床研究"];

const feed = document.getElementById("feed");
const tabs = document.getElementById("catTabs");
const badge = document.getElementById("todayBadge");
const footSrc = document.getElementById("footSrc");

let DATA = window.__ZOTERO_MEDHOT_DATA || null;
let activeCat = "全部";

async function loadData() {
  if (DATA) return;
  try {
    const res = await fetch("data.json");
    DATA = await res.json();
  } catch {
    if (!DATA) { feed.innerHTML = '<p class="empty">数据加载失败：请通过 HTTP 服务访问，或将数据写入 window.__ZOTERO_MEDHOT_DATA</p>'; return; }
  }
  badge.textContent = DATA.date;
  if (footSrc && DATA.source) footSrc.textContent = DATA.source;
  renderTabs();
  render();
}

function renderTabs() {
  tabs.innerHTML = "";
  for (const c of CATEGORIES) {
    const n = c === "全部" ? DATA.items.length : DATA.items.filter(i => i.category === c).length;
    if (c !== "全部" && n === 0) continue;
    const b = document.createElement("button");
    b.className = "cat-tab" + (c === activeCat ? " active" : "");
    b.textContent = n > 0 ? `${c} ${n}` : c;
    b.onclick = () => { activeCat = c; renderTabs(); render(); };
    tabs.appendChild(b);
  }
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function heatBadges(it) {
  const h = it.heat;
  if (!h) return "";
  const parts = [];
  if (it.origin === "feed") {
    if (h.feed) parts.push(`📡 ${esc(h.feed)}`);
  } else {
    if (h.related) parts.push(`🔗 库内手联 ${h.related}`);
    if (h.notes || h.ai) parts.push(`📝 笔记 ${h.notes || 0}` + (h.ai ? `（AI 解读 ${h.ai}）` : ""));
    if (h.theme) parts.push(`🔖 主题收藏 ${h.theme}`);
    else if (h.collections) parts.push(`🔖 收藏 ${h.collections}`);
  }
  if (!parts.length) return "";
  return `<div class="heat-badges">${parts.map(p => `<span class="heat-badge">${p}</span>`).join("")}</div>`;
}

function sectionDivider(label, hint) {
  const d = document.createElement("div");
  d.className = "section-divider";
  d.innerHTML = `<span class="sd-label">${esc(label)}</span><span class="sd-hint">${esc(hint)}</span>`;
  return d;
}

function card(idx, it) {
  const el = document.createElement("article");
  el.className = "card" + (it.origin === "library" ? " card-lib" : "");
  el.innerHTML = `
    <div class="card-top">
      <span class="rank">${idx + 1}</span>
      <span class="tag">${esc(it.category)}</span>
      ${it.origin === "library" ? '<span class="tag tag-lib">库内热榜</span>' : '<span class="tag tag-feed">订阅新品</span>'}
    </div>
    <h2><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title_zh)}</a></h2>
    <p class="summary">${esc(it.summary)}</p>
    ${heatBadges(it)}
    <div class="card-meta">
      <span>${esc(it.source)}</span>
      <span>${esc(it.date)}</span>
      <a href="${esc(it.url)}" target="_blank" rel="noopener">阅读原文 ↗</a>
    </div>
    <p class="card-meta" style="border:none;padding-top:4px"><span class="hot">★ ${esc(it.why_hot)}</span></p>`;
  return el;
}

function render() {
  feed.innerHTML = "";
  const items = DATA.items.filter(i => activeCat === "全部" || i.category === activeCat);
  if (!items.length) { feed.innerHTML = '<p class="empty">该分类暂无内容</p>'; return; }
  const feeds = items.filter(i => i.origin === "feed");
  const libs = items.filter(i => i.origin === "library");
  let idx = 0;
  if (feeds.length) {
    feed.appendChild(sectionDivider("订阅新品", "近 24h 入库 · 按订阅权重与主题命中排序"));
    feeds.forEach(it => { feed.appendChild(card(idx++, it)); });
  }
  if (libs.length) {
    feed.appendChild(sectionDivider("库内热榜", "按引用及施用关系排序：手联 · 笔记 · AI 解读 · 收藏"));
    libs.forEach(it => { feed.appendChild(card(idx++, it)); });
  }
}

loadData();
