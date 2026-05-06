#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞爬蟲 runner（由 cronjob 呼び）。
自己帶完整的程式碼，不依賴 import。
"""
import os
import sys
import time
import json
import signal
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

# =============================================================================
# 內嵌所有必要程式碼（不依賴專案 import）
# =============================================================================

_TZ_TAIPEI = timezone(timedelta(hours=8))
BATCH_SIZE = 250
WRITE_EVERY = 10
REQUEST_DELAY = 1.5
MAX_CONTENT_LENGTH = 49000
STATUS_PENDING = "PENDING"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"
ERROR_EMPTY = "內容為空"

# 欄位對應（1-based）
_COL = {
    "日期": 1, "標題": 2, "短網址": 3, "新聞網址": 4, "真實網址": 5,
    "狀態": 6, "內文": 7, "擷取方法": 8, "診斷資訊": 9,
    "最後嘗試": 10, "字數": 11, "網域": 12,
}
ACTIONABLE_STATUSES = {STATUS_PENDING, "RETRY_1", "RETRY_2", "RETRY_3"}

# 簡化版 scraper（直接內嵌，避免 import 問題）
try:
    import requests
    import trafilatura
    from trafilatura.settings import use_config
    import urllib3
    import warnings
    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    _trafil_config = use_config()
    _trafil_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "50")
    _HAS_SCRAPER = True
except Exception as e:
    print(f"[WARN] 爬蟲依賴載入失敗: {e}", flush=True)
    _HAS_SCRAPER = False

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}
JINA_BASE_URL = "https://r.jina.ai/"
TIMEOUT_REQUESTS = 15
TIMEOUT_JINA = 25

# 內文清洗
_TAIL_KW = ["延伸閱讀", "相關新聞", "熱門新聞", "看更多", "請繼續下滑閱讀", "看更多報導"]
_PARAGRAPH_KW = ["廣告", "Cookie", "cookie", "隱私權政策", "使用條款", "加入會員", "下載APP"]
_SOCIAL_EXACT = ["追蹤 Instagram", "追蹤 Facebook", "訂閱 Google News", "按讚加入", "分享至"]
_SOCIAL_TAIL = ["轉發", "分享", "粉絲團", "LINE", "LINE TODAY", "©", "版權所有", "follow", "Subscribe", "訂閱"]
TRAILING_LIST_MIN_LINES = 3


def _classify_error(body_snippet="", http_status=None):
    body_lower = body_snippet.lower()
    if any(kw in body_lower for kw in ["loading...", "please enable javascript", "noscript"]):
        return "SPA/JS渲染"
    if any(kw in body_lower for kw in ["subscribe", "付費", "premium", "paywall"]):
        return "付費牆"
    if http_status == 403:
        return "被封鎖"
    if http_status == 404:
        return "HTTP錯誤"
    return ERROR_EMPTY


def _is_valid_content(text, min_chars=50):
    if not text or len(text) < min_chars:
        return False
    text_lower = text.lower()
    if any(kw in text_lower for kw in ["loading...", "please enable javascript", "該頁面暫無內容"]):
        return False
    return True


def _clean_content(text):
    if not text:
        return text
    # tail truncation
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for kw in _TAIL_KW:
            if kw in line:
                lines = lines[:i]
                break
    text = "\n".join(lines)
    # paragraph removal
    lines = text.split("\n")
    lines = [l for l in lines if not any(kw in l for kw in _PARAGRAPH_KW)]
    text = "\n".join(lines)
    # social removal
    if text:
        tail_start = max(0, len(text) - 200)
        lines = text.split("\n")
        filtered = []
        char_pos = 0
        for line in lines:
            line_start = char_pos
            char_pos += len(line) + 1
            if any(p in line for p in _SOCIAL_EXACT):
                continue
            if _SOCIAL_TAIL and line_start >= tail_start:
                if any(p in line for p in _SOCIAL_TAIL):
                    continue
            filtered.append(line)
        text = "\n".join(filtered)
    # trailing list cleanup
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    count = 0
    for line in reversed(lines):
        if line.startswith("- "):
            count += 1
        else:
            break
    if count >= TRAILING_LIST_MIN_LINES:
        text = "\n".join(lines[:-count])
    # collapse blank lines
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _scrape_single_url(url):
    """回傳 (success, text, method, char_count, error_type)"""
    if not _HAS_SCRAPER:
        return False, "", "", 0, "缺少依賴"

    # Layer 1: trafilatura native
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, output_format="txt",
                                       include_comments=False, include_tables=True,
                                       favor_recall=True, config=_trafil_config)
            if _is_valid_content(text):
                return True, _clean_content(text[:MAX_CONTENT_LENGTH]), "trafilatura", len(text), ""
    except Exception:
        pass

    # Layer 2: requests + trafilatura
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT_REQUESTS,
                            verify=False, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) >= 500:
            text = trafilatura.extract(resp.text, output_format="txt",
                                       include_comments=False, include_tables=True,
                                       favor_recall=True, config=_trafil_config)
            if _is_valid_content(text):
                return True, _clean_content(text[:MAX_CONTENT_LENGTH]), "requests+trafilatura", len(text), ""
            else:
                err = _classify_error(body_snippet=text or resp.text[:500])
                # 仍記錄為失敗，但繼續下一層
    except Exception:
        pass

    # Layer 3: Jina
    try:
        jina_url = JINA_BASE_URL + url
        headers = {"Accept": "text/plain", "X-No-Cache": "true"}
        resp = requests.get(jina_url, headers=headers, timeout=TIMEOUT_JINA)
        if resp.status_code == 200:
            raw = resp.text
            lines = raw.split("\n")
            content_lines = []
            skip_header = True
            for line in lines:
                if skip_header and line.startswith(("Title:", "URL Source:", "Markdown Content:", "===")):
                    continue
                skip_header = False
                content_lines.append(line)
            text = "\n".join(content_lines).strip()
            if _is_valid_content(text):
                return True, _clean_content(text[:MAX_CONTENT_LENGTH]), "jina", len(text), ""
    except Exception:
        pass

    return False, "", "", 0, ERROR_EMPTY


def _next_status(current):
    return {"RETRY_1": "RETRY_2", "RETRY_2": "RETRY_3",
            STATUS_PENDING: "RETRY_1", "RETRY_3": STATUS_FAILED}.get(current, STATUS_FAILED)


def _build_fields(url, success, text, method, char_count, error_type, current_status):
    now = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    domain = urlparse(url).netloc
    if success:
        return {
            "狀態": STATUS_DONE, "內文": text, "擷取方法": method,
            "診斷資訊": "[]", "最後嘗試": now, "字數": char_count, "網域": domain
        }
    else:
        return {
            "狀態": _next_status(current_status), "擷取方法": "", "內文": "",
            "診斷資訊": "[]", "最後嘗試": now, "字數": 0, "網域": domain
        }


# =============================================================================
# openpyxl 封裝
# =============================================================================
try:
    import openpyxl
    HAS_OPENPYXL = True
except Exception:
    HAS_OPENPYXL = False


class LocalWB:
    def __init__(self, path):
        self.path = path
        self.wb = openpyxl.load_workbook(path)
        self.ws = self.wb.active
        self._cache = None

    def get_all_values(self):
        if self._cache is None:
            self._cache = list(self.ws.values)
        return self._cache

    def invalidate(self):
        self._cache = None

    def save(self):
        self._cache = None
        self.wb.save(self.path)

    def write_cell(self, row, col, val):
        self.ws.cell(row=row, column=col, value=val)


def _truncate(val):
    s = str(val) if val else ""
    if len(s) > MAX_CONTENT_LENGTH:
        return s[:MAX_CONTENT_LENGTH]
    return s


def get_pending(wb, batch_size):
    rows = []
    all_vals = wb.get_all_values()
    if len(all_vals) <= 1:
        return rows
    for i, row in enumerate(all_vals[1:], start=2):
        status = row[5] if len(row) > 5 else ""
        if status and status not in ACTIONABLE_STATUSES:
            continue
        url = row[4] if len(row) > 4 else ""
        title = row[1] if len(row) > 1 else ""
        date = row[0] if len(row) > 0 else ""
        if not url:
            continue
        rows.append({"row": i, "url": url, "title": title or "", "status": status or STATUS_PENDING, "date": date or ""})
    priority = {STATUS_PENDING: 0, "RETRY_1": 1, "RETRY_2": 2, "RETRY_3": 3}
    rows.sort(key=lambda r: priority.get(r["status"], 99))
    return rows[:batch_size]


def write_row(wb, row_idx, fields):
    for fname, val in fields.items():
        col = _COL.get(fname)
        if col is None:
            continue
        v = _truncate(val) if fname == "內文" else (str(val) if val else "")
        wb.write_cell(row_idx, col, v)


# =============================================================================
# 主程式
# =============================================================================

def main():
    xlsx_path = os.path.expanduser("~/projects/NewsUseEnhancement/news_trimmed.xlsx")
    if not os.path.exists(xlsx_path):
        print(f"檔案不存在: {xlsx_path}")
        sys.exit(1)

    print(f"[{datetime.now(_TZ_TAIPEI).strftime('%H:%M:%S')}] 開始爬蟲，任務 ID: {os.getpid()}", flush=True)

    wb = LocalWB(xlsx_path)
    rows = get_pending(wb, BATCH_SIZE)
    if not rows:
        print(f"[{datetime.now(_TZ_TAIPEI).strftime('%H:%M:%S')}] 沒有待處理項目，結束。", flush=True)
        sys.exit(0)

    print(f"取得 {len(rows)} 筆待處理", flush=True)

    total_success = 0
    total_fail = 0
    batch_buf = []

    for i, row in enumerate(rows, 1):
        url = row["url"]
        domain = urlparse(url).netloc
        print(f"[{i}/{len(rows)}] {domain} - {row['title'][:30]}...", end=" ", flush=True)

        ok, text, method, cc, err = _scrape_single_url(url)
        fields = _build_fields(url, ok, text, method, cc, err, row["status"])

        if ok:
            print(f"[OK] {method} ({cc}字)", flush=True)
            total_success += 1
        else:
            print(f"[FAIL] {err} -> {fields['狀態']}", flush=True)
            total_fail += 1

        batch_buf.append((row["row"], fields))

        if len(batch_buf) >= WRITE_EVERY:
            for ri, fi in batch_buf:
                write_row(wb, ri, fi)
            wb.save()
            print(f"  → 已儲存 {len(batch_buf)} 筆 (累計 OK:{total_success} FAIL:{total_fail})", flush=True)
            batch_buf = []

        if i < len(rows):
            time.sleep(REQUEST_DELAY)

    # 寫入剩餘
    if batch_buf:
        for ri, fi in batch_buf:
            write_row(wb, ri, fi)
        wb.save()
        print(f"  → 已儲存剩餘 {len(batch_buf)} 筆", flush=True)

    print(f"\n=== 完成 ===", flush=True)
    print(f"成功: {total_success}  失敗: {total_fail}", flush=True)


if __name__ == "__main__":
    main()
