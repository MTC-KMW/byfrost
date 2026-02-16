#!/bin/bash
# Byfrost Setup
# Run this on the Mac AFTER the main agent team setup is complete.
# It installs the bridge daemon, generates a shared secret, and
# configures launchd for auto-start.
#
# Usage: bash .agent-team/bridge/setup-bridge.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BRIDGE_DIR="$SCRIPT_DIR"
CONFIG_FILE="$PROJECT_ROOT/.agent-team/config.env"
BRIDGE_HOME="$HOME/.byfrost"
LOG_DIR="$BRIDGE_HOME/logs"
PLIST_NAME="com.byfrost.daemon"

echo "╔══════════════════════════════════════════════════╗"
echo "║   Byfrost Setup                              ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------
# Detect which machine we're on
# ---------------------------------------------------------------

OS_TYPE=$(uname -s)

if [ "$OS_TYPE" = "Darwin" ]; then
    echo "Detected: macOS (daemon host)"
    SETUP_MODE="mac"
elif [ "$OS_TYPE" = "Linux" ]; then
    echo "Detected: Linux (CLI client)"
    SETUP_MODE="linux"
else
    echo "ERROR: Unsupported OS: $OS_TYPE"
    exit 1
fi

# ---------------------------------------------------------------
# Load config
# ---------------------------------------------------------------

if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: config.env not found at $CONFIG_FILE"
    echo "Run the main agent team setup first."
    exit 1
fi
source "$CONFIG_FILE"

# ---------------------------------------------------------------
# Check Python + websockets
# ---------------------------------------------------------------

echo ""
echo "Checking dependencies..."

PYTHON=""
for p in python3 python; do
    if command -v "$p" &>/dev/null; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3 not found."
    exit 1
fi
echo "  ✓ Python: $($PYTHON --version)"

# Check/install websockets
if $PYTHON -c "import websockets" 2>/dev/null; then
    echo "  ✓ websockets already installed"
else
    echo "  Installing websockets..."
    if [ "$SETUP_MODE" = "linux" ]; then
        $PYTHON -m pip install websockets --break-system-packages -q 2>/dev/null \
            || $PYTHON -m pip install websockets -q
    else
        $PYTHON -m pip install websockets -q 2>/dev/null \
            || pip3 install websockets -q
    fi
    if $PYTHON -c "import websockets" 2>/dev/null; then
        echo "  ✓ websockets installed"
    else
        echo "  ERROR: Failed to install websockets"
        exit 1
    fi
fi

# ---------------------------------------------------------------
# Shared secret (NEVER stored in config.env / git)
# ---------------------------------------------------------------

echo ""
echo "Setting up authentication..."

mkdir -p "$BRIDGE_HOME"
mkdir -p "$LOG_DIR"
mkdir -p "$BRIDGE_HOME/certs"
chmod 700 "$BRIDGE_HOME"

SECRET_FILE="$BRIDGE_HOME/secret"

if [ "$SETUP_MODE" = "mac" ]; then
    # Mac generates the HMAC secret
    if [ -f "$SECRET_FILE" ]; then
        echo "  ✓ Existing HMAC secret found"
        BRIDGE_SECRET=$(cat "$SECRET_FILE")
    else
        BRIDGE_SECRET=$(openssl rand -hex 32)   # 256-bit
        echo "$BRIDGE_SECRET" > "$SECRET_FILE"
        chmod 600 "$SECRET_FILE"
        echo "  ✓ Generated new 256-bit HMAC secret"
    fi

    # Get bridge port
    BRIDGE_PORT="${BRIDGE_PORT:-9784}"
    read -p "  Bridge port [$BRIDGE_PORT]: " PORT_INPUT
    BRIDGE_PORT="${PORT_INPUT:-$BRIDGE_PORT}"

    # Add bridge config to config.env (NO SECRET — never in git)
    if ! grep -q "BRIDGE_PORT" "$CONFIG_FILE" 2>/dev/null; then
        echo "" >> "$CONFIG_FILE"
        echo "# Bridge Configuration (added by setup-bridge.sh)" >> "$CONFIG_FILE"
        echo "# NOTE: BRIDGE_SECRET is NOT here — it lives in ~/.byfrost/secret" >> "$CONFIG_FILE"
        echo "BRIDGE_PORT=\"$BRIDGE_PORT\"" >> "$CONFIG_FILE"
        echo "BRIDGE_HOST=\"$MAC_HOSTNAME\"" >> "$CONFIG_FILE"
        echo "BRIDGE_TIMEOUT=\"3600\"" >> "$CONFIG_FILE"
        echo "BRIDGE_HEARTBEAT=\"30\"" >> "$CONFIG_FILE"
        echo "BRIDGE_AUTO_GIT=\"true\"" >> "$CONFIG_FILE"
        echo "  ✓ Bridge config added to config.env (secret excluded)"
    else
        echo "  ✓ Bridge config already in config.env"
    fi

    # Remove any accidentally committed secret from config.env
    if grep -q "^BRIDGE_SECRET=" "$CONFIG_FILE" 2>/dev/null; then
        sed -i.bak '/^BRIDGE_SECRET=/d' "$CONFIG_FILE"
        echo "  ✓ Removed BRIDGE_SECRET from config.env (security fix)"
    fi

    # ---------------------------------------------------------------
    # TLS Certificate Generation
    # ---------------------------------------------------------------

    echo ""
    echo "Setting up TLS certificates..."

    CERTS_DIR="$BRIDGE_HOME/certs"

    if [ -f "$CERTS_DIR/ca.pem" ] && [ -f "$CERTS_DIR/server.pem" ]; then
        echo "  ✓ Existing certificates found"
    else
        echo "  Generating Certificate Authority..."
        $PYTHON -c "
import sys; sys.path.insert(0, '$BRIDGE_DIR')
from security import TLSManager
if TLSManager.generate_ca():
    print('  ✓ CA generated')
else:
    print('  ✗ CA generation failed'); sys.exit(1)

# Get hostname for SAN
import subprocess
hostname = subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip()
print(f'  Generating server certificate for {hostname}...')
if TLSManager.generate_server_cert(hostname):
    print('  ✓ Server certificate generated')
else:
    print('  ✗ Server cert generation failed'); sys.exit(1)

print('  Generating client certificate...')
if TLSManager.generate_client_cert():
    print('  ✓ Client certificate generated')
else:
    print('  ✗ Client cert generation failed'); sys.exit(1)
"
    fi

    # ---------------------------------------------------------------
    # Bridge mode selection
    # ---------------------------------------------------------------

    echo ""
    echo "Choose a bridge mode:"
    echo ""
    echo "  [1] Smart Bridge (recommended)"
    echo "      Auto git-pull before tasks, auto git-push after."
    echo "      No extra dependencies. Works everywhere."
    echo ""
    echo "  [2] Hybrid SSHFS + Smart Bridge"
    echo "      Mounts coordination files (shared/, compound/, tasks/, pm/, qa/)"
    echo "      from Linux via SSHFS for zero-delay visibility."
    echo "      Code dirs stay local. Requires macFUSE + sshfs."
    echo "      Smart Bridge auto-git still handles code sync."
    echo ""
    read -p "  Mode [1]: " BRIDGE_MODE
    BRIDGE_MODE="${BRIDGE_MODE:-1}"

    # Add mode to config
    if ! grep -q "BRIDGE_MODE" "$CONFIG_FILE" 2>/dev/null; then
        if [ "$BRIDGE_MODE" = "2" ]; then
            echo "BRIDGE_MODE=\"hybrid\"" >> "$CONFIG_FILE"
        else
            echo "BRIDGE_MODE=\"smart\"" >> "$CONFIG_FILE"
        fi
    fi

    echo ""
    echo "  ╔════════════════════════════════════════════════════════╗"
    echo "  ║  IMPORTANT: Copy secret + certs to Linux via scp:     ║"
    echo "  ║                                                        ║"
    echo "  ║  scp ~/.byfrost/secret LINUX:~/.byfrost/     ║"
    echo "  ║  scp ~/.byfrost/certs/ca.pem LINUX:~/.byfrost/certs/   ║"
    echo "  ║  scp ~/.byfrost/certs/client.* LINUX:~/.byfrost/certs/ ║"
    echo "  ║                                                        ║"
    echo "  ║  Or run setup on Linux: it will auto-copy via SSH.     ║"
    echo "  ╚════════════════════════════════════════════════════════╝"

elif [ "$SETUP_MODE" = "linux" ]; then
    # Linux gets secret + certs from the Mac via SSH/SCP
    MAC_HOST="${MAC_HOSTNAME:-}"
    if [ -z "$MAC_HOST" ]; then
        read -p "  Mac hostname (Tailscale): " MAC_HOST
    fi

    # Try to auto-copy secret from Mac
    if [ -f "$SECRET_FILE" ]; then
        echo "  ✓ Existing secret found"
    elif [ -n "$MAC_HOST" ]; then
        echo "  Copying secret from Mac..."
        if scp -o ConnectTimeout=5 "$MAC_HOST:~/.byfrost/secret" "$SECRET_FILE" 2>/dev/null; then
            chmod 600 "$SECRET_FILE"
            echo "  ✓ Secret copied from Mac"
        else
            echo "  ⚠ Could not copy secret from Mac."
            read -p "  Paste secret manually (or Enter to skip): " MANUAL_SECRET
            if [ -n "$MANUAL_SECRET" ]; then
                echo "$MANUAL_SECRET" > "$SECRET_FILE"
                chmod 600 "$SECRET_FILE"
                echo "  ✓ Secret saved"
            fi
        fi
    fi

    # Try to auto-copy TLS certs from Mac
    echo ""
    echo "Setting up TLS certificates..."
    CERTS_DIR="$BRIDGE_HOME/certs"
    mkdir -p "$CERTS_DIR"
    chmod 700 "$CERTS_DIR"

    if [ -f "$CERTS_DIR/ca.pem" ] && [ -f "$CERTS_DIR/client.pem" ]; then
        echo "  ✓ Existing client certificates found"
    elif [ -n "$MAC_HOST" ]; then
        echo "  Copying CA + client certs from Mac..."
        COPY_OK=true
        for cert_file in ca.pem client.key client.pem; do
            if scp -o ConnectTimeout=5 "$MAC_HOST:~/.byfrost/certs/$cert_file" "$CERTS_DIR/$cert_file" 2>/dev/null; then
                echo "  ✓ $cert_file"
            else
                echo "  ✗ $cert_file (failed)"
                COPY_OK=false
            fi
        done
        if [ "$COPY_OK" = "true" ]; then
            chmod 600 "$CERTS_DIR/client.key"
            echo "  ✓ TLS client certificates installed"
        else
            echo "  ⚠ Some certs failed to copy. Bridge will work without TLS"
            echo "    (Tailscale encryption still protects the connection)"
        fi
    else
        echo "  ⚠ No Mac hostname — cannot copy certs. Provide manually or re-run."
    fi
fi

# ---------------------------------------------------------------
# Mac: Install daemon + launchd
# ---------------------------------------------------------------

if [ "$SETUP_MODE" = "mac" ]; then
    echo ""
    echo "Installing daemon..."

    # Check tmux
    if command -v tmux &>/dev/null; then
        echo "  ✓ tmux found"
    else
        echo "  Installing tmux..."
        brew install tmux
        echo "  ✓ tmux installed"
    fi

    # Check claude
    CLAUDE_PATH=$(which claude 2>/dev/null || echo "")
    if [ -n "$CLAUDE_PATH" ]; then
        echo "  ✓ Claude Code found: $CLAUDE_PATH"
    else
        echo "  ⚠ Claude Code (claude) not found in PATH"
        echo "    The daemon will try to find it at runtime."
        CLAUDE_PATH="claude"
    fi

    # Make daemon executable
    chmod +x "$BRIDGE_DIR/byfrost-daemon.py"

    # Set up launchd plist
    echo ""
    echo "Setting up launchd (auto-start on login)..."

    PLIST_SRC="$BRIDGE_DIR/$PLIST_NAME.plist"
    PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
    DAEMON_PATH="$BRIDGE_DIR/byfrost-daemon.py"

    # Create customized plist
    sed \
        -e "s|DAEMON_PATH_PLACEHOLDER|$DAEMON_PATH|g" \
        -e "s|WORKDIR_PLACEHOLDER|$PROJECT_ROOT|g" \
        -e "s|LOGDIR_PLACEHOLDER|$LOG_DIR|g" \
        "$PLIST_SRC" > "$PLIST_DST"

    # If already loaded, unload first
    launchctl list | grep -q "$PLIST_NAME" 2>/dev/null && \
        launchctl unload "$PLIST_DST" 2>/dev/null

    launchctl load "$PLIST_DST"
    echo "  ✓ launchd plist installed and loaded"

    # Verify daemon started
    sleep 2
    if launchctl list | grep -q "$PLIST_NAME"; then
        echo "  ✓ Daemon is running"
    else
        echo "  ⚠ Daemon may not have started. Check:"
        echo "    cat $LOG_DIR/launchd-stderr.log"
    fi

    # Test connectivity
    echo ""
    echo "Testing daemon..."
    sleep 1
    if $PYTHON -c "
import asyncio, websockets, json, sys, os
sys.path.insert(0, '$BRIDGE_DIR')
from security import MessageSigner, TLSManager
async def test():
    try:
        secret = open(os.path.expanduser('~/.byfrost/secret')).read().strip()
        signer = MessageSigner(secret)
        # Try TLS first, fall back to plaintext
        ssl_ctx = None
        uri = 'ws://localhost:$BRIDGE_PORT'
        if TLSManager.has_server_certs():
            try:
                ssl_ctx = TLSManager.get_server_ssl_context()
                uri = 'wss://localhost:$BRIDGE_PORT'
            except: pass
        ws = await websockets.connect(uri, ssl=ssl_ctx)
        msg = signer.sign({'type': 'ping'})
        await ws.send(json.dumps(msg))
        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        proto = 'TLS' if ssl_ctx else 'plaintext'
        print(f'  ✓ Daemon responding on port $BRIDGE_PORT ({proto})')
        await ws.close()
    except Exception as e:
        print(f'  ⚠ Daemon test failed: {e}')
        sys.exit(1)
asyncio.run(test())
" 2>/dev/null; then
        true
    else
        echo "  ⚠ Could not connect. Daemon may still be starting."
        echo "    Check logs: cat $LOG_DIR/daemon.log"
    fi

    # ---------------------------------------------------------------
    # Hybrid SSHFS (mode 2 only)
    # ---------------------------------------------------------------

    if [ "$BRIDGE_MODE" = "2" ]; then
        echo ""
        echo "Setting up Hybrid SSHFS..."
        echo ""

        # Check macFUSE
        if [ -f "/Library/Filesystems/macfuse.fs/Contents/version.plist" ] || \
           [ -f "/Library/Frameworks/macFUSE.framework/Versions/Current" ]; then
            echo "  ✓ macFUSE found"
        else
            echo "  macFUSE not installed. Installing..."
            echo ""
            echo "  ⚠ macFUSE requires:"
            echo "    1. brew install macfuse"
            echo "    2. A REBOOT"
            echo "    3. Approve the extension in System Preferences > Security & Privacy"
            echo ""
            read -p "  Install macFUSE now? [y/N]: " INSTALL_FUSE
            if [[ "$INSTALL_FUSE" =~ ^[Yy] ]]; then
                brew install macfuse
                echo ""
                echo "  ╔═══════════════════════════════════════════════════╗"
                echo "  ║  macFUSE installed. You MUST:                     ║"
                echo "  ║  1. Reboot your Mac                               ║"
                echo "  ║  2. Approve the kernel extension in               ║"
                echo "  ║     System Preferences > Security & Privacy       ║"
                echo "  ║  3. Re-run this script after reboot               ║"
                echo "  ╚═══════════════════════════════════════════════════╝"
                echo ""
                echo "  SSHFS setup will continue after you reboot and re-run."
                SKIP_SSHFS=true
            else
                echo "  Skipping SSHFS setup. You can install later and run:"
                echo "    bash .agent-team/bridge/sshfs-mount.sh mount"
                SKIP_SSHFS=true
            fi
        fi

        # Check/install sshfs
        if [ "${SKIP_SSHFS:-false}" = "false" ]; then
            if command -v sshfs &>/dev/null; then
                echo "  ✓ sshfs found"
            else
                echo "  Installing sshfs..."
                brew install sshfs
                if command -v sshfs &>/dev/null; then
                    echo "  ✓ sshfs installed"
                else
                    echo "  ✗ sshfs installation failed"
                    SKIP_SSHFS=true
                fi
            fi
        fi

        # Add Linux project path to config if not present
        if ! grep -q "LINUX_PROJECT_PATH" "$CONFIG_FILE" 2>/dev/null; then
            echo ""
            read -p "  Linux project path [$HOME/project]: " LINUX_PATH
            LINUX_PATH="${LINUX_PATH:-$HOME/project}"
            echo "LINUX_PROJECT_PATH=\"$LINUX_PATH\"" >> "$CONFIG_FILE"
            echo "LINUX_HOSTNAME=\"${LINUX_HOSTNAME:-linux}\"" >> "$CONFIG_FILE"
            echo "  ✓ Linux paths added to config.env"
        fi

        # Do the initial mount
        if [ "${SKIP_SSHFS:-false}" = "false" ]; then
            echo ""
            chmod +x "$BRIDGE_DIR/sshfs-mount.sh"
            bash "$BRIDGE_DIR/sshfs-mount.sh" mount
        fi

        # Create launchd plist for auto-mount on login
        if [ "${SKIP_SSHFS:-false}" = "false" ]; then
            MOUNT_PLIST="$HOME/Library/LaunchAgents/com.byfrost.sshfs.plist"
            cat > "$MOUNT_PLIST" <<MOUNTPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.byfrost.sshfs</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BRIDGE_DIR/sshfs-mount.sh</string>
        <string>mount</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/sshfs-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/sshfs-stderr.log</string>
    <key>StartInterval</key>
    <integer>300</integer>
</dict>
</plist>
MOUNTPLIST
            launchctl list | grep -q "com.byfrost.sshfs" 2>/dev/null && \
                launchctl unload "$MOUNT_PLIST" 2>/dev/null
            launchctl load "$MOUNT_PLIST"
            echo ""
            echo "  ✓ SSHFS auto-mount installed (checks every 5 min)"
        fi
    fi

fi

# ---------------------------------------------------------------
# Linux: Install CLI
# ---------------------------------------------------------------

if [ "$SETUP_MODE" = "linux" ]; then
    echo ""
    echo "Installing bridge CLI..."

    CLI_SRC="$BRIDGE_DIR/bridge"
    chmod +x "$CLI_SRC"

    # Symlink to a PATH location
    CLI_DST=""
    for bindir in "$HOME/.local/bin" "$HOME/bin" "/usr/local/bin"; do
        if [ -d "$bindir" ]; then
            CLI_DST="$bindir/bridge"
            break
        fi
    done

    if [ -z "$CLI_DST" ]; then
        mkdir -p "$HOME/.local/bin"
        CLI_DST="$HOME/.local/bin/bridge"
        echo "  Created ~/.local/bin/ — add to PATH if not already:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi

    ln -sf "$CLI_SRC" "$CLI_DST"
    echo "  ✓ CLI installed: $CLI_DST"

    # Test connectivity to Mac daemon
    BRIDGE_HOST="${BRIDGE_HOST:-$MAC_HOSTNAME}"
    BRIDGE_PORT="${BRIDGE_PORT:-9784}"

    echo ""
    echo "Testing connectivity to Mac daemon..."
    echo "  Host: $BRIDGE_HOST"
    echo "  Port: $BRIDGE_PORT"

    if $PYTHON -c "
import asyncio, websockets, json, sys, os
sys.path.insert(0, '$BRIDGE_DIR')
from security import MessageSigner, TLSManager
async def test():
    try:
        secret_path = os.path.expanduser('~/.byfrost/secret')
        secret = open(secret_path).read().strip() if os.path.exists(secret_path) else ''
        if not secret:
            print('  ⚠ No secret found — cannot authenticate')
            return
        signer = MessageSigner(secret)
        ssl_ctx = None
        uri = 'ws://$BRIDGE_HOST:$BRIDGE_PORT'
        if TLSManager.has_client_certs():
            try:
                ssl_ctx = TLSManager.get_client_ssl_context()
                uri = 'wss://$BRIDGE_HOST:$BRIDGE_PORT'
            except: pass
        ws = await websockets.connect(uri, ssl=ssl_ctx)
        msg = signer.sign({'type': 'ping'})
        await ws.send(json.dumps(msg))
        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        proto = 'TLS' if ssl_ctx else 'plaintext'
        print(f'  ✓ Connected to Mac daemon ({proto})')
        await ws.close()
    except Exception as e:
        print(f'  ⚠ Cannot connect: {e}')
        print(f'  Make sure the daemon is running on the Mac.')
asyncio.run(test())
" 2>/dev/null; then
        true
    else
        echo "  ⚠ Connection test failed. This is normal if Mac setup hasn't been done yet."
    fi
fi

# ---------------------------------------------------------------
# Done
# ---------------------------------------------------------------

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Bridge setup complete!                         ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

if [ "$SETUP_MODE" = "mac" ]; then
    echo "The daemon is running and will auto-start on login."
    echo ""
    echo "Next steps:"
    echo "  1. git add .agent-team/config.env && git commit -m 'feat: add bridge config'"
    echo "  2. git push"
    echo "  3. On Linux: git pull && bash .agent-team/bridge/setup-bridge.sh"
    echo ""
    echo "Management:"
    echo "  Stop:     launchctl unload ~/Library/LaunchAgents/$PLIST_NAME.plist"
    echo "  Start:    launchctl load ~/Library/LaunchAgents/$PLIST_NAME.plist"
    echo "  Logs:     tail -f $LOG_DIR/daemon.log"
    echo "  Status:   launchctl list | grep byfrost"
elif [ "$SETUP_MODE" = "linux" ]; then
    echo "The bridge CLI is installed."
    echo ""
    echo "Usage:"
    echo "  bridge ping                  — verify Mac daemon is reachable"
    echo "  bridge send \"prompt\"         — send task to Mac agent"
    echo "  bridge send --priority high  — high-priority task"
    echo "  bridge status                — check queue and active tasks"
    echo "  bridge attach                — stream output from active task"
    echo "  bridge cancel <task-id>      — cancel a task"
    echo "  bridge followup <id> \"text\"  — send follow-up to running task"
    echo "  bridge logs -f               — tail daemon logs from Mac"
fi
echo ""
