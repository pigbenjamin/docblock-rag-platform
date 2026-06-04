#!/usr/bin/env bash
# 執行所有整合測試
# Usage:
#   ./tests/test_all.sh          # 本地 localhost 模式
#   TEST_ENV=k8s ./tests/test_all.sh  # k8s 模式

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PASS=0
FAIL=0
FAILED_LIST=()

for i in 01 02 04 05 06 07 08 10 12 14; do
  FILE=$(ls "${SCRIPT_DIR}/${i}_"*.py 2>/dev/null | head -1)
  if [[ -z "$FILE" ]]; then
    echo "⚠️  Test ${i}: 找不到檔案，跳過"
    continue
  fi

  echo ""
  if python3 "$FILE"; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAILED_LIST+=("${i}")
  fi
done

echo ""
echo "=================================="
echo "  結果：PASS=${PASS}  FAIL=${FAIL}"
if [[ ${#FAILED_LIST[@]} -gt 0 ]]; then
  echo "  失敗：${FAILED_LIST[*]}"
fi
echo "=================================="
[[ $FAIL -eq 0 ]]
