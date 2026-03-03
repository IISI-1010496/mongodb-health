#!/bin/sh
# 完整 pipeline：snapshot → analyze → notify
# 用法：
#   ./run.sh                           # 全部叢集
#   ./run.sh --cluster mongodb-new     # 指定叢集
set -e
cd "$(dirname "$0")"

# 從 config.json 取得叢集名稱作為 Slack 標題
TITLE=$(python -c "
import json
from pathlib import Path
p = Path('config.json')
if p.exists():
    names = [c['name'] for c in json.loads(p.read_text())['clusters']]
    print(' / '.join(names))
" 2>/dev/null || echo "")

python snapshot.py -q --compact "$@" | python analyze.py | python notify.py --title "$TITLE"
