# TickDesk 云端 relay

独立 Rust 2021 工程，需要 Rust 1.95+。relay 使用 v2 角色凭据接入 App 和本地服务，维护在线状态，并在配对内用有界内存转发。发送方在收到并验证对端 receipt 之前，仍负责保存和重发投递。业务 ack 由本地服务单独生成并投递。

## 本地启动

从 `relay` 目录运行。开发入口 `ws://127.0.0.1:18443/relay/v2/connect` 只监听 loopback。在测试机本地生成两条临时凭据，把两份明文分别提供给对应端点。下面示例把临时凭据写进同一个临时目录，联调后删除此目录。

```powershell
$relayDev = Join-Path ([IO.Path]::GetTempPath()) ("tickdesk-relay-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $relayDev | Out-Null
$relayRng = [Security.Cryptography.RandomNumberGenerator]::Create()
$relaySha = [Security.Cryptography.SHA256]::Create()
try {
    $relayRoleFile = foreach ($relayRole in 'app', 'local') {
        $relayBytes = New-Object byte[] 32
        $relayRng.GetBytes($relayBytes)
        $relayToken = [Convert]::ToBase64String($relayBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
        [IO.File]::WriteAllText((Join-Path $relayDev "$relayRole.token"), $relayToken)
        $relayDigest = [BitConverter]::ToString($relaySha.ComputeHash([Text.Encoding]::ASCII.GetBytes($relayToken))).Replace('-','').ToLowerInvariant()
        "[pairs.demo.$relayRole]"
        "sha256 = '$relayDigest'"
    }
    $relayRoleFile | Set-Content -Encoding ascii (Join-Path $relayDev 'roles.toml')
} finally {
    $relayRng.Dispose()
    $relaySha.Dispose()
}
Copy-Item config/dev.toml (Join-Path $relayDev 'relay.toml')
$relayConfig = Join-Path $relayDev 'relay.toml'
cargo run --locked -- --config $relayConfig --check-config
cargo run --locked -- --config $relayConfig
```

`--check-config` 校验 TOML、角色摘要、契约版本和传输 Schema，以及已配置的 TLS PEM，完成后退出。配置文件中的相对路径以该文件所在目录为基准。开发环境的 `/health/live` 与 `/health/ready` 都在 18443，指标在 `http://127.0.0.1:18444/metrics`。ready 表示监听器可响应、至少一个配对具备两种有效角色凭据，且进程未开始关闭。任何一端可以离线。

## 角色配置和轮换

`roles_file` 指向只读 TOML 文件，见 [roles.example.toml](config/roles.example.toml)。用 32 字节 CSPRNG 随机值生成 43 字符的无 padding base64url Bearer。云端保存 Bearer **编码后的 ASCII** 的 SHA-256 小写十六进制摘要。每个摘要只对应一个 pair/role。生产明文凭据由管理员分别离线交给对应端点，按共享契约从可信渠道给 App 配置 TLS SPKI pin。

- 正常轮换时，把新摘要填入该角色的 `sha256`。如需重叠，使用 `[pairs.demo.app.previous]`（或 local），填写旧 `sha256` 和绝对 Unix 秒 `expires_at_unix`，最长 600 秒。超过该时限的配置会被拒绝。时间到后旧凭据停止接受，已建立和等待 hello 的旧连接关闭。
- 紧急撤销时，移除旧摘要或移除整段角色配置。保留的另一角色可以连接并看到对端离线。用空 `[pairs]` 撤销全部配对，readiness 变为 503。
- 在同目录写入完整候选文件，再用原子替换覆盖 `roles_file`。relay 约每秒检查一次，整份校验成功后在同一会话锁内应用。坏文件保留上一次有效配置，撤销的连接收到 4401。监听地址或 TLS 证书变更通过重启应用。

## TLS 容器部署

容器以 UID/GID 10001 运行。准备只读挂载目录，内含 `roles.toml`、`relay-cert.pem`、`relay-key.pem` 和 `health-ca.pem`，确保运行账号可读。证书 SAN 必须覆盖端点实际连接的主机名或 IP。将**目录**挂载进容器，外部原子替换角色文件后，容器可读到新文件。默认生产配置见 [tls.example.toml](config/tls.example.toml)。

```bash
docker build -t tickdesk-relay:v2 .
RELAY_HOST=relay.example.com
docker run --rm --name tickdesk-relay --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true -p 8443:8443 \
  --mount type=bind,src=/srv/tickdesk/relay-secrets,dst=/run/secrets/tickdesk,readonly \
  -e "TICKDESK_HEALTH_URL=https://${RELAY_HOST}:8443/health/ready" \
  tickdesk-relay:v2
```

将 `RELAY_HOST` 替换为证书 SAN 中的实际主机名或 IP。容器内健康检查连接 loopback 的 8443，验证配置 URL 的主机名或 IP 与 `health-ca.pem` 的信任链。App 和本地服务另外按契约执行 SPKI pin 校验。公网只发布 TLS 8443。指标使用独立 loopback listener，仅聚合角色、队列、心跳和错误码。查看容器指标可运行 `docker exec tickdesk-relay curl -fsS http://127.0.0.1:18444/metrics`。

## App 与本地服务联调

1. 两端分别用自己的 Bearer 发起 HTTP/1.1 WS 握手。生产地址为 `wss://<主机>:8443/relay/v2/connect`，子协议固定 `tickdesk.v2`。401 检查当前角色凭据，426 检查子协议，429 依 `Retry-After` 退避。
2. 升级后 5 秒内发送 hello。两端提供持久 incarnation，本地必须携带当前 serverEpoch，App 首次可用 null。核对 welcome 的 pairId、role、relayBootId 与 connectionId，按 presence 中的完整对端状态更新路由目标。收到 4409 后停止自动争抢连接。
3. 使用 `contracts/v2/relay-fixtures.json` 的 `deliver_command`、`file_chunk` 和 `receipt` 检查字节与方向。接收方持久化成功后原样回显 deliveryId、payloadHash、fromIncarnation、toIncarnation。业务 ack 按反方向送一个新的 deliver，有独立的 receipt。App 和本地服务分别维持业务 16 条、文件 4 块的未确认窗口。
4. 断开对端确认收到离线 presence。用相同 incarnation 重连并重发原投递，保留幂等标识。更换 incarnation 后先隔离旧投递，再按恢复流程合并。测试配置撤销应关闭相关连接并拒绝旧 Bearer。端点每 15 秒发 Ping，收到对方 Ping 立即回同 payload 的 Pong。45 秒未见匹配 Pong 时应关闭并退避。
5. WS 只用文本 JSON。整条消息含分片合计上限 128 KiB，组装最多 10 秒，业务 payload 上限 64 KiB，文件块解码后上限 32 KiB。每个接收连接最多排队 32 条、1 MiB，控制帧预留 4 条和 16 KiB，文件排队最多 4 条，业务与文件按 8:1 调度。队满会返回 BUSY 或关闭 1013，端点保留未 receipt 的原投递。

自动联调用临时自签证书、临时角色摘要和系统分配端口。测试端点分别连接同一个实际 Rust relay，通过共享 fixtures 交换消息。

```powershell
cargo test --locked --test relay_flow
cargo test --locked
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
```

验证范围、限制与后续联调见 [开发记录](docs/development.md) 和 [契约待确认项](docs/contract-issues.md)。共享规范来自工作区的 `docs/design/cloud-service.md`、`docs/11-三端接口契约.md` 和 `contracts/README.md`。本工程使用 `contracts/v2` 快照独立构建。
