#!/bin/sh
# 完整 pipeline：snapshot → analyze → notify
# 用法：
#   ./run.sh                           # 全部叢集
#   ./run.sh --cluster mongodb-new     # 指定叢集
set -e
cd "$(dirname "$0")"
python snapshot.py -q --compact "$@" | python analyze.py | python notify.py
