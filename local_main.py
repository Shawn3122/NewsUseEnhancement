# -*- coding: utf-8 -*-
"""
本地新聞全文擷取主程式（寫入本地 xlsx，不碰 Google Sheet）。

職責：
1. 從本地 xlsx 讀取待處理的新聞（A~E 欄）
2. 多層 fallback 擷取全文
3. 管理重試狀態 (PENDING → RETRY_1/2/3 → DONE/FAILED)
4. 將結果寫入本地 xlsx 的 F~L 欄位
5. 每 N 筆自動儲存（避免記憶體累積）

用法：
  python local_main.py                                        # 擷取全部（預設）
  python local_main.py --input news_trimmed.xlsx              # 指定輸入檔
  python local_main.py --batch-size 100                       # 自訂批次大小
  python local_main.py --test 5                               # 測試模式（只看日誌不寫入）
  python local_main.py --resume                               # 從現有 xlsx 繼續（跳過 DONE）
"""
from __future__ import annotations

import argparse
import sys
import time
import os
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import config
import scraper
import local_sheets_client as sheets_client

_TZ_TAIPEI = timezone(timedelta(hours=8))


# =============================================================================
# 重試狀態機
# =============================================================================

def _next_status_on_failure(current_status: str) -> str:
    """決定失敗後的下一個狀態。"""
    transitions = {
        config.STATUS_PENDING: "RETRY_1",
        "RETRY_1": "RETRY_2",
        "RETRY_2": "RETRY_3",
        "RETRY_3": config.STATUS_FAILED,
    }
    return transitions.get(current_status, config.STATUS_FAILED)


def _build_update_fields(
    result: scraper.ScrapeResult,
    current_status: str,
) -> dict:
    """根據擷取結果組合要更新回 xlsx 的欄位。"""
    now_str = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    domain = urlparse(result.url).netloc

    if result.success:
        return {
            "狀態": config.STATUS_DONE,
            "內文": result.text,
            "擷取方法": result.method,
            "診斷資訊": result.diagnostics_json(),
            "最後嘗試": now_str,
            "字數": result.char_count,
            "網域": domain,
        }

    if result.error_type == config.ERROR_VIDEO:
        return {
            "狀態": config.STATUS_TITLE_ONLY,
            "內文": "",
            "擷取方法": "video-detected",
            "診斷資訊": result.diagnostics_json(),
            "最後嘗試": now_str,
            "字數": 0,
            "網域": domain,
        }

    next_status = _next_status_on_failure(current_status)
    return {
        "狀態": next_status,
        "擷取方法": "",
        "診斷資訊": result.diagnostics_json(),
        "最後嘗試": now_str,
        "字數": 0,
        "網域": domain,
    }


# =============================================================================
# 報告產生器
# =============================================================================

class ReportCollector:
    """蒐集擷取過程中的統計數據，最後產出報告。"""

    def __init__(self):
        self.start_time = time.time()
        self.processed = 0
        self.results: list[dict] = []
        self.status_counts: Counter = Counter()
        self.method_counts: Counter = Counter()
        self.error_counts: Counter = Counter()
        self.domain_failures: Counter = Counter()
        self.domain_successes: Counter = Counter()

    def record(self, row: dict, result: scraper.ScrapeResult, new_status: str):
        self.processed += 1
        self.status_counts[new_status] += 1

        if result.success:
            self.method_counts[result.method] += 1
            self.domain_successes[result.domain] += 1
        else:
            self.error_counts[result.error_type] += 1
            self.domain_failures[result.domain] += 1

        self.results.append({
            "row": row["row_index"],
            "domain": result.domain,
            "title": row["title"][:40],
            "success": result.success,
            "method": result.method,
            "chars": result.char_count,
            "error": result.error_type,
            "new_status": new_status,
            "attempts": len(result.attempts),
        })

    def print_report(self, sheet_stats: dict | None = None):
        elapsed = time.time() - self.start_time
        now = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")

        print(f"\n{'='*70}")
        print(f"  新聞擷取報告  {now}")
        print(f"{'='*70}")
        print(f"  處理筆數: {self.processed}")
        print(f"  執行時間: {elapsed:.0f} 秒 ({elapsed/60:.1f} 分鐘)")

        print(f"\n--- 本次處理結果 ---")
        for status, count in self.status_counts.most_common():
            pct = count / self.processed * 100 if self.processed else 0
            print(f"  {status:<12} {count:>4} 筆 ({pct:.0f}%)")

        if self.method_counts:
            print(f"\n--- 成功擷取方法 ---")
            for method, count in self.method_counts.most_common():
                print(f"  {method:<25} {count:>4} 筆")

        if self.error_counts:
            print(f"\n--- 失敗原因統計 ---")
            for err, count in self.error_counts.most_common():
                print(f"  {err:<20} {count:>4} 筆")

        if self.domain_failures:
            print(f"\n--- 需關注的網域 (本次有失敗) ---")
            problem_domains = sorted(
                self.domain_failures.items(), key=lambda x: -x[1]
            )
            for domain, fail_count in problem_domains[:15]:
                ok_count = self.domain_successes.get(domain, 0)
                total = ok_count + fail_count
                print(f"  {domain:<35} 失敗 {fail_count}/{total}")

        failed_items = [r for r in self.results if not r["success"]]
        if failed_items:
            print(f"\n--- 失敗項目明細 (供人工評估) ---")
            print(f"  {'列號':>5} | {'網域':<30} | {'錯誤':<15} | {'新狀態':<10} | 標題")
            print(f"  {'-'*95}")
            for item in failed_items:
                print(
                    f"  {item['row']:>5} | {item['domain']:<30} | "
                    f"{item['error']:<15} | {item['new_status']:<10} | "
                    f"{item['title']}"
                )

        if sheet_stats:
            print(f"\n--- xlsx 整體狀態 ---")
            print(f"  總筆數: {sheet_stats.get('total', '?')}")
            for key in [config.STATUS_DONE, config.STATUS_PENDING,
                        "RETRY_1", "RETRY_2", "RETRY_3",
                        config.STATUS_FAILED]:
                if key in sheet_stats:
                    print(f"  {key:<12} {sheet_stats[key]:>5} 筆")

        print(f"\n{'='*70}\n")


# =============================================================================
# 主流程
# =============================================================================

def run_fetch(xlsx_path: str, batch_size: int, test_mode: bool = False):
    """執行擷取主流程。"""
    sheets_client.init(xlsx_path)
    rows = sheets_client.get_pending_rows(batch_size=batch_size)

    if not rows:
        print("沒有待處理的新聞（或全部已完成）。")
        return

    print(f"取得 {len(rows)} 筆待處理 "
          f"(PENDING: {sum(1 for r in rows if r['status']==config.STATUS_PENDING)}, "
          f"RETRY: {sum(1 for r in rows if r['status'].startswith('RETRY'))})")

    report = ReportCollector()
    updates_batch: list[tuple[int, dict]] = []
    WRITE_EVERY = 10  # 每 10 筆寫入一次

    for i, row in enumerate(rows, 1):
        url = row["url"]
        domain = urlparse(url).netloc
        print(f"  [{i}/{len(rows)}] {domain} - {row['title'][:45]}...", end=" ", flush=True)

        result = scraper.scrape_url(url)
        fields = _build_update_fields(result, row["status"])

        if result.success:
            print(f"[OK] {result.method} ({result.char_count}字)")
        else:
            print(f"[FAIL] {result.error_type} -> {fields['狀態']}")

        report.record(row, result, fields["狀態"])
        updates_batch.append((row["row_index"], fields))

        # 定期寫入
        if len(updates_batch) >= WRITE_EVERY:
            print(f"  → 寫入 {len(updates_batch)} 筆到 xlsx...", flush=True)
            if not test_mode:
                sheets_client.batch_update_rows(updates_batch)
            updates_batch = []

        # 禮貌延遲
        if i < len(rows):
            time.sleep(config.REQUEST_DELAY)

    # 寫入剩餘
    if updates_batch:
        print(f"  → 寫入剩餘 {len(updates_batch)} 筆到 xlsx...", flush=True)
        if not test_mode:
            sheets_client.batch_update_rows(updates_batch)

    # 最終儲存
    if not test_mode:
        sheets_client.flush()
        print(f"  → xlsx 已儲存: {xlsx_path}")

    try:
        sheet_stats = sheets_client.get_sheet_stats()
    except Exception:
        sheet_stats = None
    report.print_report(sheet_stats)


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="新聞全文擷取（本地 xlsx 版）")
    parser.add_argument("--input", default="news_trimmed.xlsx",
                        help="輸入的本地 xlsx 檔案路徑 (預設: news_trimmed.xlsx)")
    parser.add_argument("--batch-size", type=int, default=config.BATCH_SIZE,
                        help=f"每次處理筆數 (預設 {config.BATCH_SIZE})")
    parser.add_argument("--test", type=int, metavar="N",
                        help=f"測試模式：處理前 N 筆，顯示日誌但不改寫 xlsx")
    args = parser.parse_args()

    xlsx_path = os.path.expanduser(args.input)
    if not os.path.exists(xlsx_path):
        print(f"檔案不存在: {xlsx_path}")
        sys.exit(1)

    test_mode = bool(args.test)
    batch_size = args.test if args.test else args.batch_size

    print(f"輸入檔案: {xlsx_path}")
    if test_mode:
        print(f"測試模式：只看日誌，不寫入 xlsx")

    run_fetch(xlsx_path=xlsx_path, batch_size=batch_size, test_mode=test_mode)


if __name__ == "__main__":
    main()
