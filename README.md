# Cityline 自动购票系统

Cityline 自动购票脚本，支持 Cloudflare 处理、自动登录、按钮检测、票价从低到高自动选择和自动提交。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置活动信息

编辑 `enhanced_config.json`，修改 URL、票价和数量：

```json
{
  "target_event": {
    "url": "https://shows.cityline.com/sc/2026/your-event.html",
    "product_strategy": { "mode": "current_url" },
    "session_strategy": { "fallback": "first_available" },
    "sale_state_strategy": {
      "stop_when": ["售罄", "Sold Out", "取消", "Cancelled"],
      "refresh_when": ["繁忙", "Busy", "系統忙碌", "系统忙碌"]
    }
  },
  "ticket_preferences": {
    "quantity": 1
  },
  "purchase_settings": {
    "auto_purchase": true,
    "auto_submit_order": true
  },
  "browser_config": {
    "stealth_mode": true,
    "user_data_dir": "artifacts/chrome-profile",
    "accept_language": "zh-HK,zh-TW,zh-CN,en-US,en"
  },
  "cloudflare": {
    "safe_mode": true,
    "manual_wait_seconds": 300,
    "keep_browser_open_on_failure": true
  }
}
```

### 2.1 可选：环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

支持的变量：

| 变量 | 说明 |
|---|---|
| `CITYLINE_EMAIL` / `CITYLINE_PASSWORD` | 自动填入会员登录 |
| `NOTIFICATION_WEBHOOK_URL` | 通知 Webhook |
| `CHROME_BINARY_PATH` | Chrome 可执行文件路径 |
| `CHROME_USER_DATA_DIR` | 持久化 Chrome 用户目录（保留 Cookie/Cloudflare 验证状态）|
| `NON_INTERACTIVE` | 设为 `true` 运行结束后自动关闭浏览器 |

### 3. 运行

```bash
python3 enhanced_ticket_purchaser.py
```

## 购票流程

1. 访问活动页 → 若被重定向到登录页，等待手动登录
2. 登录后重新进入活动页 → 检测开售状态
3. 查找并点击"前 往 购 票"按钮
4. 进入 venue 页 → 点击"继续"/"登入"
5. 选票页 → 从最低价格依次选择可用票价，设置数量
6. 自动提交订单（`auto_submit_order: true`）

## 票价选择策略

脚本会从**最低价格开始**尝试选择：

- 扫描页面所有票价选项，提取价格信息
- 按价格从低到高排序
- 跳过标注"售罄/Sold Out"的选项
- 依尝试最低价的可点击选项
- 如果所有可用价位都不行，强制尝试第一个可点击选项

## Cloudflare 处理

当检测到 Cloudflare 验证时，脚本会：

- 进入安全浏览器模式（保留图片、扩展和后台网络）
- 等待用户手动完成验证
- 使用持久化 Chrome Profile（`artifacts/chrome-profile`）保留通过后的 Cookie
- 失败时保持浏览器打开，可手动继续后重新运行

## 配置说明

| 配置项 | 说明 |
|---|---|
| `target_event.url` | 活动页面 URL |
| `target_event.product_strategy.mode` | `current_url`（跳过商品扫描）或 `scan_and_rank`（扫描页面卡片） |
| `ticket_preferences.quantity` | 购票数量（1-10） |
| `purchase_settings.auto_purchase` | 是否自动选票 |
| `purchase_settings.auto_submit_order` | 选票后是否自动点击"確定"提交 |
| `browser_config.user_data_dir` | 持久化 Chrome Profile 路径 |
| `cloudflare.safe_mode` | Cloudflare 安全模式，保留资源加载 |
| `cloudflare.manual_wait_seconds` | 手动验证最长等待时间 |
| `cloudflare.keep_browser_open_on_failure` | 验证失败时保持浏览器打开 |
| `runtime.speed_mode` | `fast`（推荐）或普通模式 |
| `runtime.verbose_login_status` | 输出每轮登录检测得分 |

## 故障排查

| 问题 | 解决 |
|---|---|
| Cloudflare 验证失败 | 检查 `artifacts/*cloudflare*.json` 中的指纹信息，确认 `safe_mode` 已启用 |
| 找不到购票按钮 | 查看 `artifacts/` 下最新诊断快照中的 HTML |
| 登录后不继续 | 确认活动 URL 有效，脚本会在登录后重新访问目标页面 |

## 技术栈

- Selenium WebDriver
- undetected-chromedriver
- loguru

## 安全说明

本本仅用于合法个人购票，请遵守 Cityline 使用条款。

## 文件结构

```
cityline_ticket-main/
├── enhanced_ticket_purchaser.py   # 主本
├── enhanced_config.json           # 配置文件
├── .env.example                   # 环境变量模板
├── requirements.txt               # Python 依赖
├── artifacts/                     # 诊断快照（.gitignore）
│   └── chrome-profile/            # 持久化 Chrome Profile
└── README.md
```
