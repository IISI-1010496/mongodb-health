# Architecture

## 系統概覽

```mermaid
graph LR
    subgraph MongoDB Cluster
        M[mongos]
        S1[Shard 1]
        S2[Shard 2]
        S3[Shard N]
        M --- S1
        M --- S2
        M --- S3
    end

    subgraph Docker Container
        SP[snapshot.py]
        AN[analyze.py]
        NO[notify.py]
        PR[prompts/analyze.md]
    end

    subgraph External Services
        GM[Gemini API]
        SL[Slack]
    end

    M -->|pymongo| SP
    SP -->|JSON stdout| AN
    PR -.->|prompt| AN
    AN -->|HTTP POST| GM
    GM -->|分析報告| AN
    AN -->|text stdout| NO
    NO -->|HTTP POST| SL
```

## Pipeline 資料流

三個 Python 腳本透過 Unix pipe 串接，每個元件獨立可用。

```mermaid
flowchart TD
    subgraph "run.sh"
        A["snapshot.py -q --compact"]
        B[analyze.py]
        C["notify.py --title NAME"]
    end

    CFG[config.json] -->|clusters, API key, webhook| A
    CFG -->|gemini_api_key| B
    CFG -->|slack_webhook| C

    A -->|"stdout: JSON"| B
    B -->|"stdout: 分析報告"| C
    C -->|"HTTP POST"| SLACK[Slack Channel]

    PR[prompts/analyze.md] -.->|分析規則| B
    GM[Gemini API] <-->|request/response| B
```

## snapshot.py 資料收集

透過 mongos 連線，收集 12 類健康指標。

```mermaid
flowchart TD
    START[MongoClient 連線] --> PING{ping 成功?}
    PING -->|No| ERR[回傳 error]
    PING -->|Yes| SS[serverStatus]

    SS --> IS_MONGOS{process == mongos?}

    IS_MONGOS -->|Yes| SHARD_DATA
    IS_MONGOS -->|No| COMMON

    subgraph SHARD_DATA [Sharded Cluster 專屬]
        SH[listShards<br/>shard 清單]
        BAL[balancerStatus<br/>+ config.settings]
        CHK[config.chunks<br/>chunk 分佈]
        MIG[config.changelog<br/>遷移紀錄]
        MGS[config.mongos<br/>mongos 實例]
        LCK[config.locks<br/>active locks]
    end

    SHARD_DATA --> COMMON

    subgraph COMMON [通用指標]
        POOL[connPoolStats<br/>連線池 + RS 拓撲]
        OPS[currentOp<br/>長時間操作]
        DISK[admin.dbStats<br/>磁碟空間]
        DB[各 DB dbStats<br/>容量 + 索引]
        WARN[getLog startupWarnings]
    end

    COMMON --> OUTPUT["JSON 輸出<br/>{generated_at, clusters: {...}}"]
```

## Snapshot JSON 結構

```mermaid
classDiagram
    class Snapshot {
        string generated_at
        map~string, Cluster~ clusters
    }

    class Cluster {
        bool up
        string version
        int uptime_seconds
        string process
        string context
        Connections connections
        Memory memory_mb
        OpCounters opcounters_total
        Network network_bytes_total
        DiskSpace disk_space
        Database[] databases
        string[] startup_warnings
    }

    class ShardedCluster {
        Shard[] shards
        Balancer balancer
        ChunkDist[] chunk_distribution
        ChunkMigration chunk_migrations
        MongosInstance[] mongos_instances
        Lock[] active_locks
        RSTopology[] rs_topology
        ConnectionPool connection_pool
        LongOp[] long_running_ops
    }

    class DiskSpace {
        DiskInfo dataRS0
        DiskInfo dataRS1
        DiskInfo dataRS_N
    }

    class DiskInfo {
        float total_gb
        float used_gb
        float free_gb
        float used_pct
    }

    class Database {
        string name
        float size_on_disk_mb
        float data_size_mb
        float storage_size_mb
        float index_size_mb
    }

    class Balancer {
        string mode
        bool running
        string activeWindow
        int chunkSize_mb
    }

    Snapshot --> Cluster
    Cluster <|-- ShardedCluster
    Cluster --> DiskSpace
    Cluster --> Database
    DiskSpace --> DiskInfo
    ShardedCluster --> Balancer
```

## 設定解析

config.json 的值如何流入各元件。

```mermaid
flowchart LR
    subgraph config.json
        CL["clusters[]<br/>{name, uri, context}"]
        GK[gemini_api_key]
        SW[slack_webhook]
    end

    subgraph 環境變數
        EG[GEMINI_API_KEY]
        ES[SLACK_WEBHOOK_URL]
    end

    subgraph CLI 參數
        AG["--api-key"]
        AW["--webhook"]
    end

    CL --> SP[snapshot.py]
    GK --> AN[analyze.py]
    EG --> AN
    AG --> AN
    SW --> NO[notify.py]
    ES --> NO
    AW --> NO

    style AG fill:#f9f,stroke:#333
    style AW fill:#f9f,stroke:#333
    style EG fill:#ff9,stroke:#333
    style ES fill:#ff9,stroke:#333
    style CL fill:#9ff,stroke:#333
    style GK fill:#9ff,stroke:#333
    style SW fill:#9ff,stroke:#333
```

每個元件的設定來源優先順序：**CLI 參數 > 環境變數 > config.json**

## Docker 部署

```mermaid
flowchart TD
    subgraph "Docker Image (tqsmm628/mongodb-health)"
        IM_SP[snapshot.py]
        IM_AN[analyze.py]
        IM_NO[notify.py]
        IM_RUN[run.sh]
        IM_PR[prompts/analyze.md<br/>通用版]
        IM_REQ[pymongo]
    end

    subgraph "使用者本地"
        U_CFG[config.json<br/>連線資訊 + API keys]
        U_PR["analyze.md<br/>自訂 prompt（選填）"]
    end

    U_CFG -->|"-v ./config.json:/app/config.json"| IM_SP
    U_PR -->|"-v ./analyze.md:/app/prompts/analyze.md"| IM_PR

    IM_RUN --> IM_SP
    IM_SP --> IM_AN
    IM_AN --> IM_NO
```

## AI 分析流程

analyze.py 如何與 Gemini API 互動。

```mermaid
sequenceDiagram
    participant stdin as stdin (snapshot JSON)
    participant analyze as analyze.py
    participant gemini as Gemini API

    stdin->>analyze: JSON 資料
    analyze->>analyze: 讀取 prompts/analyze.md
    analyze->>analyze: 組合 prompt + JSON
    analyze->>gemini: POST /v1beta/models/gemini-2.5-flash:generateContent
    gemini-->>analyze: candidates[0].content.parts[0].text
    analyze->>analyze: 輸出到 stdout
```

## Slack 通知流程

notify.py 的 Markdown 轉換與發送。

```mermaid
flowchart LR
    STDIN[stdin 文字] --> TITLE{"有 --title?"}
    TITLE -->|Yes| ADD["加標題<br/>## title + 內容"]
    TITLE -->|No| CONV
    ADD --> CONV

    subgraph CONV [Markdown → Slack mrkdwn]
        H["### heading → *heading*"]
        B["**bold** → *bold*"]
    end

    CONV --> POST["HTTP POST<br/>Slack Webhook"]
    POST --> SL[Slack Channel]
```

## 監控指標與閾值

```mermaid
flowchart TD
    subgraph "🔴 嚴重"
        D1["磁碟 > 85%"]
        C1["connections > 80%"]
        R1["rs_topology 節點故障<br/>（非 hidden）"]
        B1["balancer mode = off"]
    end

    subgraph "🟡 關注"
        D2["磁碟 70% - 85%"]
        I2["index_size/data_size<br/>40% - 60%"]
        M2["chunk 分佈不均 > 30%"]
        S2["mongos stale > 60s"]
        F2["碎片化 > 30%"]
    end

    subgraph "🟢 正常"
        D3["磁碟 < 70%"]
        I3["index ratio < 40%"]
        C3["connections < 80%"]
        N3["所有節點 ok"]
    end
```
