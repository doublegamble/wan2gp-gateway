# Wan2GP Gateway 獨立服務指南

本目錄包含了 Wan2GP Gateway API 的獨立服務、設定與 Docker 部署設定。

---

## 📁 目錄結構

```
gateway/
├── gateway.py            # FastAPI 主服務程式 (Port 50080)
├── gateway_config.json   # 服務設定檔 (模型選擇、畫質、CFG、Webhook 回呼位址)
├── Dockerfile            # Gateway 專用 Docker 容器定義
├── docker-compose.yml    # Docker 一鍵啟動設定檔 (自動掛載 GPU 與模型路徑)
├── start_gateway.bat     # Windows 本地 Python 啟動腳本
├── run_docker.bat        # Windows Docker Compose 一鍵啟動腳本
├── run_docker.sh         # Linux Docker Compose 一鍵啟動腳本
└── README.md             # 本說明文件
```

---

## 🚀 跨機器轉移與部署步驟

當您需要將 Gateway 服務複製轉移到其他電腦或 GPU 伺服器時：

### 方式 A：使用 Docker (推薦，跨平台最穩定)

**前置需求**：
* 安裝好 NVIDIA 顯卡驅動
* 安裝 Docker 與 NVIDIA Container Toolkit (`--gpus all` 支援)

**部署步驟**：
1. 將專案資料夾打包複製到新電腦上。
2. 進入 `gateway` 資料夾：
   ```bash
   cd gateway
   ```
3. 執行啟動命令：
   - **Linux / macOS**: `./run_docker.sh` 或 `docker compose up -d`
   - **Windows**: 雙擊 `run_docker.bat` 或在 CMD 執行 `docker compose up -d`

4. 服務將自動於 `http://localhost:50080` 啟動！
   - 設定介面：`http://localhost:50080/`
   - 生圖 API：`POST http://localhost:50080/api/generate`

---

### 方式 B：本地 Python 虛擬環境啟動

**前置需求**：
* 安裝好 Python 3.10+ 及需求套件 (`requirements.txt`)

**部署步驟**：
1. 在根目錄雙擊 `start_gateway.bat` 或執行：
   ```bash
   python gateway/gateway.py
   ```
2. Gateway 伺服器即會在 Port 50080 運行。

---

## 🔑 安全驗證與 Token

* 預設認證 Token 為：`my_super_secret_cookcalai_token_999`
* 您可透過環境變數 `GATEWAY_TOKEN` 修改：
  ```bash
  export GATEWAY_TOKEN="your_custom_secure_token"
  ```
* 或在 `docker-compose.yml` 中的 `environment` 段落調整。
