# AIDS-OJ：AI 智慧錯誤診斷系統

基於開源 OnlineJudge 開發，以下為新增與修改的核心檔案：

## 修改說明

| 檔案 | 類型 | 說明 |
|---|---|---|
| utils/tutor_engine.py | 新增 | 三家 AI（Gemini、Claude、ChatGPT）診斷引擎 |
| judge/dispatcher.py | 修改 | 判題完成後觸發 AI 診斷、靜態分析、知識追蹤 |
| submission/models.py | 修改 | 新增 ai_hint、error_type、problem_tags 欄位 |
| docker-compose.yml | 修改 | 新增 API 金鑰環境變數 |

## 系統功能
- 三家 AI 同時診斷，生成漸進式引導提示
- AST + flake8 靜態程式分析
- 錯誤類型自動分類（SYNTAX/LOGIC/RUNTIME/STYLE）
- 知識點追蹤（student_knowledge_state）
