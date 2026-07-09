# 功能測試腳本

## 前置需求

- Docker Compose stack 已啟動（`docker compose up`）
- 外部 LiteLLM（`LITELLM_BASE_URL`）可連線，並已載入 embedding / rerank / chat 模型
- Python 3.x + `requests`：`pip install requests`
- `tests/fixtures/test.pdf` 已存在（初次執行請先複製）：

```bash
docker cp compose-ingest-worker-1:/data/uploads/104fa00d-4609-4368-a1f8-e9edd35bab9b/deptA_IT-OT_Network_Policy.pdf \
  tests/fixtures/test.pdf
```

---

## 執行方式

所有腳本從專案根目錄執行：

```bash
cd /home/ai-x/km/repo/docblock-rag-platform

# 單支測試
python3 tests/01_health_check.py

# 依序跑完所有測試
for f in tests/0*.py; do echo "\n>>> $f"; python3 "$f"; done
```

---

## 測試清單

| 腳本 | 功能 | 預計時間 |
|------|------|----------|
| `01_health_check.py` | 所有服務 `/healthz` + `/readyz` | < 5s |
| `02_documents_list.py` | 列出文件、查單一文件、404 測試 | < 5s |
| `03_upload_pipeline.py` | 上傳 PDF → 完整 pipeline → 驗證建立 | 1~5 分鐘 |
| `04_acl_write_delete.py` | 設定/刪除 ACL、搜尋驗證 | < 30s |
| `05_search_acl.py` | 多用戶存取層級驗證（detail/summary/deny） | < 30s |
| `06_search_user_override.py` | user 規則覆蓋 dept 規則的優先順序驗證 | < 30s |
| `08_rag_answer.py` | RAG 問答生成 | 30~120s |
| `09_ingest_stages.py` | ingest-worker 三個階段分別執行 | 1~5 分鐘 |
| `10_document_delete.py` | 上傳 → 確認 → 刪除 → 驗證 404 | 1~5 分鐘 |
| `11_webhook_user_sync.py` | Webhook 認證驗證（Keycloak 可選） | < 10s |
| `12_reupload_same_document.py` | 帶原 document_id 重新上傳：version 不遞增（內容相同）/ 遞增（內容變更）、document_id 不變、chunks 不重複 | 2~10 分鐘 |

---

## 輸出說明

```
  [OK]   正常通過
  [FAIL] 驗證失敗（腳本結尾會列出所有失敗並以 exit code 1 結束）
  [--]   資訊輸出（非驗證）
```

---

## 常用設定覆蓋

透過環境變數可切換測試目標：

```bash
DOCUMENT_API=http://staging:8765 python3 tests/01_health_check.py
ACL_ADMIN_SECRET=my-secret python3 tests/04_acl_write_delete.py
```

---

## 注意事項

- **03、10** 需等待完整 pipeline（最多 5 分鐘），若逾時表示 ingest-worker 或 Ollama 有問題
- **08** RAG 問答需要 LLM 生成，預設 timeout 120s
- **09** 分階段測試使用 container 內現有的 PDF，不需上傳
- **11** 若 Keycloak 未啟動，第 3 個場景會回傳 5xx，測試標記為 SKIP 而非 FAIL
- **03、10** 會建立臨時文件（伺服器生成新 document_id），不會污染現有資料
- **12** 場景 A 使用 `TEST_PDF`；場景 B（內容變更）以程式動態產生合成 PDF，無需額外設定
