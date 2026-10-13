#!/usr/bin/env bash

set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/open-ai-canvas}"
REPOSITORY="${REPOSITORY:-ddcat-ai/open-ai-canvas}"
SOCKET_DIR="${CANVAS_UPDATER_SOCKET_DIR:-/run/open-ai-canvas-updater}"
UPDATER_BIN="/usr/local/bin/open-ai-canvas-host-updater"
UPDATER_ENV="/etc/open-ai-canvas-updater.env"
UPDATER_SERVICE="/etc/systemd/system/open-ai-canvas-updater.service"

fail() {
    printf 'Host Updater 安装失败：%s\n' "$1" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || fail "请使用 sudo 运行"
    [[ "$(uname -s)" == "Linux" ]] || fail "仅支持 Linux 服务器"
    command -v systemctl >/dev/null 2>&1 || fail "服务器必须使用 systemd"
    command -v curl >/dev/null 2>&1 || fail "缺少 curl"
    command -v sha256sum >/dev/null 2>&1 || fail "缺少 sha256sum"
    command -v openssl >/dev/null 2>&1 || fail "缺少 openssl"
    [[ -f "${INSTALL_DIR}/.env" ]] || fail "未找到 ${INSTALL_DIR}/.env"
    [[ -f "${INSTALL_DIR}/docker-compose.deploy.yml" ]] || fail "未找到部署 Compose"
    local backend_image web_image
    backend_image="$(sed -n 's/^CANVAS_BACKEND_IMAGE=//p' "${INSTALL_DIR}/.env" | tail -n 1)"
    web_image="$(sed -n 's/^CANVAS_WEB_IMAGE=//p' "${INSTALL_DIR}/.env" | tail -n 1)"
    [[ "$backend_image" =~ ^ghcr\.io/[^[:space:]]+@sha256:[a-f0-9]{64}$ ]] || fail "CANVAS_BACKEND_IMAGE 必须固定为 GHCR digest"
    [[ "$web_image" =~ ^ghcr\.io/[^[:space:]]+@sha256:[a-f0-9]{64}$ ]] || fail "CANVAS_WEB_IMAGE 必须固定为 GHCR digest"
}

read_image_tag() {
    local configured
    configured="$(sed -n 's/^CANVAS_IMAGE_TAG=//p' "${INSTALL_DIR}/.env" | tail -n 1)"
    [[ -n "$configured" && "$configured" != "latest" ]] || fail "请先把 CANVAS_IMAGE_TAG 固定为已发布版本"
    if [[ "$configured" == v* ]]; then
        RELEASE_TAG="$configured"
    else
        RELEASE_TAG="v${configured}"
    fi
}

install_binary() {
    local arch asset temporary checksum_file expected
    case "$(uname -m)" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) fail "不支持的 CPU 架构：$(uname -m)" ;;
    esac
    asset="open-ai-canvas-host-updater-linux-${arch}"
    temporary="$(mktemp)"
    checksum_file="$(mktemp)"
    curl -fsSL "https://github.com/${REPOSITORY}/releases/download/${RELEASE_TAG}/${asset}" -o "$temporary"
    curl -fsSL "https://github.com/${REPOSITORY}/releases/download/${RELEASE_TAG}/SHA256SUMS" -o "$checksum_file"
    expected="$(awk -v asset="$asset" '$2 == asset { print $1 }' "$checksum_file")"
    [[ "$expected" =~ ^[a-f0-9]{64}$ ]] || fail "Release 校验清单缺少 ${asset}"
    printf '%s  %s\n' "$expected" "$temporary" | sha256sum -c - >/dev/null || fail "Host Updater SHA-256 校验失败"
    install -m 0755 "$temporary" "$UPDATER_BIN"
    rm -f "$temporary" "$checksum_file"
}

ensure_token() {
    local token temporary
    token="$(sed -n 's/^CANVAS_UPDATER_TOKEN=//p' "${INSTALL_DIR}/.env" | tail -n 1)"
    if [[ -z "$token" ]]; then
        token="$(openssl rand -hex 32)"
        temporary="$(mktemp "${INSTALL_DIR}/.env.XXXXXX")"
        awk -v token="$token" '
            BEGIN { updated=0 }
            /^CANVAS_UPDATER_TOKEN=/ { print "CANVAS_UPDATER_TOKEN=" token; updated=1; next }
            { print }
            END { if (!updated) print "CANVAS_UPDATER_TOKEN=" token }
        ' "${INSTALL_DIR}/.env" > "$temporary"
        chmod --reference="${INSTALL_DIR}/.env" "$temporary"
        mv "$temporary" "${INSTALL_DIR}/.env"
    fi
    [[ ${#token} -ge 32 ]] || fail "CANVAS_UPDATER_TOKEN 长度不足"
    umask 077
    printf 'CANVAS_UPDATER_TOKEN=%s\nCANVAS_UPDATER_INSTALL_DIR=%s\nCANVAS_UPDATER_SOCKET=%s/updater.sock\n' "$token" "$INSTALL_DIR" "$SOCKET_DIR" > "$UPDATER_ENV"
}

# updater 二进制的默认健康检查地址是回环 http://127.0.0.1:<port>/api/health/ready。
# 一旦 CANVAS_WEB_HOST_IP 绑到非回环地址（典型是 Tailscale 的 100.x），回环上就没有
# 监听，在线更新会以「当前运行版本与部署配置不一致或服务未就绪」失败。二进制不在本仓库内，
# 无法改其默认值，这里按部署编排实际配置推导出正确的健康检查地址。
ensure_health_url() {
    local host_ip http_port health_url current
    host_ip="$(sed -n 's/^CANVAS_WEB_HOST_IP=//p' "${INSTALL_DIR}/.env" | tail -n 1)"
    http_port="$(sed -n 's/^CANVAS_HTTP_PORT=//p' "${INSTALL_DIR}/.env" | tail -n 1)"
    http_port="${http_port:-3000}"

    if [[ -z "$host_ip" || "$host_ip" == "0.0.0.0" ]]; then
        # 监听全部接口时回环必然可达，用回环可以避免把健康检查流量打到外部网卡。
        health_url="http://127.0.0.1:${http_port}/api/health/ready"
    else
        health_url="http://${host_ip}:${http_port}/api/health/ready"
    fi

    current="$(sed -n 's/^CANVAS_UPDATER_HEALTH_URL=//p' "$UPDATER_ENV" | tail -n 1)"
    [[ "$current" == "$health_url" ]] && return

    umask 077
    # 手工配置过（与推导值不同）时保留用户的设置，不覆盖。
    if [[ -n "$current" ]]; then
        printf '保留手工配置的 CANVAS_UPDATER_HEALTH_URL：%s\n' "$current"
        return
    fi
    printf 'CANVAS_UPDATER_HEALTH_URL=%s\n' "$health_url" >> "$UPDATER_ENV"
    printf '健康检查地址：%s\n' "$health_url"
}

install_service() {
    local temporary_service
    install -d -m 0755 "$SOCKET_DIR"
    install -d -m 0700 /var/lib/open-ai-canvas-updater "${INSTALL_DIR}/backups"
    temporary_service="$(mktemp)"
    printf '%s\n' \
        '[Unit]' \
        'Description=Open AI Canvas Host Updater' \
        'After=docker.service network-online.target' \
        'Requires=docker.service' \
        'Wants=network-online.target' \
        '' \
        '[Service]' \
        'Type=simple' \
        "EnvironmentFile=${UPDATER_ENV}" \
        "ExecStart=${UPDATER_BIN}" \
        'Restart=on-failure' \
        'RestartSec=5s' \
        'NoNewPrivileges=true' \
        'PrivateTmp=true' \
        'ProtectHome=true' \
        'ProtectSystem=full' \
        "ReadWritePaths=${INSTALL_DIR} /var/lib/open-ai-canvas-updater ${SOCKET_DIR} /usr/local/bin" \
        '' \
        '[Install]' \
        'WantedBy=multi-user.target' > "$temporary_service"
    install -m 0644 "$temporary_service" "$UPDATER_SERVICE"
    rm -f "$temporary_service"
    systemctl daemon-reload
    systemctl enable --now open-ai-canvas-updater.service
    systemctl restart open-ai-canvas-updater.service
}

main() {
    require_root
    read_image_tag
    install_binary
    ensure_token
    ensure_health_url
    install_service
    printf 'Host Updater 已安装，Socket：%s/updater.sock\n' "$SOCKET_DIR"
    printf '请重建 backend 容器，使 Token 与 Socket 挂载生效。\n'
}

main "$@"
