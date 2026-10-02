# MedHOT — 医学热点日报

聚焦 **中西医结合** 与 **神经病学** 的每日医学热点站，参考 [AIHOT](https://aihot.news/) 形式：每日精选榜单 + 分类 + 摘要卡片 + 可溯源原文链接。

## 内容

- 神经病学：卒中 / 多发性硬化 / 阿尔茨海默病 / 癫痫 / 罕见神经病
- 中西医结合：针灸 RCT / 中药 Meta 分析 / 中西医专家共识
- 其他：指南更新、FDA / NMPA 药物审批、临床研究

## 运行（本地）

纯静态站点，零依赖：

```bash
python3 -m http.server 8090
# 打开 http://localhost:8090
```

## 每日更新

编辑 [`data.json`](data.json)：

- `date`：本期日期
- `items[]`：每条含 `title_zh` / `summary` / `category`（神经病学、中西医结合、指南更新、药物审批、临床研究 五选一）/ `source` / `url` / `date` / `why_hot`

保存后刷新页面即可，无需构建步骤。

## 部署（GitHub Pages）

本仓库 main 分支即为站点根目录，在仓库 Settings → Pages 选择 `main` 分支 / root 即可上线。
