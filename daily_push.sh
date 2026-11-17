#!/bin/zsh
# MedHOT 一次性推送：JSON 校验 → commit → push → 线上验证，单次调用完成
# 用法: GH_TOKEN=xxx zsh daily_push.sh [YYYY-MM-DD]
# 退出码 0 = 线上 date 已更新到目标日期
set -uo pipefail
cd "$(dirname "$0")"
DATE="${1:-$(date +%F)}"

python3 -m json.tool data.json >/dev/null
echo "[1/4] JSON OK"

git add data.json
if ! git diff --cached --quiet; then
  git commit -m "MedHOT 每日更新 $DATE"
  echo "[2/4] committed"
else
  echo "[2/4] no changes (data.json already committed)"
fi

if [ -z "${GH_TOKEN:-}" ]; then
  echo "GH_TOKEN 未设置" >&2
  exit 1
fi
git push "https://x-access-token:${GH_TOKEN}@github.com/gulepu/medhot.git" main
echo "[3/4] pushed"

# GitHub Pages 有延迟，30s 间隔最多重试 6 次（约 3 分钟）
for i in 1 2 3 4 5 6; do
  sleep 30
  ONLINE="$(curl -s https://gulepu.github.io/medhot/data.json | jq -r .date)"
  if [ "$ONLINE" = "$DATE" ]; then
    echo "[4/4] 线上 date=$ONLINE 验证通过"
    exit 0
  fi
done
echo "[4/4] 线上 date 仍未更新（最后读到: $ONLINE）" >&2
exit 1
