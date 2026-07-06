#!/bin/bash

# 闲鱼监控系统本地启动脚本
# 功能：清理旧构建、安装依赖、构建前端、启动服务

if [ -z "${BASH_VERSION:-}" ] || [ "$(basename "${BASH:-$0}")" = "sh" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "错误: 需要使用 bash 运行此脚本"
    exit 1
fi

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    printf '%b\n' "$*"
}

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log "${GREEN}========================================${NC}"
log "${GREEN}闲鱼监控系统 - 本地启动脚本${NC}"
log "${GREEN}========================================${NC}"

# 0. 环境与依赖检查
log "\n${YELLOW}[1/6] 检查环境与依赖...${NC}"

OS_FAMILY="unknown"
LINUX_ID=""
LINUX_LIKE=""
PYTHON_CMD="${PYTHON_CMD:-}"
VENV_DIR="${VENV_DIR:-.venv}"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    LINUX_ID="$ID"
    LINUX_LIKE="$ID_LIKE"
fi

case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin)
        OS_FAMILY="macos"
        ;;
    Linux)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            OS_FAMILY="wsl"
        else
            OS_FAMILY="linux"
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*)
        OS_FAMILY="windows"
        ;;
    *)
        OS_FAMILY="unknown"
        ;;
esac

if [ "$OS_FAMILY" = "windows" ]; then
    VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
else
    VENV_PYTHON="$VENV_DIR/bin/python"
fi

MISSING_ITEMS=()

python_is_310_plus() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

resolve_python_cmd() {
    local candidate
    local resolved
    local candidates=(
        python3.12
        python3.11
        python3.10
        python3.13
        python3.14
        /opt/homebrew/bin/python3.12
        /opt/homebrew/bin/python3.11
        /opt/homebrew/bin/python3.10
        /opt/homebrew/bin/python3.13
        /opt/homebrew/bin/python3.14
        /opt/homebrew/bin/python3
        /usr/local/bin/python3.12
        /usr/local/bin/python3.11
        /usr/local/bin/python3.10
        /usr/local/bin/python3.13
        /usr/local/bin/python3.14
        /usr/local/bin/python3
        python3
    )

    if [ -n "$PYTHON_CMD" ] && python_is_310_plus "$PYTHON_CMD"; then
        printf '%s\n' "$PYTHON_CMD"
        return 0
    fi

    for candidate in "${candidates[@]}"; do
        resolved=""
        if [ -x "$candidate" ]; then
            resolved="$candidate"
        elif command -v "$candidate" >/dev/null 2>&1; then
            resolved="$(command -v "$candidate")"
        fi

        if [ -n "$resolved" ] && python_is_310_plus "$resolved"; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done

    return 1
}

if [ -x "$VENV_PYTHON" ] && python_is_310_plus "$VENV_PYTHON"; then
    PYTHON_CMD="$VENV_PYTHON"
else
    BASE_PYTHON_CMD=""
    if ! BASE_PYTHON_CMD="$(resolve_python_cmd)"; then
        PYTHON_CMD=""
        MISSING_ITEMS+=("python3(>=3.10)")
    else
        if [ -d "$VENV_DIR" ]; then
            log "${YELLOW}检测到 $VENV_DIR 不是 Python 3.10+ 环境，正在重建...${NC}"
            if ! "$BASE_PYTHON_CMD" -m venv --clear "$VENV_DIR"; then
                PYTHON_CMD=""
                MISSING_ITEMS+=("python3-venv")
            fi
        else
            log "${YELLOW}正在创建 Python 虚拟环境: $VENV_DIR${NC}"
            if ! "$BASE_PYTHON_CMD" -m venv "$VENV_DIR"; then
                PYTHON_CMD=""
                MISSING_ITEMS+=("python3-venv")
            fi
        fi

        if [ -n "$BASE_PYTHON_CMD" ] && [ -x "$VENV_PYTHON" ] && python_is_310_plus "$VENV_PYTHON"; then
            PYTHON_CMD="$VENV_PYTHON"
        elif [ -n "$PYTHON_CMD" ]; then
            PYTHON_CMD=""
            MISSING_ITEMS+=("python3(>=3.10)")
        fi
    fi
fi

if [ -n "$PYTHON_CMD" ]; then
    PYTHON_VERSION="$("$PYTHON_CMD" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
    if ! "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
        MISSING_ITEMS+=("pip")
    fi
else
    PYTHON_VERSION=""
    MISSING_ITEMS+=("pip")
fi

if ! command -v node >/dev/null 2>&1; then
    MISSING_ITEMS+=("node")
fi

if ! command -v npm >/dev/null 2>&1; then
    MISSING_ITEMS+=("npm")
fi

has_browser=false
case "$OS_FAMILY" in
    macos)
        if [ -d "/Applications/Google Chrome.app" ] || [ -d "/Applications/Microsoft Edge.app" ]; then
            has_browser=true
        fi
        ;;
    linux|wsl)
        if command -v google-chrome >/dev/null 2>&1 \
            || command -v google-chrome-stable >/dev/null 2>&1 \
            || command -v chromium >/dev/null 2>&1 \
            || command -v chromium-browser >/dev/null 2>&1 \
            || command -v microsoft-edge >/dev/null 2>&1 \
            || command -v microsoft-edge-stable >/dev/null 2>&1; then
            has_browser=true
        fi
        ;;
    windows)
        if [ -d "/c/Program Files/Google/Chrome/Application" ] \
            || [ -d "/c/Program Files (x86)/Google/Chrome/Application" ] \
            || [ -d "/c/Program Files (x86)/Microsoft/Edge/Application" ] \
            || [ -d "/c/Program Files/Microsoft/Edge/Application" ]; then
            has_browser=true
        fi
        ;;
esac

if [ "$has_browser" = false ]; then
    MISSING_ITEMS+=("浏览器(Chrome 或 Edge)")
fi


print_solution_macos() {
    cat <<'EOF'
macOS 解决办法:
1) 安装 Homebrew:
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
2) 安装 Python 与 Node:
   brew install python@3.11 node
3) 安装 Playwright:
   python3 -m pip install playwright
   python3 -m playwright install chromium
4) 安装浏览器:
   brew install --cask google-chrome
   # 或
   brew install --cask microsoft-edge
5) 配置文件（可选）:
   cp .env.example .env
   cp config.json.example config.json
EOF
}

print_solution_linux_deb() {
    cat <<'EOF'
Linux (Debian/Ubuntu) 解决办法:
1) 安装 Python 与 pip:
   sudo apt-get update
   sudo apt-get install -y python3 python3-pip python3-venv
2) 安装 Node.js 与 npm (LTS):
   curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
   sudo apt-get install -y nodejs
3) 安装 Playwright:
   python3 -m pip install playwright
   python3 -m playwright install chromium
   python3 -m playwright install-deps chromium
4) 安装浏览器:
   sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
   # 或安装 Edge:
   sudo apt-get install -y microsoft-edge-stable
5) 配置文件（可选）:
   cp .env.example .env
   cp config.json.example config.json
EOF
}

print_solution_linux_rpm() {
    cat <<'EOF'
Linux (RHEL/CentOS/Fedora) 解决办法:
1) 安装 Python 与 pip:
   sudo dnf install -y python3 python3-pip
2) 安装 Node.js 与 npm (LTS):
   sudo dnf install -y nodejs
3) 安装 Playwright:
   python3 -m pip install playwright
   python3 -m playwright install chromium
   python3 -m playwright install-deps chromium
4) 安装浏览器:
   sudo dnf install -y chromium
   # 或安装 Edge:
   sudo dnf install -y microsoft-edge-stable
5) 配置文件（可选）:
   cp .env.example .env
   cp config.json.example config.json
EOF
}

print_solution_linux_arch() {
    cat <<'EOF'
Linux (Arch) 解决办法:
1) 安装 Python 与 pip:
   sudo pacman -S --noconfirm python python-pip
2) 安装 Node.js 与 npm:
   sudo pacman -S --noconfirm nodejs npm
3) 安装 Playwright:
   python3 -m pip install playwright
   python3 -m playwright install chromium
   python3 -m playwright install-deps chromium
4) 安装浏览器:
   sudo pacman -S --noconfirm chromium
   # 或安装 Edge:
   yay -S microsoft-edge-stable
5) 配置文件:
   cp .env.example .env
   cp config.json.example config.json
EOF
}

print_solution_wsl() {
    cat <<'EOF'
WSL 解决办法:
1) 安装 Python 与 pip:
   sudo apt-get update
   sudo apt-get install -y python3 python3-pip python3-venv
2) 安装 Node.js 与 npm (LTS):
   curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
   sudo apt-get install -y nodejs
3) 安装 Playwright:
   python3 -m pip install playwright
   python3 -m playwright install chromium
   python3 -m playwright install-deps chromium
4) 安装浏览器:
   sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
   # 或在 Windows 安装 Chrome/Edge 并在 WSL 使用 Linux 版本浏览器
5) 配置文件:
   cp .env.example .env
   cp config.json.example config.json
EOF
}

print_solution_windows() {
    cat <<'EOF'
Windows (PowerShell) 解决办法:
1) 安装 Python 与 Node:
   winget install Python.Python.3.11
   winget install OpenJS.NodeJS.LTS
2) 安装 Playwright:
   py -m pip install playwright
   py -m playwright install chromium
3) 安装浏览器:
   winget install Google.Chrome
   # 或
   winget install Microsoft.Edge
4) 配置文件（可选）:
   Copy-Item .env.example .env
   Copy-Item config.json.example config.json
EOF
}

print_solution_generic() {
    cat <<'EOF'
通用解决办法:
1) 安装 Python 3.10+ 与 pip
2) 安装 Node.js 与 npm
3) 安装 Playwright:
   python3 -m pip install playwright
   python3 -m playwright install chromium
4) 安装浏览器 Chrome 或 Edge
5) 配置文件（可选）:
   cp .env.example .env
   cp config.json.example config.json
EOF
}

if [ "${#MISSING_ITEMS[@]}" -ne 0 ]; then
    log "${RED}✗ 检测到缺失的环境/依赖:${NC}"
    for item in "${MISSING_ITEMS[@]}"; do
        echo "  - $item"
    done
    echo ""
    case "$OS_FAMILY" in
        macos)
            print_solution_macos
            ;;
        linux)
            if [ "$LINUX_ID" = "arch" ] || echo "$LINUX_LIKE" | grep -qi "arch"; then
                print_solution_linux_arch
            elif [ "$LINUX_ID" = "fedora" ] || [ "$LINUX_ID" = "rhel" ] || [ "$LINUX_ID" = "centos" ] || echo "$LINUX_LIKE" | grep -qi "rhel\|fedora"; then
                print_solution_linux_rpm
            else
                print_solution_linux_deb
            fi
            ;;
        wsl)
            print_solution_wsl
            ;;
        windows)
            print_solution_windows
            ;;
        *)
            print_solution_generic
            ;;
    esac
    exit 1
fi

log "${GREEN}✓ 环境与依赖检查通过${NC}"
echo "Python: $PYTHON_CMD ($PYTHON_VERSION)"
echo "Venv: $VENV_DIR"

# 1. 清理旧的 dist 目录
log "\n${YELLOW}[2/6] 清理旧的构建产物...${NC}"
if [ -d "dist" ]; then
    rm -rf dist
    log "${GREEN}✓ 已删除旧的 dist 目录${NC}"
else
    log "${GREEN}✓ dist 目录不存在，跳过清理${NC}"
fi

# 2. 检查并安装 Python 依赖
log "\n${YELLOW}[3/6] 检查 Python 依赖...${NC}"
if [ ! -f "requirements.txt" ]; then
    log "${RED}✗ 错误: requirements.txt 文件不存在${NC}"
    exit 1
fi

echo "正在安装 Python 依赖..."
"$PYTHON_CMD" -m pip install -r requirements.txt --quiet
if ! "$PYTHON_CMD" -m playwright --version >/dev/null 2>&1; then
    log "${RED}✗ 错误: Playwright 安装失败，请检查 requirements.txt 与 pip 输出${NC}"
    exit 1
fi
echo "正在安装/校验 Playwright Chromium..."
"$PYTHON_CMD" -m playwright install chromium
log "${GREEN}✓ Python 依赖安装完成${NC}"

# 3. 构建前端
log "\n${YELLOW}[4/6] 构建前端项目...${NC}"
if [ ! -d "web-ui" ]; then
    log "${RED}✗ 错误: web-ui 目录不存在${NC}"
    exit 1
fi

cd web-ui

# 检查 node_modules 是否存在
if [ ! -d "node_modules" ]; then
    echo "首次运行，正在安装前端依赖..."
    npm install
fi

echo "正在构建前端..."
npm run build

cd "$SCRIPT_DIR"

if [ ! -d "dist" ]; then
    log "${RED}✗ 错误: 前端构建失败，dist 目录未生成${NC}"
    exit 1
fi

log "${GREEN}✓ 前端构建完成，产物已输出到项目根目录 dist/${NC}"

# 4. 校验构建产物
log "\n${YELLOW}[5/6] 校验构建产物...${NC}"
log "${GREEN}✓ 已确认构建产物位于项目根目录 dist/${NC}"

# 5. 启动后端服务
log "\n${YELLOW}[6/6] 启动后端服务...${NC}"
log "${GREEN}========================================${NC}"
log "${GREEN}服务启动中...${NC}"
log "${GREEN}访问地址: http://localhost:8000${NC}"
log "${GREEN}API 文档: http://localhost:8000/docs${NC}"
log "${GREEN}========================================${NC}\n"

"$PYTHON_CMD" -m src.app
