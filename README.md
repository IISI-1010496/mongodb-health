# MongoDB Health

MongoDB Sharded Cluster 自動化健康檢查工具。收集叢集快照、透過 Gemini AI 分析、推送結果到 Slack。

## 架構

```
snapshot.py → analyze.py → notify.py → Slack
  收集快照       AI 分析      推送通知
```

## 快速開始

### Docker（推薦）

1. 建立 `config.json`（參考 `config.example.json`）
2. 執行：

```bash
docker run --rm -v ./config.json:/app/config.json tqsmm628/mongodb-health
```

自訂分析 prompt（覆蓋內建的通用版）：

```bash
docker run --rm \
  -v ./config.json:/app/config.json \
  -v ./analyze.md:/app/prompts/analyze.md \
  tqsmm628/mongodb-health
```

指定叢集：

```bash
docker run --rm -v ./config.json:/app/config.json tqsmm628/mongodb-health --cluster mongodb-new
```

> **Windows PowerShell** 使用 `${PWD}\config.json` 取代 `./config.json`

### 本地執行

```bash
pip install -r requirements.txt
python snapshot.py -q --compact | python analyze.py | python notify.py
```

## 設定

複製 `config.example.json` 為 `config.json`：

```json
{
  "clusters": [
    {
      "name": "my-cluster",
      "uri": "mongodb://user:password@host:27017/admin?authSource=admin",
      "context": "選填。給 AI 的環境背景說明。"
    }
  ],
  "gemini_api_key": "YOUR_GEMINI_API_KEY",
  "slack_webhook": "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
}
```

| 欄位 | 必填 | 取得方式 |
|------|------|----------|
| `clusters[].uri` | ✅ | MongoDB 連線字串 |
| `gemini_api_key` | ✅ | [Google AI Studio](https://aistudio.google.com/apikey) |
| `slack_webhook` | ✅ | [Slack Apps](https://api.slack.com/apps) → Incoming Webhooks |
| `clusters[].context` | ❌ | 環境說明，AI 分析時會參考 |

## 各元件說明

| 檔案 | 用途 | 可獨立使用 |
|------|------|-----------|
| `snapshot.py` | 連接 MongoDB 收集健康快照（JSON） | ✅ |
| `analyze.py` | 將快照送 Gemini API 產生分析報告 | ✅ |
| `notify.py` | 將文字推送到 Slack | ✅ |
| `prompts/analyze.md` | AI 分析的 prompt | - |

### snapshot.py

```bash
python snapshot.py -q --compact                    # 全部叢集
python snapshot.py -q --compact --cluster my-cluster  # 指定叢集
python snapshot.py -o snapshot.json                # 輸出到檔案
```

### analyze.py

```bash
echo '{"clusters":{...}}' | python analyze.py              # 從 config.json 讀 API key
echo '{"clusters":{...}}' | python analyze.py --model gemini-2.5-pro  # 指定模型
```

### notify.py

```bash
echo "Hello" | python notify.py                            # 從 config.json 讀 webhook
echo "Hello" | python notify.py --webhook "https://..."    # 指定 webhook URL
```

## 監控項目

- 磁碟空間（每個 shard）
- Chunk 分佈均衡度
- Chunk 遷移成敗
- Balancer 狀態
- 連線使用率
- 長時間操作
- 節點拓撲健康
- 索引大小比例
- 資料庫碎片化
