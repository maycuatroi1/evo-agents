#!/bin/bash
# Make a fresh Ubuntu 22.04 or 24.04 machine a node of an evo-agents worker pool, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/maycuatroi1/evo-agents/main/deploy/worker/bootstrap.sh -o bootstrap.sh
#   EVO_HUB_URL=https://hub.example.org EVO_JOIN_TOKEN=evj_... bash bootstrap.sh
#
# The join token comes from `evo-agents worker pool token create POOL`. The same file is cloud-init user-data:
# `evo-agents worker pool user-data POOL` makes a new join token and prints this script with the hub and the token
# written in below, to paste as the user-data of a new machine.
#
# Run as root (cloud-init runs user-data as root): it installs the system packages with apt (git, tmux, jq, ripgrep,
# and the libraries and fonts Chromium needs), creates the user `evo` (EVO_BOOTSTRAP_USER) without sudo and outside
# the docker group, turns lingering on so its services run without a session, and installs the rest as that user.
# Run as any other user: it installs in that user's home alone and only says which system packages are missing.
#
# In the user's home: uv with Python 3.12, Node 22 with pnpm through corepack, Claude Code with its native
# installer, Codex and opencode with npm under ~/.local, evo-agents from the tag nightly with `uv tool`, then
# `evo-agents worker join` with the token and `evo-agents worker service install` (a systemd user unit whose PATH
# has ~/.local/bin). Running it again on the same machine skips what is there and never joins twice: a machine that
# is a worker already keeps its worker, and the token may then be left out.
set -euo pipefail

# evo-bootstrap settings: `evo-agents worker pool user-data` writes the hub and the join token here.

EVO_HUB_URL=${EVO_HUB_URL:-}
EVO_JOIN_TOKEN=${EVO_JOIN_TOKEN:-}
EVO_BOOTSTRAP_USER=${EVO_BOOTSTRAP_USER:-evo}
EVO_AK_REQUIREMENT=${EVO_AK_REQUIREMENT:-evo-ak[graphify,worker] @ git+https://github.com/maycuatroi1/evo-agents@nightly}
export EVO_HUB_URL EVO_JOIN_TOKEN EVO_BOOTSTRAP_USER EVO_AK_REQUIREMENT

# what any node needs from apt
SYSTEM_PACKAGES="ca-certificates curl git tmux jq ripgrep xz-utils unzip"
# what Chromium needs on an Ubuntu without a desktop (evo_agents/worker/browser/install.py LINUX_PACKAGES), and fonts;
# a name ending in t64 is the 24.04 one, and 22.04 has it without the suffix
CHROMIUM_PACKAGES="libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libgbm1 libxcomposite1
libxdamage1 libxfixes3 libxi6 libxrandr2 libxrender1 libcups2t64 libcairo2 libpango-1.0-0 libavahi-common3
libavahi-client3 libxcb-render0 libxcb-shm0 libpixman-1-0 libthai0 libharfbuzz0b libdatrie1 libgraphite2-3
libavahi-common-data libfontconfig1 fontconfig-config fonts-dejavu-core libwayland-server0 libxcb-randr0 libxkbcommon0
libnss3 libnspr4 libdrm2 libxshmfence1 fontconfig fonts-liberation fonts-noto-core"
UNIT=evo-agents-worker.service

say() { printf 'evo-bootstrap: %s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

check_system() {
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}-${VERSION_ID:-}" in
            ubuntu-22.04 | ubuntu-24.04) ;;
            *) say "this is ${PRETTY_NAME:-an unknown system}, not Ubuntu 22.04 or 24.04; going on anyway" ;;
        esac
    fi
}

# The apt name of a package on this release: the t64 name on 24.04, the plain one on 22.04.
apt_name() {
    if apt-cache show --no-all-versions "$1" >/dev/null 2>&1; then
        printf '%s\n' "$1"
    elif [ "${1%t64}" != "$1" ] && apt-cache show --no-all-versions "${1%t64}" >/dev/null 2>&1; then
        printf '%s\n' "${1%t64}"
    else
        say "apt has no package $1 here; skipping it"
    fi
}

install_system_packages() {
    export DEBIAN_FRONTEND=noninteractive
    say "installing the system packages with apt"
    apt-get update -q
    local wanted=() name package
    # shellcheck disable=SC2086 # the lists are words
    for package in $SYSTEM_PACKAGES $CHROMIUM_PACKAGES; do
        name=$(apt_name "$package")
        if [ -n "$name" ]; then wanted+=("$name"); fi
    done
    apt-get install -y -q --no-install-recommends "${wanted[@]}"
}

# The user the root run installs for: created without sudo and outside the docker group, lingering on.
make_user() {
    local user=$EVO_BOOTSTRAP_USER group
    if id "$user" >/dev/null 2>&1; then
        say "user $user is there already"
    else
        say "creating user $user, without sudo"
        useradd --create-home --shell /bin/bash "$user"
    fi
    for group in sudo admin wheel docker; do
        if id -nG "$user" | tr ' ' '\n' | grep -qx "$group"; then
            say "warning: user $user is in the group $group; a worker should not be (gpasswd -d $user $group)"
        fi
    done
    if has loginctl; then
        loginctl enable-linger "$user"
        local uid
        uid=$(id -u "$user")
        local waited=0
        until [ -S "/run/user/$uid/bus" ] || [ "$waited" -ge 30 ]; do
            sleep 1
            waited=$((waited + 1))
        done
    fi
}

# Node 22 from nodejs.org under ~/.local/lib/nodejs, its checksum checked, with node, npm, npx and corepack in
# ~/.local/bin.
install_node() {
    if has node && node --version | grep -q '^v22\.'; then
        say "Node $(node --version) is there already"
        return
    fi
    local arch
    case "$(uname -m)" in
        x86_64) arch=x64 ;;
        aarch64 | arm64) arch=arm64 ;;
        *) die "no Node 22 build for $(uname -m)" ;;
    esac
    local base=https://nodejs.org/dist/latest-v22.x work file
    work=$(mktemp -d)
    curl -fsSL "$base/SHASUMS256.txt" -o "$work/SHASUMS256.txt"
    file=$(grep -oE "node-v22\.[0-9.]+-linux-$arch\.tar\.xz" "$work/SHASUMS256.txt" | head -n 1)
    [ -n "$file" ] || die "nodejs.org lists no Node 22 for linux-$arch"
    say "installing $file"
    curl -fsSL "$base/$file" -o "$work/$file"
    (cd "$work" && grep " $file\$" SHASUMS256.txt | sha256sum -c --quiet -)
    mkdir -p "$HOME/.local/lib/nodejs"
    tar -xJf "$work/$file" -C "$HOME/.local/lib/nodejs"
    ln -sfn "$HOME/.local/lib/nodejs/${file%.tar.xz}" "$HOME/.local/lib/nodejs/current"
    local tool
    for tool in node npm npx corepack; do
        ln -sfn "$HOME/.local/lib/nodejs/current/bin/$tool" "$HOME/.local/bin/$tool"
    done
    rm -rf "$work"
}

install_pnpm() {
    if has pnpm; then
        say "pnpm is there already"
        return
    fi
    say "enabling pnpm through corepack"
    corepack enable --install-directory "$HOME/.local/bin" pnpm
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm --version >/dev/null
}

install_npm_tool() {  # command, package
    if has "$1"; then
        say "$1 is there already"
        return
    fi
    say "installing $2 with npm under ~/.local"
    npm install --global --prefix "$HOME/.local" --no-fund --no-audit "$2"
}

# The part that runs as the worker's user, in its home. Root runs it through runuser, which keeps the environment.
user_phase() {
    set -euo pipefail
    cd "$HOME"
    mkdir -p "$HOME/.local/bin"
    export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    fi
    local state="${EVO_WORKER_HOME:-$HOME/.evo/worker}" joined=no
    if [ -f "$state/config.json" ] && [ -f "$state/token" ]; then
        joined=yes
    elif [ -z "$EVO_HUB_URL" ] || [ -z "$EVO_JOIN_TOKEN" ]; then
        die "this machine is not a worker yet: set EVO_HUB_URL to the hub and EVO_JOIN_TOKEN to a join token from \
\`evo-agents worker pool token create POOL\`"
    fi

    if [ "$(id -u)" -ne 0 ]; then
        local missing=() tool
        for tool in git tmux jq rg curl; do
            has "$tool" || missing+=("$tool")
        done
        [ "${#missing[@]}" -eq 0 ] || say "warning: missing ${missing[*]}; root installs them (apt-get install git \
tmux jq ripgrep curl), or run this script as root"
        has git || die "git is missing, and a worker cannot run without it"
        has curl || die "curl is missing, and this script downloads with it"
    fi

    if has uv; then
        say "uv is there already"
    else
        say "installing uv"
        curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    fi
    uv python install 3.12
    install_node
    install_pnpm
    if has claude; then
        say "Claude Code is there already"
    else
        say "installing Claude Code with its native installer"
        curl -fsSL https://claude.ai/install.sh | bash
    fi
    install_npm_tool codex @openai/codex
    install_npm_tool opencode opencode-ai
    if has evo-agents; then
        say "evo-agents is there already; its daemon updates it to nightly itself"
    else
        say "installing evo-agents from the tag nightly"
        uv tool install --python 3.12 "$EVO_AK_REQUIREMENT"
    fi

    if [ "$joined" = yes ]; then
        say "this machine is a worker already ($state/config.json); not joining again"
    else
        say "joining $EVO_HUB_URL with the join token"
        evo-agents worker join --url "$EVO_HUB_URL"  # the token is read from EVO_JOIN_TOKEN, out of the process list
    fi
    if [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT" ]; then
        say "the service $UNIT is installed already"
    else
        if [ "$(id -u)" -ne 0 ] && [ ! -e "/var/lib/systemd/linger/$(id -un)" ] && has loginctl; then
            loginctl enable-linger "$(id -un)" || say "warning: lingering is off, so the worker stops at logout \
(root: loginctl enable-linger $(id -un))"
        fi
        evo-agents worker service install
    fi
    say "done: \`evo-agents worker status\` shows the worker, and the hub hands it runs once its doctor is clean"
}

main() {
    check_system
    if [ "$(id -u)" -ne 0 ]; then
        user_phase
        return
    fi
    install_system_packages
    make_user
    say "installing for user $EVO_BOOTSTRAP_USER"
    local home
    home=$(getent passwd "$EVO_BOOTSTRAP_USER" | cut -d: -f6)
    # runuser without --login keeps the environment (EVO_JOIN_TOKEN among it) and sets HOME, USER and LOGNAME
    runuser -u "$EVO_BOOTSTRAP_USER" -- env HOME="$home" XDG_RUNTIME_DIR="/run/user/$(id -u "$EVO_BOOTSTRAP_USER")" \
        bash -c "UNIT=$UNIT; $(declare -f say die has install_node install_pnpm install_npm_tool user_phase); user_phase"
}

main "$@"
