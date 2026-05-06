# -*- coding: utf-8 -*-
"""
Google Sheets 讀寫客戶端。

職責：
1. 讀取待處理的新聞列 (PENDING / RETRY_N)
2. 寫回擷取結果
3. 匯出資料到本地 Excel (在 GAS dailyClean 刪除前備份)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse

import gspread
from google.oauth2.service_account import Credentials

import config

# 台北時區
_TZ_TAIPEI = timezone(timedelta(hours=8))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_client() -> gspread.Client:
    """建立 gspread 客戶端，支援環境變數或本地 JSON 檔案。"""
    json_str = os.environ.get(config.GOOGLE_SERVICE_ACCOUNT_JSON_ENV)
    if json_str:
        info = json.loads(json_str)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    else:
        raise RuntimeError(
            f"找不到 Google 認證。請設定環境變數 {config.GOOGLE_SERVICE_ACCOUNT_JSON_ENV} "
            f"或放置 {config.GOOGLE_SERVICE_ACCOUNT_FILE} 檔案。"
        )
    return gspread.authorize(creds)


def _get_worksheet() -> gspread.Worksheet:
    """取得目標工作表。"""
    client = _get_client()
    spreadsheet = client.open_by_key(config.GOOGLE_SHEET_ID)
    return spreadsheet.worksheet(config.GOOGLE_SHEET_TAB)


# =============================================================================
# 讀取
# =============================================================================

def get_pending_rows(batch_size: int = config.BATCH_SIZE) -> list[dict]:
    """
    讀取需要處理的列，優先順序：PENDING > RETRY_1 > RETRY_2 > RETRY_3。

    回傳 list of dict，每個 dict 包含:
      - row_index: Sheet 中的列號 (1-based)
      - url: 真實網址
      - title: 新聞標題
      - status: 目前狀態
      - date: 新聞日期
    """
    ws = _get_worksheet()
    all_values = ws.get_all_values()

    if len(all_values) <= 1:
        return []

    rows = []
    for i, row in enumerate(all_values[1:], start=2):  # 跳過 header，列號從 2 開始
        # 確保有足夠的欄位
        status = row[config.COL["狀態"] - 1] if len(row) >= config.COL["狀態"] else ""
        if status not in config.ACTIONABLE_STATUSES:
            continue

        url = row[config.COL["真實網址"] - 1] if len(row) >= config.COL["真實網址"] else ""
        title = row[config.COL["標題"] - 1] if len(row) >= config.COL["標題"] else ""
        date = row[config.COL["日期"] - 1] if len(row) >= config.COL["日期"] else ""

        if not url:
            continue

        rows.append({
            "row_index": i,
            "url": url,
            "title": title,
            "status": status,
            "date": date,
        })

    # 排序：PENDING 優先，然後 RETRY_1 > RETRY_2 > RETRY_3
    # 使用大寫確保與 Google Sheets 實際寫入的狀態值一致
    priority = {config.STATUS_PENDING: 0, "RETRY_1": 1, "RETRY_2": 2, "RETRY_3": 3}
    rows.sort(key=lambda r: priority.get(r["status"], 99))

    return rows[:batch_size]


# =============================================================================
# 寫入
# =============================================================================

def _truncate_if_needed(field_name: str, value: str) -> str:
    """對內文等大欄位截斷，避免超出 Google Sheets 上限。"""
    if field_name == "內文" and len(value) > config.MAX_CONTENT_LENGTH:
        return value[:config.MAX_CONTENT_LENGTH]
    return value


def update_row(row_index: int, fields: dict) -> None:
    """
    更新 Sheet 中指定列的多個欄位。

    fields 的 key 對應 config.COL 的欄位名稱，例如：
      {"狀態": "DONE", "內文": "...", "擷取方法": "trafilatura", ...}

    使用 batch_update 減少 API 呼叫次數。
    """
    ws = _get_worksheet()
    cells_to_update = []

    for field_name, value in fields.items():
        col_index = config.COL.get(field_name)
        if col_index is None:
            continue
        str_value = str(value) if value else ""
        str_value = _truncate_if_needed(field_name, str_value)
        cell = gspread.Cell(row=row_index, col=col_index, value=str_value)
        cells_to_update.append(cell)

    if cells_to_update:
        ws.update_cells(cells_to_update, value_input_option="RAW")


def batch_update_rows(updates: list[tuple[int, dict]]) -> None:
    """
    批次更新多列。updates = [(row_index, fields_dict), ...]

    為減少 API 呼叫，一次送出所有 cell 更新。
    """
    ws = _get_worksheet()
    cells_to_update = []

    for row_index, fields in updates:
        for field_name, value in fields.items():
            col_index = config.COL.get(field_name)
            if col_index is None:
                continue
            str_value = str(value) if value else ""
            str_value = _truncate_if_needed(field_name, str_value)
            cell = gspread.Cell(
                row=row_index, col=col_index,
                value=str_value,
            )
            cells_to_update.append(cell)

    if cells_to_update:
        # gspread 的 update_cells 可一次更新多個不連續的 cell
        ws.update_cells(cells_to_update, value_input_option="RAW")


# =============================================================================
# 匯出備份 (在 GAS dailyClean 刪除資料前保存)
# =============================================================================

def export_to_excel(output_dir: str = "exports") -> Optional[str]:
    """
    匯出整份 Sheet 到本地 Excel 檔案作為備份。

    檔案命名：news_export_YYYYMMDD.xlsx
    回傳檔案路徑，若無資料則回傳 None。

    注意：GAS 的 dailyClean 設定在凌晨 04:00 (UTC+8) 執行，
    此函式應在 dailyClean 之前執行（建議 UTC+8 03:00 前，即 UTC 19:00）。
    """
    import openpyxl

    ws = _get_worksheet()
    all_values = ws.get_all_values()

    if len(all_values) <= 1:
        print("Sheet 無資料，跳過匯出。")
        return None

    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now(_TZ_TAIPEI)
    filename = f"news_export_{now.strftime('%Y%m%d')}.xlsx"
    filepath = os.path.join(output_dir, filename)

    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = config.GOOGLE_SHEET_TAB

    for row in all_values:
        sheet.append(row)

    wb.save(filepath)
    print(f"已匯出 {len(all_values) - 1} 筆資料到 {filepath}")
    return filepath


def get_sheet_stats() -> dict:
    """取得 Sheet 的狀態統計，用於報告。"""
    ws = _get_worksheet()
    all_values = ws.get_all_values()

    stats = {"total": len(all_values) - 1}
    for row in all_values[1:]:
        status = row[config.COL["狀態"] - 1] if len(row) >= config.COL["狀態"] else "UNKNOWN"
        stats[status] = stats.get(status, 0) + 1

    return stats
