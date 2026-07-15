# 安全事件記錄

## 2026-07-09：`KEYCLOAK_CLIENT_SECRET` 曾隨程式碼明文外洩

`services/webhook-service/app/config.py` 在早期開發階段（`5880ec1` ~
`d245f78`，共 3 個 commit）把 `KEYCLOAK_CLIENT_SECRET` 的預設值寫死在程式碼裡。
該值已隨這些 commit push 到本 repo（public）。

- **發現**：2026-07-09，在協助另一次改動時發現。
- **修正 commit**：`44a660c`（改成強制吃環境變數，移除硬編碼預設值）。
- **輪替**：2026-07-15，已在 Keycloak 後台（Clients → `user-sync-service` →
  Credentials）Regenerate，舊值正式作廢；新值已部署並驗證 client_credentials
  換 token 成功。
- **git 歷史處理**：**未改寫歷史**。舊值已作廢、無法再用於任何認證，繼續留在
  `5880ec1`/`922225e`/`d245f78` 這幾個 commit 的歷史樹裡只是死資料，不構成
  可利用的風險。改寫歷史需要 force-push 覆蓋 public repo 的 `main`，會讓既有
  clone/fork 的歷史分岔，兩相權衡後選擇不做。

同一輪也一併輪替了 `WEBHOOK_SECRET`、`PG_DSN` 密碼（兩者未曾在 git 歷史中明文
出現，屬預防性輪替，非曝光事件）。
