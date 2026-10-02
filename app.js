const CATEGORIES = ["全部", "神经病学", "中西医结合", "指南更新", "药物审批", "临床研究"];

const feed = document.getElementById("feed");
const tabs = document.getElementById("catTabs");
const badge = document.getElementById("todayBadge");

let DATA = window.__MEDHOT_DATA || null;
let activeCat = "全部";

async function loadData() {
  if (DATA) return;
  try {
    const res = await fetch("data.json");
    DATA = await res.json();
  } catch {
    if (!DATA) { feed.innerHTML = '<p class="empty">数据加载失败：请通过 HTTP 服务访问，或将数据写入 window.__MEDHOT_DATA</p>'; return; }
  }
  badge.textContent = DATA.date;
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
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function render() {
  feed.innerHTML = "";
  const items = DATA.items.filter(i => activeCat === "全部" || i.category === activeCat);
  if (!items.length) { feed.innerHTML = '<p class="empty">该分类暂无内容</p>'; return; }
  items.forEach((it, idx) => {
    const el = document.createElement("article");
    el.className = "card";
    el.innerHTML = `
      <div class="card-top">
        <span class="rank">${idx + 1}</span>
        <span class="tag">${esc(it.category)}</span>
      </div>
      <h2><a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title_zh)}</a></h2>
      <p class="summary">${esc(it.summary)}</p>
      <div class="card-meta">
        <span>${esc(it.source)}</span>
        <span>${it.date}</span>
        <a href="${esc(it.url)}" target="_blank" rel="noopener">阅读原文 ↗</a>
      </div>
      <p class="card-meta" style="border:none;padding-top:4px"><span class="hot">★ ${esc(it.why_hot)}</span></p>`;
    feed.appendChild(el);
  });
}

loadData();
