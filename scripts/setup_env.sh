#!/usr/bin/env bash
# ashare-anomaly-prober 部署脚本（Linux / macOS）
# 用法: bash scripts/setup_env.sh [--with-dev]
#
# 做什么:
#   1. 校验 Python >= 3.12
#   2. 在项目根创建 .venv 虚拟环境（不动系统 Python）
#   3. 按 requirements.lock.txt 精确安装依赖（含 akshare==1.18.84 锁定）
#   4. 以 --no-deps 方式安装本包（依赖以 lock 为准，避免重新解析漂移）
#   5. 冒烟: prober version / status，并回显 akshare 实际版本供人工核对
#
# ⚠️ akshare 版本锁定纪律（详见 README「版本锁定与升级注意事项」）:
#   - akshare 函数名随版本剧烈变动，lock 文件外的版本未经本项目验证；
#   - 升级前必须: dir(ak) 核实候选函数链 → 全量 pytest → 真数据冒烟。
set -euo pipefail

cd "$(dirname "$0")/.."
LOCK="requirements.lock.txt"
WITH_DEV="${1:-}"

if [ ! -f "$LOCK" ]; then
    echo "[错误] 未找到 $LOCK —— 请在项目根目录运行本脚本" >&2
    exit 1
fi

# 1) Python 版本校验
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装 Python >= 3.12" >&2
    exit 1
fi
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
python3 - "$PYVER" <<'PYEOF'
import sys
major, minor = sys.argv[1].split(".")
if (int(major), int(minor)) < (3, 12):
    print(f"[错误] Python {sys.argv[1]} 过低，本项目要求 >= 3.12", file=sys.stderr)
    sys.exit(1)
PYEOF
echo "[1/5] Python $PYVER 校验通过"

# 2) 虚拟环境（已存在则复用）
if [ ! -d ".venv" ]; then
    echo "[2/5] 创建虚拟环境 .venv"
    python3 -m venv .venv
else
    echo "[2/5] 复用已有 .venv"
fi
PIP=".venv/bin/pip"

# 3) 按 lock 精确安装（保证 akshare 版本与验证基线一致）
echo "[3/5] 安装锁定依赖（$LOCK）"
"$PIP" install --upgrade pip
"$PIP" install -r "$LOCK"
if [ "$WITH_DEV" = "--with-dev" ]; then
    "$PIP" install "pytest>=8.0" "pytest-asyncio>=0.23" "ruff>=0.5" "mypy>=1.10"
fi

# 4) 安装本包（--no-deps：依赖一律以 lock 为准）
echo "[4/5] 安装 ashare-anomaly-prober（--no-deps）"
"$PIP" install --no-deps .

# 5) 冒烟
echo "[5/5] 冒烟检查"
.venv/bin/prober version
AKVER="$(.venv/bin/python -c 'from importlib.metadata import version; print(version("akshare"))')"
LOCKVER="$(grep -E '^akshare==' "$LOCK" | cut -d= -f3)"
echo "akshare 实际安装: $AKVER / lock 锁定: $LOCKVER"
if [ "$AKVER" != "$LOCKVER" ]; then
    echo "[警告] akshare 版本与 lock 不一致！未经本项目验证的版本可能出现函数名变动，" >&2
    echo "        请核对 README「版本锁定与升级注意事项」后再继续。" >&2
fi
.venv/bin/prober status || echo "[提示] prober status 异常，请检查数据源连通性（可先 prober dashboard 验证）"

echo
echo "完成。后续步骤:"
echo "  cp config.example.toml config.toml   # 填 LLM / webhook 凭据（或启动后在页面 ⚙️ 设置里配）"
echo "  .venv/bin/prober dashboard           # 生成默认页"
echo "  .venv/bin/prober serve --port 8000   # 起本地服务"
