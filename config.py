# -*- coding: utf-8 -*-
"""集中管理所有設定常數。修改行為只需要改這個檔案。"""
import os

# =============================================================================
# Google Sheets 設定
# =============================================================================
# 從 Google Sheet URL 中取得: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit
GOOGLE_SHEET_ID = os.environ.get(
    "GOOGLE_SHEET_ID",
    os.environ.get("GOOGLE_SHEET_ID_LOCAL", "")  # 本地測試請設環境變數或在此填入
)
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "news_sheet1")

# 認證方式：GitHub Actions 用環境變數，本地用 JSON 檔案
GOOGLE_SERVICE_ACCOUNT_JSON_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
GOOGLE_SERVICE_ACCOUNT_FILE = "service_account.json"

# =============================================================================
# Google Sheets 欄位對應 (1-based index)
# 與 GAS 腳本寫入的欄位保持一致，H-L 為 Python 新增欄位
# =============================================================================
COL = {
    "日期":       1,   # A - GAS 寫入
    "標題":       2,   # B - GAS 寫入
    "短網址":     3,   # C - GAS 寫入
    "新聞網址":   4,   # D - GAS 寫入 (Google 轉址 URL)
    "真實網址":   5,   # E - GAS 寫入 (解析後的真實 URL)
    "狀態":       6,   # F - GAS 初始寫入 PENDING，Python 更新
    "內文":       7,   # G - Python 寫入擷取的全文
    "擷取方法":   8,   # H - Python: trafilatura / req+trafil / jina
    "診斷資訊":   9,   # I - Python: 每次嘗試的詳細紀錄 (JSON)
    "最後嘗試":  10,   # J - Python: ISO 8601 timestamp
    "字數":      11,   # K - Python: 擷取到的字數
    "網域":      12,   # L - Python: 自動填入 URL 的 domain
}

# =============================================================================
# 擷取策略
# =============================================================================
# 每次執行最多處理幾筆 (GitHub Actions 建議 100，本地可調高)
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "250"))

# 每筆之間的延遲秒數 (禮貌爬蟲)
REQUEST_DELAY = 1.5

# 各層 HTTP 超時秒數
TIMEOUT_TRAFILATURA = 15
TIMEOUT_REQUESTS = 15
TIMEOUT_JINA = 25

# 最大重試次數 (PENDING → RETRY_1 → RETRY_2 → RETRY_3 → FAILED)
MAX_RETRIES = 3

# 全文最大長度 (Google Sheets 單格上限 50,000 字元)
MAX_CONTENT_LENGTH = 49000

# Jina Reader API（結尾斜線已確保）
JINA_BASE_URL = "https://r.jina.ai/"
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")  # 選用，有 key 可提升配額

# =============================================================================
# 狀態值定義
# =============================================================================
STATUS_PENDING = "PENDING"
STATUS_DONE = "DONE"
STATUS_TITLE_ONLY = "TITLE_ONLY"
STATUS_FAILED = "FAILED"
# RETRY_1, RETRY_2, RETRY_3 由程式動態產生

# 這些狀態代表「需要處理」
ACTIONABLE_STATUSES = {
    STATUS_PENDING, "RETRY_1", "RETRY_2", "RETRY_3"
}

# =============================================================================
# 錯誤分類
# =============================================================================
ERROR_CLOUDFLARE = "Cloudflare"
ERROR_SSL = "SSL"
ERROR_TIMEOUT = "Timeout"
ERROR_SPA = "SPA/JS渲染"
ERROR_EMPTY = "內容為空"
ERROR_HTTP = "HTTP錯誤"
ERROR_PAYWALL = "付費牆"
ERROR_BLOCKED = "被封鎖"
ERROR_UNKNOWN = "未知錯誤"

# =============================================================================
# 內文清洗規則 (content_cleaner.py 使用)
# =============================================================================

# Rule A: 尾部截斷 — 出現時截斷該行及之後所有內容
TAIL_TRUNCATION_KEYWORDS = [
    "延伸閱讀",
    "相關新聞",
    "熱門新聞",
    "看更多",
    "請繼續下滑閱讀",
    "看更多報導",
]

# Rule B: 段落移除 — 移除包含這些關鍵字的整行
PARAGRAPH_REMOVAL_KEYWORDS = [
    "廣告",
    "Cookie",
    "cookie",
    "隱私權政策",
    "使用條款",
    "加入會員",
    "下載APP",
    "不用抽 不用搶",
    "點我下載",
    "保證天天中獎",
]

# Rule C: 社群元素（完整片語）— 全文任何位置安全匹配
SOCIAL_PATTERNS_EXACT = [
    "追蹤 Instagram",
    "追蹤 Facebook",
    "訂閱 Google News",
    "按讚加入",
    "分享至",
    "歡迎用「轉貼」或「分享」",
]

# Rule C: 社群元素（短詞）— 僅文末 200 字內匹配，避免誤刪正文
# 這些短詞太常見於正文中（如「轉發」「分享」），所以只 在文章末尾 200 字範圍內移除
SOCIAL_PATTERNS_TAIL_ONLY: list[str] = [
    "轉發",
    "分享",
    "粉絲團",
    "LINE",
    "LINE TODAY",
    "©",
    "版權所有",
    "follow",
    "Subscribe",
    "訂閱",
]

# Rule D: 尾部連續列表清理的最少行數門檻
TRAILING_LIST_MIN_LINES = 3

# =============================================================================
# HTTP Headers (模擬一般瀏覽器)
# =============================================================================
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}
