# -*- coding: utf-8 -*-
"""
多層新聞全文擷取引擎。

每個 URL 依序嘗試三種方法，每層都獨立記錄成敗細節。
設計原則：同一個網域的不同頁面可能有不同結果，
所以診斷資訊以「單次 URL 嘗試」為粒度，而非網域。
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse

import requests
import trafilatura
from trafilatura.settings import use_config
import urllib3

import config
import content_cleaner

# 關閉 SSL 警告 (部分政府網站憑證有問題)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# trafilatura 全域設定
_trafil_config = use_config()
_trafil_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "50")


# =============================================================================
# 資料結構
# =============================================================================

@dataclass
class LayerAttempt:
    """單一擷取層的嘗試紀錄。"""
    method: str               # "trafilatura" / "requests+trafilatura" / "jina"
    success: bool = False
    http_status: Optional[int] = None
    char_count: int = 0
    error_type: str = ""      # Cloudflare / SSL / Timeout / SPA / ...
    error_detail: str = ""    # 更具體的錯誤訊息
    elapsed_sec: float = 0.0

    def to_short_str(self) -> str:
        if self.success:
            return f"{self.method}: OK ({self.char_count}字, {self.elapsed_sec:.1f}s)"
        status_part = f"HTTP {self.http_status}" if self.http_status else ""
        parts = [self.method, status_part, self.error_type, self.error_detail]
        return " | ".join(p for p in parts if p)


@dataclass
class ScrapeResult:
    """一個 URL 的完整擷取結果。"""
    url: str
    domain: str
    success: bool = False
    text: str = ""
    method: str = ""            # 最終成功的方法
    char_count: int = 0
    error_type: str = ""        # 最終失敗的錯誤分類
    attempts: list[LayerAttempt] = field(default_factory=list)

    def diagnostics_json(self) -> str:
        """產出 JSON 格式的診斷紀錄，方便人工檢視。"""
        return json.dumps(
            [asdict(a) for a in self.attempts],
            ensure_ascii=False,
            separators=(",", ":"),
        )


# =============================================================================
# 錯誤分類器
# =============================================================================

def _classify_error(
    e: Optional[Exception] = None,
    http_status: Optional[int] = None,
    response_headers: Optional[dict] = None,
    body_snippet: str = "",
) -> tuple[str, str]:
    """
    根據各種訊號判斷錯誤類型，回傳 (error_type, error_detail)。
    設計為盡可能細膩：同網站不同頁面可能有不同錯誤。
    """
    # 例外型錯誤
    if e is not None:
        err_str = str(e).lower()
        if "ssl" in err_str or "certificate" in err_str:
            return config.ERROR_SSL, str(e)[:120]
        if "timeout" in err_str or "timed out" in err_str:
            return config.ERROR_TIMEOUT, str(e)[:120]
        if "connectionerror" in err_str or "connection" in err_str:
            return config.ERROR_TIMEOUT, f"連線失敗: {str(e)[:100]}"
        return config.ERROR_UNKNOWN, str(e)[:150]

    # HTTP 狀態碼型錯誤
    headers_str = str(response_headers or {}).lower()
    if http_status == 403:
        if "cloudflare" in headers_str or "cf-ray" in headers_str:
            return config.ERROR_CLOUDFLARE, "Cloudflare 403 Challenge"
        return config.ERROR_BLOCKED, f"HTTP 403 Forbidden"
    if http_status == 451:
        return config.ERROR_BLOCKED, "HTTP 451 地區/法律限制"
    if http_status == 404:
        return config.ERROR_HTTP, "HTTP 404 頁面不存在"
    if http_status and http_status >= 400:
        return config.ERROR_HTTP, f"HTTP {http_status}"

    # 內容型錯誤
    body_lower = body_snippet.lower()
    if any(kw in body_lower for kw in ["loading...", "please enable javascript", "noscript"]):
        return config.ERROR_SPA, "需要 JavaScript 渲染"
    if any(kw in body_lower for kw in ["subscribe", "付費", "premium", "paywall"]):
        return config.ERROR_PAYWALL, "疑似付費牆"

    return config.ERROR_EMPTY, "擷取結果為空或過短"


def _is_valid_content(text: Optional[str], min_chars: int = 50) -> bool:
    """判斷擷取的文字是否有效（不是 loading 畫面等）。"""
    if not text or len(text) < min_chars:
        return False
    # 過濾假陽性（只抓到 loading 或 menu）
    noise_keywords = ["loading...", "please enable javascript", "該頁面暫無內容"]
    text_lower = text.lower()
    if any(kw in text_lower for kw in noise_keywords):
        return False
    return True


# =============================================================================
# 三層擷取方法
# =============================================================================

def _try_trafilatura_native(url: str) -> LayerAttempt:
    """第 1 層：trafilatura 內建的 fetch + extract。"""
    attempt = LayerAttempt(method="trafilatura")
    start = time.time()
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded is None:
            attempt.error_type, attempt.error_detail = config.ERROR_EMPTY, "fetch_url 回傳 None"
            attempt.elapsed_sec = time.time() - start
            return attempt

        text = trafilatura.extract(
            downloaded,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            config=_trafil_config,
        )

        if _is_valid_content(text):
            attempt.success = True
            attempt.text = text[:config.MAX_CONTENT_LENGTH]
            attempt.char_count = len(text)
        else:
            attempt.error_type, attempt.error_detail = _classify_error(
                body_snippet=(text or "")[:500]
            )

    except Exception as e:
        attempt.error_type, attempt.error_detail = _classify_error(e=e)

    attempt.elapsed_sec = time.time() - start
    return attempt


def _try_requests_trafilatura(url: str) -> LayerAttempt:
    """第 2 層：自訂 headers 的 requests + trafilatura extract。"""
    attempt = LayerAttempt(method="requests+trafilatura")
    start = time.time()
    try:
        resp = requests.get(
            url,
            headers=config.BROWSER_HEADERS,
            timeout=config.TIMEOUT_REQUESTS,
            verify=False,  # 處理 SSL 問題的網站
            allow_redirects=True,
        )
        attempt.http_status = resp.status_code

        if resp.status_code != 200 or len(resp.text) < 500:
            attempt.error_type, attempt.error_detail = _classify_error(
                http_status=resp.status_code,
                response_headers=dict(resp.headers),
                body_snippet=resp.text[:500],
            )
            attempt.elapsed_sec = time.time() - start
            return attempt

        text = trafilatura.extract(
            resp.text,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            config=_trafil_config,
        )

        if _is_valid_content(text):
            attempt.success = True
            attempt.text = text[:config.MAX_CONTENT_LENGTH]
            attempt.char_count = len(text)
        else:
            attempt.error_type, attempt.error_detail = _classify_error(
                body_snippet=(text or resp.text[:500])
            )

    except Exception as e:
        attempt.error_type, attempt.error_detail = _classify_error(e=e)

    attempt.elapsed_sec = time.time() - start
    return attempt


def _try_jina_reader(url: str) -> LayerAttempt:
    """第 3 層：Jina Reader API 備援。"""
    attempt = LayerAttempt(method="jina")
    start = time.time()
    try:
        jina_url = f"{config.JINA_BASE_URL}{url}"
        headers = {"Accept": "text/plain", "X-No-Cache": "true"}
        if config.JINA_API_KEY:
            headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"

        resp = requests.get(jina_url, headers=headers, timeout=config.TIMEOUT_JINA)
        attempt.http_status = resp.status_code

        if resp.status_code != 200:
            attempt.error_type, attempt.error_detail = _classify_error(
                http_status=resp.status_code,
                response_headers=dict(resp.headers),
            )
            attempt.elapsed_sec = time.time() - start
            return attempt

        # Jina 回傳的格式：Title: ...\nURL Source: ...\nMarkdown Content:\n...
        raw_text = resp.text
        # 移除 Jina metadata 行，只保留實質內容
        lines = raw_text.split("\n")
        content_lines = []
        skip_header = True
        for line in lines:
            if skip_header and line.startswith(("Title:", "URL Source:", "Markdown Content:", "===")):
                continue
            skip_header = False
            content_lines.append(line)
        text = "\n".join(content_lines).strip()

        if _is_valid_content(text):
            attempt.success = True
            attempt.text = text[:config.MAX_CONTENT_LENGTH]
            attempt.char_count = len(text)
        else:
            attempt.error_type, attempt.error_detail = _classify_error(
                body_snippet=text[:500]
            )

    except Exception as e:
        attempt.error_type, attempt.error_detail = _classify_error(e=e)

    attempt.elapsed_sec = time.time() - start
    return attempt


# =============================================================================
# 主要擷取函式
# =============================================================================

# 擷取層順序
_LAYERS = [
    _try_trafilatura_native,
    _try_requests_trafilatura,
    _try_jina_reader,
]


def scrape_url(url: str) -> ScrapeResult:
    """
    對單一 URL 執行多層 fallback 擷取。

    依序嘗試每一層，第一個成功的就停止。
    所有嘗試（含成功和失敗）都記錄在 result.attempts 中，
    提供完整的診斷資訊供人工檢視。
    """
    domain = urlparse(url).netloc
    result = ScrapeResult(url=url, domain=domain)

    for layer_fn in _LAYERS:
        attempt = layer_fn(url)
        result.attempts.append(attempt)

        if attempt.success:
            result.success = True
            result.text = content_cleaner.clean_content(attempt.text)
            result.method = attempt.method
            result.char_count = len(result.text)
            return result

        # 如果是確定無法重試的錯誤 (404)，提前結束不繼續嘗試
        if attempt.error_type == config.ERROR_HTTP and "404" in attempt.error_detail:
            break

    # 所有層都失敗 — 取最後一層的錯誤分類
    last_attempt = result.attempts[-1]
    result.error_type = last_attempt.error_type
    return result
