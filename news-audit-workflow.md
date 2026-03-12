# Google Alerts 新聞蒐集與內部稽核風險分析系統：工作流設計與可行性評估

---

## 一、需求拆解與整體架構

整體系統可拆解為兩大模組：

**模組 A — 新聞擷取與結構化儲存**（資料工程層）
**模組 B — LLM 驅動的分類、風險萃取與稽核建議**（智慧分析層）

```
┌─────────────────────────────────────────────────────────────────────┐
│                        整體工作流程                                  │
│                                                                     │
│  Google Alerts Email                                                │
│        │                                                            │
│        ▼                                                            │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────────────┐     │
│  │ 1.解析信件    │───▶│ 2.解析Google │───▶│ 3.爬蟲擷取全文     │     │
│  │   擷取連結   │    │   轉址取真實 │    │   (Readability     │     │
│  │              │    │   URL        │    │    清洗廣告雜訊)   │     │
│  └──────────────┘    └──────────────┘    └────────┬───────────┘     │
│                                                    │                │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ 4.寫入 Google    │       │
│                                          │   Sheets / Excel │       │
│                                          └────────┬─────────┘       │
│                                                   │                 │
│                            ┌───────────────────────┘                │
│                            ▼                                        │
│  ┌─────────────────────────────────────────────────────────┐        │
│  │              模組 B：LLM 智慧分析層                      │        │
│  │                                                         │        │
│  │  5.批次分類     6.事件萃取      7.稽核風險       8.產出  │        │
│  │  (新聞/公告/   (去重+關聯     辨識與建議       週報/    │        │
│  │   影片/其他)    事件群組)      (切入角度+       摘要     │        │
│  │                               作業流程)                 │        │
│  └─────────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、模組 A：新聞擷取與結構化儲存

### 步驟 1：取得 Google Alerts 連結

**方案比較：**

| 方案 | 說明 | 優點 | 缺點 |
|------|------|------|------|
| **A1：Gmail API 自動擷取** | 透過 Gmail API 讀取 Google Alerts 信件，解析 HTML 取出連結 | 全自動、即時 | 需 Google OAuth 設定 |
| **A2：RSS Feed** | Google Alerts 可設定 RSS 輸出，用 feedparser 解析 | 簡單、不需驗證 | Google 可能限制 RSS 功能 |
| **A3：手動貼上連結** | 每天手動將連結貼到指定欄位 | 零技術門檻 | 費時、易遺漏 |

**建議：** 優先嘗試 A2（RSS Feed），若不可用則採 A1（Gmail API）。

**技術實作（RSS 方案）：**
```python
import feedparser

# Google Alerts RSS URL（在 Google Alerts 設定中選擇「RSS 動態消息」）
feeds = [
    "https://www.google.com/alerts/feeds/XXXXXX/YYYYYY",  # 中科院
    "https://www.google.com/alerts/feeds/XXXXXX/ZZZZZZ",  # 國防軍事
    "https://www.google.com/alerts/feeds/XXXXXX/WWWWWW",  # 資訊安全
]

for feed_url in feeds:
    feed = feedparser.parse(feed_url)
    for entry in feed.entries:
        title = entry.title
        link = entry.link        # 這是 Google 轉址 URL
        published = entry.published
```

### 步驟 2：解析 Google 轉址取得真實 URL

Google Alerts 的連結格式通常為：
`https://www.google.com/url?...&url=<真實URL>&...`

```python
from urllib.parse import urlparse, parse_qs
import requests

def resolve_google_redirect(google_url: str) -> str:
    """解析 Google 轉址 URL，取得真實網址"""
    parsed = urlparse(google_url)
    params = parse_qs(parsed.query)
    
    # 方法一：直接從 query string 解析
    if 'url' in params:
        return params['url'][0]
    
    # 方法二：實際 follow redirect
    try:
        resp = requests.head(google_url, allow_redirects=True, timeout=10)
        return resp.url
    except:
        return google_url
```

### 步驟 3：爬蟲擷取全文並清洗

**核心挑戰：** 新聞網站結構各異、廣告干擾多、有些需要 JavaScript 渲染。

**技術棧選擇：**

| 工具 | 用途 | 適用情境 |
|------|------|----------|
| **requests + BeautifulSoup** | 靜態網頁擷取 | 多數新聞網站 |
| **newspaper3k / trafilatura** | 新聞專用擷取（自動去廣告） | 推薦首選 |
| **Playwright / Selenium** | 動態渲染網頁 | JavaScript 重度網站 |
| **yt-dlp** | YouTube 影片資訊擷取 | 影片類連結 |
| **Jina Reader API** | 免費 API 將網頁轉為乾淨文字 | 簡單快速方案 |

**建議方案：trafilatura 為主 + Jina Reader API 為備援**

```python
import trafilatura
import requests
from urllib.parse import urlparse

def classify_and_extract(url: str) -> dict:
    """依據 URL 類型分類並擷取內容"""
    domain = urlparse(url).netloc
    
    # 影片類
    if any(d in domain for d in ['youtube.com', 'youtu.be']):
        return extract_video_info(url)
    
    # 新聞/一般網頁
    try:
        # 方案一：trafilatura（自動去廣告、提取正文）
        downloaded = trafilatura.fetch_url(url)
        result = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            output_format='json'   # 含標題、作者、日期等 metadata
        )
        if result:
            return json.loads(result)
    except:
        pass
    
    # 方案二（備援）：Jina Reader API（免費額度）
    try:
        jina_url = f"https://r.jina.ai/{url}"
        resp = requests.get(jina_url, timeout=15)
        return {"text": resp.text, "source": url}
    except:
        return {"text": "[擷取失敗]", "source": url}
```

### 步驟 4：寫入 Google Sheets / Excel

**Google Sheets 方案（推薦用於日常查找）：**
```python
import gspread
from google.oauth2.service_account import Credentials

# 使用 Service Account
creds = Credentials.from_service_account_file(
    'service_account.json',
    scopes=['https://www.googleapis.com/auth/spreadsheets']
)
gc = gspread.authorize(creds)
sheet = gc.open("Google Alerts 新聞蒐集").sheet1

# 欄位結構
# | 日期 | 來源 | 標題 | 分類 | 摘要 | 全文 | 原始連結 | 擷取狀態 |
row = [
    date_str,           # 發布日期
    source_domain,      # 來源網站
    title,              # 新聞標題
    "",                 # 分類（後續 LLM 填入）
    "",                 # 摘要（後續 LLM 填入）
    full_text[:50000],  # 全文（Google Sheets 單格上限 50,000 字元）
    original_url,       # 原始連結
    "成功"              # 擷取狀態
]
sheet.append_row(row)
```

**Excel 備援方案（用於大量資料或離線場景）：**
```python
import openpyxl

# 同樣欄位結構，但無字數上限
wb = openpyxl.load_workbook('news_archive.xlsx')
ws = wb.active
ws.append(row)
wb.save('news_archive.xlsx')
```

**欄位設計建議：**

| 欄位 | 說明 | 填入時機 |
|------|------|----------|
| 日期 | 新聞發布日期 | 爬蟲階段 |
| 來源 | 來源網站域名 | 爬蟲階段 |
| 標題 | 新聞標題 | 爬蟲階段 |
| 內容類型 | 新聞/影片/公告/其他 | LLM 分類 |
| 主題分類 | 中科院/國防/資安/其他 | LLM 分類 |
| 摘要 | 100-200 字重點摘要 | LLM 生成 |
| 關鍵事件 | 萃取出的核心事件 | LLM 生成 |
| 稽核相關性 | 高/中/低/無 | LLM 評估 |
| 全文 | 清洗後的完整內容 | 爬蟲階段 |
| 原始連結 | 可追溯原始來源 | 爬蟲階段 |
| 擷取狀態 | 成功/失敗/部分 | 爬蟲階段 |

---

## 三、模組 B：LLM 驅動的智慧分析

### 整體分析流程（非逐篇分析）

```
全部新聞（例如今日 30 篇）
        │
        ▼
   ┌─────────┐
   │ 第一輪   │  只送「標題 + 摘要」給 LLM
   │ 批次分類 │  將 30 篇分為 5-8 個主題群組
   └────┬────┘
        │
        ▼
   ┌─────────┐
   │ 第二輪   │  每個群組內，送「全文」給 LLM
   │ 事件萃取 │  合併重複報導 → 萃取核心事件
   └────┬────┘
        │
        ▼
   ┌──────────┐
   │ 第三輪    │  針對高相關性事件
   │ 稽核分析  │  辨識風險 + 建議稽核切入角度
   └──────────┘
```

### 第一輪 Prompt：批次分類

```markdown
## System Prompt

你是一位熟悉台灣國防產業、資訊安全及公部門治理的內部稽核分析師。
你的任務是將以下新聞進行初步分類與篩選。

## 分類規則

請將每則新聞歸入以下類別之一：
1. **國防科技研發** — 中科院、軍備局、國防自主研發相關
2. **軍事採購與預算** — 國防預算、軍購案、招標案相關
3. **資訊安全事件** — 資安攻擊、個資外洩、網路威脅相關
4. **資安政策法規** — 資通安全管理法、法規修訂、合規要求
5. **國防人事與組織** — 人事異動、組織調整、人才培訓
6. **國際情勢與軍事動態** — 區域安全、軍事演習、外交關係
7. **其他** — 不屬於以上類別

同時判斷「內容類型」：新聞報導 / 影音內容 / 政府公告 / 學術研究 / 其他

## 輸入資料

以下是今日蒐集的新聞清單：

{每則新聞的「編號 + 標題 + 來源 + 前200字摘要」}

## 輸出格式

請以 JSON 格式輸出：
```json
{
  "classification_date": "2025-XX-XX",
  "total_articles": 30,
  "groups": [
    {
      "category": "資訊安全事件",
      "articles": [1, 5, 12, 18],
      "key_theme": "本週多起政府機關遭釣魚攻擊事件",
      "audit_relevance": "高",
      "audit_relevance_reason": "涉及組織資安防護措施有效性"
    }
  ],
  "excluded": [
    {"article_id": 7, "reason": "影片內容無實質新聞價值"}
  ]
}
```
```

### 第二輪 Prompt：事件萃取與去重

```markdown
## System Prompt

你是一位資深內部稽核分析師，專精於從新聞群組中萃取核心事件。
以下是同一主題類別「{category_name}」下的多篇新聞全文。

## 任務

1. **合併重複報導**：多家媒體報導同一事件時，合併為單一事件紀錄
2. **萃取核心事件**：每個事件包含：
   - 事件名稱（簡潔描述）
   - 事件摘要（200字以內）
   - 關鍵時間點
   - 涉及的機關/組織/人物
   - 事件影響範圍
   - 資訊可信度（高/中/低，依據報導來源數量與品質）
3. **建立事件關聯**：若事件間有因果或關聯關係，請標註

## 輸入資料

{該類別下所有新聞的全文}

## 輸出格式

```json
{
  "category": "資訊安全事件",
  "events": [
    {
      "event_id": "EVT-20250101-001",
      "event_name": "某政府機關遭 APT 攻擊資料外洩",
      "summary": "...",
      "timeline": ["2025-01-01: 攻擊發生", "2025-01-02: 事件曝光"],
      "entities": ["某機關", "某資安公司"],
      "impact_scope": "涉及約5萬筆個資",
      "source_articles": [1, 5, 12],
      "credibility": "高",
      "related_events": ["EVT-20250101-002"]
    }
  ]
}
```
```

### 第三輪 Prompt：內部稽核風險分析（核心 Prompt）

```markdown
## System Prompt

你是一位擁有 CIA（國際內部稽核師）認證的資深稽核專家，專精於國防
產業及資訊安全領域。你深入了解 COSO 內部控制架構、IIA 國際內部稽核
準則，以及台灣《資通安全管理法》等相關法規。

## 你的任務

根據以下萃取出的事件，從內部稽核的角度進行風險分析，並提供具體的
稽核建議。你的分析應幫助內部稽核人員判斷「我們公司/組織是否存在
類似的風險或控制缺失」。

## 分析架構

對每個事件，請依下列架構進行分析：

### 1. 風險辨識
- 這個事件揭示了什麼類型的風險？
  - 作業風險 / 合規風險 / 財務風險 / 策略風險 / 資訊科技風險
- 根本原因推測（Root Cause Analysis）
- 風險發生的前提條件

### 2. 對標自身組織的適用性
- 我們組織是否有類似的作業流程或系統？
- 我們是否受相同法規或合約要求約束？
- 哪些部門或業務最可能受到類似風險影響？

### 3. 建議的稽核切入角度
- 具體的稽核目標（Audit Objective）
- 建議審視的作業流程清單
- 需要調閱的關鍵文件與資料
- 建議訪談的對象

### 4. 控制測試建議
- 應測試哪些既有的內部控制措施
- 測試方法（查核、觀察、訪談、重新執行）
- 預期的控制設計 vs. 可能的缺失

### 5. 優先順序與急迫性
- 風險等級（高/中/低）
- 建議的處理時程
- 是否需要立即通報管理階層

## 輸入資料

{第二輪萃取出的事件清單}

## 輸出格式

```json
{
  "analysis_date": "2025-XX-XX",
  "events_analyzed": 5,
  "audit_recommendations": [
    {
      "event_ref": "EVT-20250101-001",
      "risk_type": ["資訊科技風險", "合規風險"],
      "risk_description": "...",
      "root_cause_hypothesis": "...",
      "applicability_to_org": {
        "relevance": "高",
        "similar_processes": ["電子郵件系統管理", "端點防護機制"],
        "affected_departments": ["資訊部", "人事處"],
        "applicable_regulations": ["資通安全管理法第X條"]
      },
      "audit_approach": {
        "objective": "評估組織防範 APT 攻擊的控制措施有效性",
        "processes_to_review": [
          "資安事件通報與應變程序",
          "電子郵件過濾與防護機制",
          "員工資安意識訓練執行情形"
        ],
        "documents_to_request": [
          "資安事件通報紀錄（近6個月）",
          "郵件安全閘道器設定與紀錄",
          "資安教育訓練出席紀錄與測驗結果"
        ],
        "interviewees": ["資安長", "郵件系統管理員", "人資教育訓練負責人"]
      },
      "control_tests": [
        {
          "control": "電子郵件惡意連結過濾",
          "test_method": "重新執行 — 發送測試釣魚信件驗證過濾率",
          "expected_design": "系統應攔截已知惡意連結並警示使用者",
          "potential_gap": "過濾規則可能未及時更新，或僅比對已知特徵"
        }
      ],
      "priority": {
        "risk_level": "高",
        "urgency": "建議於本季度納入稽核計畫",
        "escalation_needed": false
      }
    }
  ],
  "cross_event_insights": "本期多起資安事件顯示...",
  "suggested_audit_plan_updates": "建議將...納入年度稽核計畫"
}
```
```

---

## 四、技術棧總覽與比較

### 方案一：低技術門檻方案（推薦入門）

適合對象：程式基礎有限，希望快速上手

| 元件 | 工具 | 說明 |
|------|------|------|
| 新聞擷取 | Jina Reader API | 免費 API，URL 前加 `r.jina.ai/` 即可取得乾淨文字 |
| 自動化排程 | Google Apps Script | 內建於 Google 生態系，免額外部署 |
| 資料儲存 | Google Sheets | 直接在 Sheets 中操作 |
| LLM 分析 | Claude API / ChatGPT API | 透過 Apps Script 呼叫 |
| 整合介面 | Google Sheets + Sidebar | 用 Apps Script 打造簡易操作介面 |

**自動化程度：** ★★★☆☆（約 70%，需手動觸發分析）

### 方案二：中階自動化方案（推薦長期使用）

適合對象：有基礎 Python 能力，追求穩定自動化

| 元件 | 工具 | 說明 |
|------|------|------|
| 新聞擷取 | Python + trafilatura | 開源、高品質文章提取 |
| URL 解析 | requests + urllib | 解析 Google 轉址 |
| 排程 | cron job / GitHub Actions | 每日自動執行 |
| 資料儲存 | Google Sheets API + 本地 SQLite | 雙重備份 |
| LLM 分析 | Claude API (claude-sonnet-4) | 性價比最佳 |
| 部署 | 本地電腦 / VPS / Google Cloud Run | 視預算選擇 |

**自動化程度：** ★★★★☆（約 90%，可全自動）

### 方案三：高階企業方案

適合對象：團隊使用，需要完整資料治理

| 元件 | 工具 | 說明 |
|------|------|------|
| 資料擷取 | Scrapy + Playwright | 處理各種複雜網站 |
| 資料儲存 | PostgreSQL + Elasticsearch | 全文檢索、進階查詢 |
| LLM 編排 | LangChain / LlamaIndex | 複雜 Prompt 鏈管理 |
| 排程 | Airflow / Prefect | 企業級工作流排程 |
| 前端 | Streamlit / Gradio | 互動式分析儀表板 |
| 部署 | Docker + Cloud | 容器化部署 |

**自動化程度：** ★★★★★（100%，含監控告警）

---

## 五、執行成本分析

### 假設條件
- 每日新聞量：約 20-50 則
- 每月工作日：22 天
- 平均每篇新聞全文：約 1,500 字（約 750 tokens）

### 方案一成本（低技術門檻）

| 項目 | 月費用（TWD） | 說明 |
|------|---------------|------|
| Jina Reader API | $0 | 免費額度通常足夠 |
| Google Sheets | $0 | Google 帳號即可 |
| Claude API (Sonnet) | $150-500 | 依分析量而定（見下方估算） |
| Google Apps Script | $0 | 內建免費 |
| **合計** | **$150-500/月** | |

### 方案二成本（中階自動化）

| 項目 | 月費用（TWD） | 說明 |
|------|---------------|------|
| VPS（如果不用本機） | $150-500 | 最低規格即可 |
| Claude API (Sonnet) | $150-500 | 主要分析引擎 |
| Google Sheets API | $0 | 免費配額充足 |
| 域名（選用） | $30 | 如需固定網址 |
| **合計** | **$200-1,000/月** | |

### 方案三成本（高階企業方案）

| 項目 | 月費用（TWD） | 說明 |
|------|---------------|------|
| 雲端主機 | $1,500-5,000 | 含資料庫、運算 |
| Claude API | $500-2,000 | 高量分析 |
| Elasticsearch | $3,000+ | 雲端託管方案 |
| **合計** | **$5,000-10,000/月** | |

### LLM API 費用詳細估算（以 Claude Sonnet 為例）

```
每日流程：
  第一輪（分類）：30 篇 × 200字摘要 = 6,000字 輸入 ≈ 3,000 tokens
                  + 系統提示 ≈ 1,000 tokens
                  + 輸出 ≈ 2,000 tokens
                  
  第二輪（事件萃取）：假設分為 5 組，每組 6 篇全文
                      5 × (6 × 1,500字) = 45,000字 ≈ 22,500 tokens 輸入
                      + 輸出 ≈ 5,000 tokens
                      
  第三輪（稽核分析）：5 個事件深度分析
                      輸入 ≈ 5,000 tokens
                      + 輸出 ≈ 8,000 tokens（詳細建議）

  每日 tokens 合計：≈ 輸入 31,500 + 輸出 15,000 = 46,500 tokens
  
  每月（22 天）：≈ 1,023,000 tokens

  Claude Sonnet 定價（約）：
    輸入：$3 / 1M tokens ≈ $2.08/月
    輸出：$15 / 1M tokens ≈ $4.95/月
    
  每月 LLM 費用 ≈ $7 USD ≈ TWD $230
```

> 實際費用可能因 Prompt 長度、分析深度而有 2-3 倍浮動，但整體而言 LLM API 費用非常低廉。

---

## 六、建議實施路徑

### Phase 1（第 1-2 週）：驗證可行性

- 用 Google Apps Script 手動測試 5-10 則連結的擷取流程
- 測試 Jina Reader API 對台灣主要新聞網站的擷取品質
- 在 Claude.ai 上手動測試三輪 Prompt 的分析效果
- 確認 Google Sheets 的欄位設計是否符合需求

### Phase 2（第 3-4 週）：建立基本自動化

- 完成 Google Alerts → RSS → 擷取 → Sheets 的自動化流程
- 將三輪 Prompt 串接為 Claude API 自動呼叫
- 設定每日排程（Google Apps Script 的觸發條件或 cron）
- 建立錯誤處理與通知機制

### Phase 3（第 2-3 個月）：優化與擴展

- 根據實際使用經驗調整 Prompt（最重要的持續優化）
- 建立歷史事件資料庫，支援趨勢分析
- 加入「與過往稽核發現比對」的功能
- 考慮是否需要升級到方案二

---

## 七、風險與限制

| 風險 | 影響 | 緩解措施 |
|------|------|----------|
| 新聞網站反爬蟲 | 擷取失敗 | 多方案備援（trafilatura → Jina → 手動） |
| Google Alerts 格式變更 | 連結解析失敗 | 模組化設計，易於更新解析邏輯 |
| LLM 幻覺（Hallucination） | 產出錯誤分析 | 始終附上原文連結，人工覆核高風險項目 |
| Google Sheets 容量限制 | 超過 1,000 萬格上限 | 定期歸檔到 Excel，或升級到資料庫方案 |
| 付費牆（Paywall） | 部分新聞無法取得全文 | 記錄失敗項目，手動補充或標記為「僅標題」 |
| LLM 稽核建議的專業性 | 建議可能過於泛化 | 在 Prompt 中加入組織特定脈絡，人工審查建議 |

---

## 八、關鍵成功因素

1. **Prompt 的持續迭代**：根據實際分析結果不斷調校 Prompt，加入你組織的特定脈絡（產業別、已知風險、既有控制措施），這是效果好壞的決定性因素。

2. **人機協作而非全自動**：LLM 負責初篩與建議，內部稽核人員負責專業判斷與決策。LLM 的角色是「分析助理」而非「替代稽核師」。

3. **資料品質把關**：垃圾進、垃圾出。擷取階段的品質直接影響後續分析，建議初期每天花 5 分鐘快速瀏覽擷取結果。

4. **與既有稽核流程整合**：分析結果應能直接對應到年度稽核計畫、風險評估矩陣等既有工具，而非獨立運作。
