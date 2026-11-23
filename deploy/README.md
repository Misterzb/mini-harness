# MiniHarness 部署速查

面向面试官的在线演示：**双进程 + 口令闸门 + 真实百炼**。

- API：`127.0.0.1:8000`（**只绑本机，不对公网**）
- UI：`0.0.0.0:8501`（对外，口令在 UI 层校验）

## 一键部署

```bash
export MINI_HARNESS_API_KEY="网关key（swas.xxx）"
export DEMO_PASSWORD="给面试官的口令"
sudo -E bash deploy/setup_server.sh
# 强制重建数据库：sudo -E bash deploy/setup_server.sh --force-db
```

默认走 MaaS 中转网关 `https://maas-api.simple-server.cn/v1`，主模型 `qwen4-max`，备用 `kimi-k2.6`。换服务只需 `export MINI_HARNESS_BASE_URL=... MINI_HARNESS_MODEL=...`。旧变量 `DASHSCOPE_API_KEY` 仍兼容。

前提：Ubuntu 22.04/24.04，能访问 GitHub，root 或 sudo（`-E` 保留环境变量）。

## 必须开放的端口（阿里云轻量控制台【防火墙】）

| 端口 | 用途 | 公网放行？ |
|---|---|---|
| 8501 | Streamlit UI | ✅ 放行 |
| 8000 | FastAPI（仅供本机 UI 调用） | ❌ **不要放行** |
| 22 | SSH | 按你的习惯 |

## 采集服务器信息（部署前准备）

部署需要以下信息；如由 AI 代为远程部署，请提供：

1. **公网 IP**
2. **SSH 用户名**（root / ubuntu / 自定义）
3. **认证方式**：密码 或 私钥文件路径
4. **系统版本**：`cat /etc/os-release`（Ubuntu 22.04/24.04 已验证；其他发行版需改脚本）
5. **网关 API Key**（图中 `swas.` 开头的 token；若不便提供，脚本会走 mock 模式，演示可用但非真实模型）
6. **口令**：给面试官的访问口令

## 部署后自检

```bash
systemctl status miniharness-api miniharness-ui
curl -s http://127.0.0.1:8000/health          # 本机应返回 JSON
tail -f /var/log/miniharness/api.log /var/log/miniharness/ui.log
```

浏览器打开 `http://<公网IP>:8501` → 输入口令 → 依次点侧栏 6 个演示问题。

## 常见故障

| 现象 | 原因 / 处理 |
|---|---|
| 公网打不开 8501 | 阿里云控制台防火墙没放行 TCP 8501 |
| 页面能开但"请求失败" | API 服务没起：`systemctl restart miniharness-api`；或 `.env` 的 key 格式错 |
| 口令框反复出现 | 环境变量没传给 UI 服务：确认 `.env` 里 `MINI_HARNESS_DEMO_PASSWORD` 已写，restart UI |
| 刷新后聊天记录丢失 | 正常——聊天在浏览器 session，服务端 state 按 session_id 落盘，刷新会换 id |
| 退款批准后无反应 | 看 `api.log`；多为 ledger 写锁，重启 API 服务即可（演示数据可丢） |

## 安全边界（诚实声明）

- 口令是**共享字符串**，不是账号体系；无 HTTPS，明文传输——演示级，别放敏感数据。
- FastAPI 无鉴权但只绑本机，公网不可达；MCP Server 默认不随部署启动。
- 写操作（退款）走独立 ledger 库， approve 无身份认证——演示级设计，见 README §9。
