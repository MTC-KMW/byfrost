#!/bin/bash
# Hybrid SSHFS Mount Manager
# Mounts coordination directories from Linux onto the Mac so task files,
# compound knowledge, and shared contracts are instantly visible.
#
# Code directories (backend/, web/, ios/) stay local for Xcode performance.
#
# Usage:
#   bash .agent-team/bridge/sshfs-mount.sh mount     # Mount all coordination dirs
#   bash .agent-team/bridge/sshfs-mount.sh unmount    # Unmount all
#   bash .agent-team/bridge/sshfs-mount.sh status     # Check mount status
#   bash .agent-team/bridge/sshfs-mount.sh remount    # Unmount + mount (reconnect)
#
# Requires: macFUSE + sshfs (brew install macfuse && brew install sshfs)
# Note: macFUSE requires a reboot after first install and a security approval
#       in System Preferences > Security & Privacy.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/../config.env" 2>/dev/null || {
    echo "ERROR: config.env not found. Run setup first."
    exit 1
}

# Directories to mount (coordination files only â€” no code dirs)
MOUNT_DIRS=("shared" "compound" "tasks" "pm" "qa")

# SSHFS options for reliability over Tailscale
SSHFS_OPTS=(
    -o reconnect                     # Auto-reconnect on disconnect
    -o ServerAliveInterval=15        # Send keepalive every 15s
    -o ServerAliveCountMax=3         # Disconnect after 3 missed keepalives
    -o StrictHostKeyChecking=no      # Don't prompt for host keys (Tailscale)
    -o NumberOfPasswordPrompts=0     # Fail fast if key auth fails
    -o follow_symlinks               # Follow symlinks on remote
    -o cache=yes                     # Enable caching for read performance
    -o cache_timeout=5               # Cache for 5s (balance freshness vs speed)
    -o attr_timeout=5                # Attribute cache timeout
    -o auto_cache                    # Invalidate cache when files are written
    -o volname=byfrost          # Volume name in Finder
)

ACTION="${1:-status}"

# ---------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------

check_deps() {
    if ! command -v sshfs &>/dev/null; then
        echo "ERROR: sshfs not found."
        echo ""
        echo "Install macFUSE and sshfs:"
        echo "  brew install macfuse"
        echo "  # Reboot, then approve in System Preferences > Security & Privacy"
        echo "  brew install sshfs"
        exit 1
    fi
}

check_ssh() {
    if ! ssh -o ConnectTimeout=5 "$LINUX_HOSTNAME" "echo ok" &>/dev/null; then
        echo "ERROR: Cannot SSH to $LINUX_HOSTNAME"
        echo "Is Tailscale running?"
        exit 1
    fi
}

# ---------------------------------------------------------------
# Mount
# ---------------------------------------------------------------

do_mount() {
    check_deps
    check_ssh

    echo "Mounting coordination directories from $LINUX_HOSTNAME..."
    echo ""

    local mounted=0
    local failed=0

    for dir in "${MOUNT_DIRS[@]}"; do
        local remote_path="$LINUX_PROJECT_PATH/$dir"
        local local_path="$PROJECT_ROOT/$dir"

        # Skip if already mounted
        if mount | grep -q "on $local_path "; then
            echo "  âœ“ $dir/ (already mounted)"
            ((mounted++))
            continue
        fi

        # Ensure remote dir exists
        if ! ssh "$LINUX_HOSTNAME" "test -d $remote_path" 2>/dev/null; then
            echo "  âš  $dir/ (remote directory not found, skipping)"
            continue
        fi

        # Ensure local mount point exists and is empty
        mkdir -p "$local_path"

        # If local dir has content, back it up
        if [ "$(ls -A "$local_path" 2>/dev/null)" ]; then
            local backup="${local_path}.local-backup"
            if [ ! -d "$backup" ]; then
                echo "  â†— $dir/ has local content, backing up to ${dir}.local-backup/"
                cp -r "$local_path" "$backup"
            fi
        fi

        # Mount
        if sshfs "$LINUX_HOSTNAME:$remote_path" "$local_path" "${SSHFS_OPTS[@]}" 2>/dev/null; then
            echo "  âœ“ $dir/ mounted"
            ((mounted++))
        else
            echo "  âœ— $dir/ FAILED to mount"
            ((failed++))
        fi
    done

    echo ""
    echo "Mounted: $mounted/${#MOUNT_DIRS[@]}"
    if [ $failed -gt 0 ]; then
        echo "Failed:  $failed"
        return 1
    fi

    echo ""
    echo "Coordination files are now live from Linux."
    echo "Code directories (backend/, web/, ios/) remain local."
}

# ---------------------------------------------------------------
# Unmount
# ---------------------------------------------------------------

do_unmount() {
    echo "Unmounting coordination directories..."
    echo ""

    local unmounted=0

    for dir in "${MOUNT_DIRS[@]}"; do
        local local_path="$PROJECT_ROOT/$dir"

        if mount | grep -q "on $local_path "; then
            if umount "$local_path" 2>/dev/null || diskutil unmount "$local_path" 2>/dev/null; then
                echo "  âœ“ $dir/ unmounted"
                ((unmounted++))

                # Restore local backup if it exists
                local backup="${local_path}.local-backup"
                if [ -d "$backup" ]; then
                    echo "    â†™ Restoring local content from ${dir}.local-backup/"
                    rm -rf "$local_path"
                    mv "$backup" "$local_path"
                fi
            else
                echo "  âœ— $dir/ FAILED to unmount (busy?)"
                echo "    Try: diskutil unmount force $local_path"
            fi
        else
            echo "  - $dir/ (not mounted)"
        fi
    done

    echo ""
    echo "Unmounted: $unmounted"
}

# ---------------------------------------------------------------
# Status
# ---------------------------------------------------------------

do_status() {
    echo "SSHFS Mount Status"
    echo "â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•"
    echo "Linux host: $LINUX_HOSTNAME"
    echo "Remote path: $LINUX_PROJECT_PATH"
    echo ""

    local mounted=0
    local total=${#MOUNT_DIRS[@]}

    for dir in "${MOUNT_DIRS[@]}"; do
        local local_path="$PROJECT_ROOT/$dir"
        if mount | grep -q "on $local_path "; then
            echo "  âœ“ $dir/ â†’ $LINUX_HOSTNAME:$LINUX_PROJECT_PATH/$dir"
            ((mounted++))

            # Test readability
            if ls "$local_path" &>/dev/null; then
                local count=$(ls -1 "$local_path" 2>/dev/null | wc -l | xargs)
                echo "    ($count items, readable)"
            else
                echo "    âš  mounted but not readable (stale?)"
            fi
        else
            echo "  âœ— $dir/ (not mounted)"
        fi
    done

    echo ""
    echo "Mounted: $mounted/$total"

    if [ $mounted -eq 0 ]; then
        echo ""
        echo "To mount: bash .agent-team/bridge/sshfs-mount.sh mount"
    elif [ $mounted -lt $total ]; then
        echo ""
        echo "Some mounts missing. To remount: bash .agent-team/bridge/sshfs-mount.sh remount"
    fi
}

# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

case "$ACTION" in
    mount)
        do_mount
        ;;
    unmount|umount)
        do_unmount
        ;;
    remount)
        do_unmount
        echo ""
        do_mount
        ;;
    status)
        do_status
        ;;
    *)
        echo "Usage: $0 {mount|unmount|remount|status}"
        exit 1
        ;;
esac
