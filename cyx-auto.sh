#!/data/data/com.termux/files/usr/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PREFIX/bin/python"
CYTHANX="$SCRIPT_DIR/cythanx_xmrig_termux.py"

echo "[CYTHANX] Termux automation"

if ! command -v python >/dev/null 2>&1; then
    echo "[CYTHANX] Installing Python..."
    pkg update -y || exit 1
    pkg install -y python || exit 1
fi

if ! "$PYTHON" -c 'import blake3' >/dev/null 2>&1; then
    echo "[CYTHANX] Installing Python blake3..."
    "$PYTHON" -m pip install --upgrade pip || exit 1
    "$PYTHON" -m pip install blake3 || exit 1
fi

if [ ! -f "$CYTHANX" ]; then
    echo "[CYTHANX] Missing $CYTHANX"
    exit 1
fi

if [ "$#" -eq 0 ]; then
    echo
    echo "Usage:"
    echo "  $0 POOL:PORT WALLET.WORKER [XMRIG_PATH]"
    echo
    echo "Example:"
    echo "  $0 pool.example:3333 WALLET.WORKER ./xmrig"
    echo
    echo "Or set:"
    echo "  CYTHANX_POOL"
    echo "  CYTHANX_USER"
    echo "  CYTHANX_XMRIG"
    echo
    exit 2
fi

export CYTHANX_POOL="${CYTHANX_POOL:-$1}"
export CYTHANX_USER="${CYTHANX_USER:-${2:-}}"

if [ -n "${3:-}" ]; then
    export CYTHANX_XMRIG="$3"
fi

if [ -z "${CYTHANX_USER:-}" ]; then
    echo "[CYTHANX] Wallet/worker is required."
    exit 2
fi

if [ -z "${CYTHANX_XMRIG:-}" ] && ! command -v xmrig >/dev/null 2>&1; then
    echo "[CYTHANX] XMRig was not found."
    echo "Place the XMRig binary on PATH or pass its path as argument 3."
    exit 1
fi

chmod +x "$CYTHANX"

echo "[CYTHANX] Self-test..."
"$PYTHON" "$CYTHANX" --self-test || exit 1

echo "[CYTHANX] Starting automatic organism supervisor..."
exec "$PYTHON" "$CYTHANX" --auto
