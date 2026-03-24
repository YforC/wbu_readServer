# WBU Read Server

这是一个基于 Flask 和 Playwright 的本地管理面板，用来维护多个账号的阅读任务。

程序做两件事：

1. 通过 Playwright 打开 WebVPN / CAS 登录流程，抓取阅读平台会话信息。
2. 通过 HTTP 心跳接口持续上报阅读状态，并在页面中管理账号、短信验证码和重新抓取流程。

## 功能概览

- 单文件服务入口：`wbu_server.py`
- Web 管理面板：默认监听 `http://127.0.0.1:5000`
- 账号存储：运行后会在项目目录生成 `accounts.json`
- 支持操作：
  - 添加账号
  - 启动 / 停止任务
  - 提交短信验证码
  - 修改 `book_id`
  - 手动触发重新抓取

## 运行环境

- Python 3.10 及以上
- Windows 环境下建议先安装最新版 Chrome / Edge
- 首次运行 Playwright 前需要安装 Chromium 浏览器

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

如果你使用的是 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## 启动

```bash
python wbu_server.py
```

启动后访问：

```text
http://127.0.0.1:5000
```

服务启动时会额外拉起一个后台管理线程，用于检查已激活账号并创建对应的工作线程。

## 项目结构

```text
.
├─ wbu_server.py        # Flask 服务、Playwright 抓取逻辑、心跳逻辑、内嵌 HTML 面板
├─ requirements.txt     # Python 依赖
├─ accounts.json        # 运行后生成的账号数据文件
└─ README.md            # 项目说明
```

## 主要路由

- `GET /`：管理面板首页
- `POST /add`：添加账号
- `POST /trigger`：触发短信发送
- `POST /submit`：提交短信验证码
- `POST /del`：删除账号
- `POST /update_book`：更新 `book_id`
- `POST /stop`：停止账号任务
- `POST /start`：启动账号任务
- `POST /recapture`：手动重新抓取 token / reader_id

## 数据说明

`accounts.json` 中会保存每个账号的运行状态，包括但不限于：

- `password`
- `book_id`
- `status`
- `active`
- `action_required`
- `sms_code`
- `sms_code_time`
- `total_seconds`
- `token_preview`
- `twfid_preview`
- `reader_id`

## 注意事项

- 该项目当前将页面模板直接内嵌在 `wbu_server.py` 中，后续如果继续扩展，建议拆分模板与静态资源。
- 运行过程中会创建类似 `browser_data_<username>` 的浏览器持久化目录。
- `accounts.json` 可能包含敏感信息，不建议直接提交到公共仓库。

## 开发建议

- 安装依赖后可先执行 `python -m playwright install chromium`
- 如需修改端口，可直接调整 `wbu_server.py` 末尾的 `app.run(host='0.0.0.0', port=5000)`
- 如果后续需要部署，建议补充：
  - 更严格的配置管理
  - 日志落盘
  - 数据文件忽略规则
  - 生产环境 WSGI 启动方式
