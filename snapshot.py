"""
MongoDB Sharded Cluster 健康快照 — 供 AI 分析用

用法：
  # 用 config 檔（預設 config.json）
  python snapshot.py

  # 指定 config 檔
  python snapshot.py -c prod.json

  # 直接給連線字串（不需要 config 檔）
  python snapshot.py --uri "mongodb://user:pass@host:27017/admin?authSource=admin"
  python snapshot.py --uri "mongodb://..." --name my-cluster

  # 多個叢集
  python snapshot.py --uri "mongodb://host1:27017" --name cluster-a --uri "mongodb://host2:27017" --name cluster-b

  # 輸出到檔案
  python snapshot.py -o snapshot.json

  # pipe 給 AI 分析（compact 模式省 token）
  python snapshot.py -q --compact | gemini -p "$(cat prompts/analyze.md)"
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient


def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return default if default is not None else {"error": str(e)}


def collect_server_status(client):
    ss = client.admin.command("serverStatus")
    conn = ss.get("connections", {})
    mem = ss.get("mem", {})
    ops = ss.get("opcounters", {})
    net = ss.get("network", {})
    return {
        "version": ss.get("version"),
        "uptime_seconds": ss.get("uptimeMillis", 0) // 1000,
        "process": ss.get("process"),
        "connections": {
            "current": conn.get("current"),
            "available": conn.get("available"),
            "total_created": conn.get("totalCreated"),
        },
        "memory_mb": {
            "resident": mem.get("resident"),
            "virtual": mem.get("virtual"),
        },
        "opcounters_total": {
            k: ops.get(k, 0)
            for k in ["insert", "query", "update", "delete", "getmore", "command"]
        },
        "network_bytes_total": {
            "in": net.get("bytesIn"),
            "out": net.get("bytesOut"),
        },
    }


def collect_shards(client):
    shards_resp = client.admin.command("listShards")
    result = []
    for s in shards_resp.get("shards", []):
        host_str = s.get("host", "")
        rs_name = host_str.split("/")[0] if "/" in host_str else None
        members = host_str.split("/")[1].split(",") if "/" in host_str else [host_str]
        result.append({
            "name": s["_id"],
            "rs": rs_name,
            "state": s.get("state"),
            "members": members,
        })
    return result


def collect_balancer(client):
    bal = client.admin.command("balancerStatus")
    result = {
        "mode": bal.get("mode"),
        "running": bal.get("inBalancerRound", False),
    }

    # 從 config.settings 讀取 activeWindow 和 chunkSize
    settings = {
        doc["_id"]: doc
        for doc in client.config.settings.find({"_id": {"$in": ["balancer", "chunksize"]}})
    }
    bal_settings = settings.get("balancer", {})
    if "activeWindow" in bal_settings:
        result["activeWindow"] = bal_settings["activeWindow"]
    chunk_settings = settings.get("chunksize", {})
    if "value" in chunk_settings:
        result["chunkSize_mb"] = chunk_settings["value"]

    return result


def collect_conn_pool(client):
    pool = client.admin.command("connPoolStats")

    primary_hosts = set()
    rs_topology = {}
    for rs_name, rs_info in pool.get("replicaSets", {}).items():
        members = []
        for m in rs_info.get("hosts", []):
            role = "PRIMARY" if m.get("ismaster") else "SECONDARY"
            if m.get("ismaster"):
                primary_hosts.add(m["addr"])
            members.append({
                "host": m["addr"],
                "role": role,
                "ok": m.get("ok"),
                "ping_ms": m.get("pingTimeMillis"),
                "hidden": m.get("hidden", False),
            })
        rs_topology[rs_name] = members

    pool_nodes = []
    for host, info in sorted(pool.get("hosts", {}).items()):
        pool_nodes.append({
            "host": host,
            "role": "PRIMARY" if host in primary_hosts else "SECONDARY",
            "in_use": info.get("inUse", 0),
            "available": info.get("available", 0),
            "created": info.get("created", 0),
        })

    return {"rs_topology": rs_topology, "connection_pool": pool_nodes}


def collect_chunk_distribution(client, version):
    major = int(version.split(".")[0]) if version else 0

    if major >= 6:
        rows = list(client.admin.aggregate([{"$shardedDataDistribution": {}}]))
        result = []
        for r in rows:
            shards = {}
            for s in r.get("shards", []):
                shards[s["shardName"]] = {
                    "docs": s.get("numOwnedDocuments"),
                    "size_bytes": s.get("ownedSizeBytes"),
                    "chunks": s.get("numOwnedChunks"),
                    "orphaned_docs": s.get("numOrphanedDocs", 0),
                }
            result.append({"ns": r.get("ns"), "shards": shards})
        result.sort(
            key=lambda x: sum(s.get("size_bytes", 0) for s in x["shards"].values()),
            reverse=True,
        )
        return result[:20]
    else:
        rows = list(client.config.chunks.aggregate([
            {"$group": {
                "_id": {"ns": "$ns", "shard": "$shard"},
                "count": {"$sum": 1},
            }},
            {"$group": {
                "_id": "$_id.ns",
                "shards": {"$push": {"shard": "$_id.shard", "chunks": "$count"}},
                "total": {"$sum": "$count"},
            }},
            {"$sort": {"total": -1}},
            {"$limit": 20},
        ]))
        return [
            {
                "ns": r["_id"],
                "total_chunks": r["total"],
                "shards": {s["shard"]: {"chunks": s["chunks"]} for s in r["shards"]},
            }
            for r in rows
        ]


def collect_chunk_migrations(client):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    stats_raw = list(client.config.changelog.aggregate([
        {"$match": {
            "what": {"$in": ["moveChunk.commit", "moveChunk.error"]},
            "time": {"$gte": cutoff},
        }},
        {"$group": {"_id": "$what", "count": {"$sum": 1}}},
    ]))
    stats = {r["_id"]: r["count"] for r in stats_raw}

    failures = list(
        client.config.changelog.find(
            {"what": "moveChunk.error", "time": {"$gte": cutoff}},
            {"_id": 0, "time": 1, "ns": 1, "details": 1},
        ).sort("time", -1).limit(5)
    )

    recent = list(
        client.config.changelog.find(
            {"what": {"$regex": "moveChunk"}, "time": {"$gte": cutoff}},
            {"_id": 0, "time": 1, "what": 1, "ns": 1, "details.from": 1, "details.to": 1},
        ).sort("time", -1).limit(10)
    )

    return {
        "last_24h": {
            "successful": stats.get("moveChunk.commit", 0),
            "failed": stats.get("moveChunk.error", 0),
        },
        "recent_failures": [
            {
                "time": str(f.get("time")),
                "ns": f.get("ns"),
                "from": f.get("details", {}).get("from"),
                "to": f.get("details", {}).get("to"),
                "error": (
                    f.get("details", {}).get("errmsg")
                    or f.get("details", {}).get("note")
                    or f.get("details", {}).get("error")
                ),
                "chunk_min": str(f.get("details", {}).get("min")) if f.get("details", {}).get("min") else None,
                "chunk_max": str(f.get("details", {}).get("max")) if f.get("details", {}).get("max") else None,
            }
            for f in failures
        ],
        "recent_activity": [
            {
                "time": str(r.get("time")),
                "what": r.get("what"),
                "ns": r.get("ns"),
                "from": r.get("details", {}).get("from"),
                "to": r.get("details", {}).get("to"),
            }
            for r in recent
        ],
    }


def collect_long_running_ops(client):
    pipeline = [
        {"$currentOp": {"allUsers": True, "idleSessions": False}},
        {"$match": {"active": True, "secs_running": {"$gt": 10}}},
        {"$project": {
            "opid": 1, "op": 1, "ns": 1, "secs_running": 1,
            "desc": 1, "waitingForLock": 1, "msg": 1,
        }},
        {"$sort": {"secs_running": -1}},
        {"$limit": 20},
    ]
    rows = list(client.admin.aggregate(pipeline))
    return [
        {
            "opid": str(r.get("opid")),
            "op": r.get("op"),
            "ns": r.get("ns"),
            "secs_running": r.get("secs_running"),
            "desc": r.get("desc"),
            "waiting_for_lock": r.get("waitingForLock", False),
            "msg": r.get("msg"),
        }
        for r in rows
    ]


def collect_disk_space(client):
    """從 admin dbStats 取得磁碟空間（4.4+），sharded cluster 取每個 shard"""
    stats = client.admin.command("dbStats")
    raw = stats.get("raw", {})

    def fmt(shard_stats):
        total = shard_stats.get("fsTotalSize", 0)
        used = shard_stats.get("fsUsedSize", 0)
        if not total:
            return None
        return {
            "total_gb": round(total / 1024**3, 1),
            "used_gb": round(used / 1024**3, 1),
            "free_gb": round((total - used) / 1024**3, 1),
            "used_pct": round(used / total * 100, 1),
        }

    if raw:
        result = {}
        for shard_key, shard_stats in raw.items():
            shard_name = shard_key.split("/")[0]
            info = fmt(shard_stats)
            if info:
                result[shard_name] = info
        return result

    info = fmt(stats)
    return {"server": info} if info else {}


def collect_db_stats(client):
    dbs = client.admin.command("listDatabases")
    result = []
    for db_info in sorted(
        dbs.get("databases", []), key=lambda x: x.get("sizeOnDisk", 0), reverse=True
    ):
        name = db_info["name"]
        if name in ("admin", "local", "config"):
            continue
        try:
            stats = client[name].command("dbStats")
            entry = {
                "name": name,
                "size_on_disk_mb": round(db_info.get("sizeOnDisk", 0) / 1024 / 1024, 1),
                "collections": stats.get("collections"),
                "objects": stats.get("objects"),
                "data_size_mb": round(stats.get("dataSize", 0) / 1024 / 1024, 1),
                "storage_size_mb": round(stats.get("storageSize", 0) / 1024 / 1024, 1),
                "index_size_mb": round(stats.get("indexSize", 0) / 1024 / 1024, 1),
            }
            free = stats.get("freeStorageSize")
            if free is not None:
                entry["free_storage_mb"] = round(free / 1024 / 1024, 1)
            result.append(entry)
        except Exception:
            result.append({"name": name, "error": "dbStats failed"})
    return result[:15]


def collect_mongos_instances(client):
    now = datetime.now(timezone.utc)
    rows = list(client.config.mongos.find(
        {}, {"_id": 1, "ping": 1, "up": 1, "mongoVersion": 1}
    ))
    result = []
    for r in rows:
        ping_time = r.get("ping")
        stale_seconds = None
        if ping_time:
            if ping_time.tzinfo is None:
                ping_time = ping_time.replace(tzinfo=timezone.utc)
            stale_seconds = int((now - ping_time).total_seconds())
        result.append({
            "host": r["_id"],
            "version": r.get("mongoVersion"),
            "uptime_seconds": r.get("up"),
            "last_ping": str(ping_time) if ping_time else None,
            "stale_seconds": stale_seconds,
        })
    return result


def collect_config_locks(client):
    rows = list(client.config.locks.find(
        {"state": {"$ne": 0}},
        {"_id": 1, "state": 1, "who": 1, "when": 1, "why": 1},
    ))
    return [
        {
            "name": r["_id"],
            "state": r.get("state"),
            "who": r.get("who"),
            "when": str(r.get("when")),
            "why": r.get("why"),
        }
        for r in rows
    ]


def collect_startup_warnings(client):
    log = client.admin.command("getLog", "startupWarnings")
    warnings = []
    for line in log.get("log", []):
        try:
            parsed = json.loads(line)
            warnings.append(parsed.get("msg", line))
        except (json.JSONDecodeError, TypeError):
            warnings.append(str(line))
    return warnings


def collect_cluster(name, uri):
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
    except Exception as e:
        return {"error": f"Connection failed: {e}"}

    result = {"up": True}

    ss = safe(lambda: collect_server_status(client))
    if isinstance(ss, dict) and "error" not in ss:
        result.update(ss)
    else:
        result["serverStatus_error"] = ss

    version = result.get("version", "")

    # 判斷是否為 sharded cluster（mongos）
    is_mongos = result.get("process") == "mongos"

    if is_mongos:
        result["shards"] = safe(lambda: collect_shards(client), [])
        result["balancer"] = safe(lambda: collect_balancer(client))
        result["chunk_distribution"] = safe(lambda: collect_chunk_distribution(client, version), [])
        result["chunk_migrations"] = safe(lambda: collect_chunk_migrations(client))
        result["mongos_instances"] = safe(lambda: collect_mongos_instances(client), [])
        result["active_locks"] = safe(lambda: collect_config_locks(client), [])

    pool_data = safe(lambda: collect_conn_pool(client))
    if isinstance(pool_data, dict) and "error" not in pool_data:
        result["rs_topology"] = pool_data["rs_topology"]
        result["connection_pool"] = pool_data["connection_pool"]
    else:
        result["connpool_error"] = pool_data

    result["long_running_ops"] = safe(lambda: collect_long_running_ops(client), [])
    result["disk_space"] = safe(lambda: collect_disk_space(client), {})
    result["databases"] = safe(lambda: collect_db_stats(client), [])
    result["startup_warnings"] = safe(lambda: collect_startup_warnings(client), [])

    try:
        client.close()
    except Exception:
        pass

    return result


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_args():
    parser = argparse.ArgumentParser(
        description="MongoDB health snapshot for AI analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s                                    # 用 config.json
  %(prog)s -c prod.json                       # 用指定 config
  %(prog)s --uri "mongodb://host:27017"       # 直接給連線字串
  %(prog)s --uri "mongodb://h1" --name c1 --uri "mongodb://h2" --name c2
  %(prog)s -o snapshot.json                   # 輸出到檔案
""",
    )
    parser.add_argument("-c", "--config", help="Config JSON file (default: config.json)")
    parser.add_argument("--cluster", action="append", help="Only run specified cluster(s) from config (repeatable)")
    parser.add_argument("--uri", action="append", help="MongoDB connection URI (repeatable)")
    parser.add_argument("--name", action="append", help="Cluster name for each --uri (optional)")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress messages on stderr")
    parser.add_argument("--compact", action="store_true", help="Compact JSON output (no indentation, saves ~30%% tokens)")
    return parser.parse_args()


def resolve_clusters(args):
    """從 CLI args 或 config 檔解析出 {name: {uri, context?}} 字典"""
    # --uri 優先
    if args.uri:
        names = args.name or []
        clusters = {}
        for i, uri in enumerate(args.uri):
            name = names[i] if i < len(names) else f"cluster-{i + 1}"
            clusters[name] = {"uri": uri}
        return clusters

    # 找 config 檔
    config_path = args.config or "config.json"
    p = Path(config_path)
    if not p.exists():
        # 嘗試 script 同目錄
        p = Path(__file__).parent / config_path
    if not p.exists():
        print(f"Error: config file '{config_path}' not found.", file=sys.stderr)
        print("Use --uri to specify connection strings, or create a config.json.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(p)
    filter_set = set(args.cluster) if args.cluster else None
    clusters = {}
    for entry in cfg.get("clusters", []):
        if filter_set and entry["name"] not in filter_set:
            continue
        clusters[entry["name"]] = {
            "uri": entry["uri"],
            "context": entry.get("context"),
        }
    if filter_set and not clusters:
        available = [e["name"] for e in cfg.get("clusters", [])]
        print(f"Error: cluster(s) {args.cluster} not found. Available: {available}", file=sys.stderr)
        sys.exit(1)
    return clusters


def main():
    args = parse_args()
    clusters = resolve_clusters(args)

    if not clusters:
        print("Error: no clusters configured.", file=sys.stderr)
        sys.exit(1)

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clusters": {},
    }

    for name, info in clusters.items():
        uri = info["uri"]
        if not args.quiet:
            print(f"Collecting {name}...", file=sys.stderr)
        data = collect_cluster(name, uri)
        if info.get("context"):
            data["context"] = info["context"]
        snapshot["clusters"][name] = data

    indent = None if args.compact else 2
    output = json.dumps(snapshot, indent=indent, ensure_ascii=False, default=str, separators=(",", ":") if args.compact else None)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
