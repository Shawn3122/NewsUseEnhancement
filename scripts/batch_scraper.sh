#!/bin/bash
# Batch scraper: 自動處理 xlsx 中所有 PENDING/None 狀態的列
# 每次執行處理 BATCH_SIZE 筆，自動找到最後一個未處理的列銜接
# 相容 no_agent cronjob 模式

cd ~/projects/NewsUseEnhancement

BATCH_SIZE=250
INPUT_FILE="news_trimmed.xlsx"

# 計算目前剩餘未處理筆數
TOTAL=$(python3 -c "
import openpyxl, sys
wb = openpyxl.load_workbook('$INPUT_FILE', read_only=True)
ws = wb.active
total = ws.max_row - 1
pending = sum(1 for row in ws.iter_rows(min_row=2, min_col=6, max_col=6)
              if row[0].value is None or row[0].value == 'PENDING')
print(pending)
wb.close()
")

if [ "$TOTAL" -eq 0 ]; then
    echo "[$(date)] 全部處理完成，無待處理項目"
    exit 0
fi

# 如果待處理數量 > BATCH_SIZE，只跑一批；否則全部跑完
if [ "$TOTAL" -gt "$BATCH_SIZE" ]; then
    RUN_SIZE=$BATCH_SIZE
else
    RUN_SIZE=$TOTAL
fi

echo "[$(date)] 開始處理 $RUN_SIZE 筆（剩餘 $TOTAL 筆待處理）..."

source .venv/bin/activate

python local_main.py \
    --input "$INPUT_FILE" \
    --batch-size "$RUN_SIZE" \
    2>&1 | tail -20

echo "[$(date)] 本批完成"
