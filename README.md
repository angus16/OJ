# OJ：AI 智慧錯誤診斷系統

基於開源 OnlineJudge 2.0 開發，針對 Python 程式設計初學者提供即時 AI 除錯引導。

## 系統簡介

傳統線上評測系統在學生提交錯誤程式碼後，只回傳通過或失敗，學生無法得知錯在哪裡、也不知道如何修正。本系統在判題完成後，自動分析錯誤類型並同時呼叫三家 AI，根據學生的嘗試次數給予漸進式引導提示，同時追蹤每位學生在各知識點的學習狀況。

## 修改說明

| 檔案 | 類型 | 說明 |
|---|---|---|
| utils/tutor_engine.py | 新增 | 三家 AI（Gemini、Claude、ChatGPT）診斷引擎，包含漸進式提示邏輯 |
| judge/dispatcher.py | 修改 | 判題完成後觸發 AI 診斷、靜態分析、錯誤分類、知識追蹤 |
| submission/models.py | 修改 | 新增 ai_hint、error_type、problem_tags 欄位 |
| docker-compose.yml | 修改 | 新增 API 金鑰環境變數（金鑰請自行填入） |

## 系統功能

- **三家 AI 同時診斷**：同時呼叫 Gemini、Claude、ChatGPT，各自生成引導提示，可比較三家 AI 的提示風格差異
- **漸進式鷹架**：根據學生對該題的累計嘗試次數動態調整提示深度，第 1 次只給方向，第 4 次以上給填空式詳細引導，始終不直接給出答案
- **靜態程式分析**：整合 AST 語法解析與 flake8 風格檢查，分析結果附加至 AI Prompt 提升提示精準度
- **錯誤類型自動分類**：自動將每次提交分類為 SYNTAX / LOGIC / RUNTIME / STYLE
- **知識點追蹤**：以題目 tag 為知識單元，記錄每位學生的嘗試次數、錯誤分布、連續答對次數、再犯率與掌握度

## 新增資料庫欄位

### submission 表
| 欄位 | 說明 |
|---|---|
| ai_hint | 三家 AI 提示與 error_log |
| error_type | 錯誤類型（SYNTAX/LOGIC/RUNTIME/STYLE） |
| problem_tags | 題目對應知識點 |

### student_knowledge_state 表（新增）
| 欄位 | 說明 |
|---|---|
| user_id + knowledge_tag | 學生與知識點（聯合唯一鍵） |
| attempt_count | 總嘗試次數 |
| correct_count | 答對次數 |
| error_count | 答錯次數 |
| syntax_error_count | 語法錯誤次數 |
| logic_error_count | 邏輯錯誤次數 |
| runtime_error_count | 執行錯誤次數 |
| consecutive_correct | 連續答對次數 |
| recurrence_count | 再犯次數 |
| mastery_level | 掌握度 0.0~1.0 |
| response_sequence | 對錯序列（供未來 DKT 模型使用） |

## 環境變數設定

在 docker-compose.yml 的 oj-backend 服務下填入：

    GEMINI_API_KEY=你的金鑰
    ANTHROPIC_API_KEY=你的金鑰
    OPENAI_API_KEY=你的金鑰
