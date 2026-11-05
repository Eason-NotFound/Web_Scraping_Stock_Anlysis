# NewsLens：先从这里开始

**v0.1 检查结果：**64 项离线检查通过，包含 Streamlit 三页、规则创建、模式隔离与合成回放。回放和评价命令已运行。真实行情权限、AI 判断质量和 Telegram 实际发送仍需用你的密钥验证；冻结记录见 `MILESTONES.md`。

## 1．先运行不用密钥的演示

需要 **Python 3.11 或更新版本**。解压后，在有 `app.py` 的文件夹打开终端：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m newslens.demo --reset
python -m streamlit run app.py
```

打开终端显示的地址。左侧选择 **Synthetic replay**，点击 **Advance replay →** 推进场景，再依次查看行情、新闻和提醒页面。

**演示价格与新闻是人工编写的合成样例；不是实时行情、真实历史数据，也不是实际 AI 输出。**它用于检查程序流程，不能当作投资表现或模型准确率。

Windows 的虚拟环境激活命令改为 `.venv\Scripts\Activate.ps1`。

## 2．检查程序

```bash
python -m unittest discover -s tests -v
```

这些是离线软件测试，不代表已经连接真实接口，也不代表已经测得 AI 准确率。

## 3．接入真实数据

先复制配置：

```bash
cp .env.example .env
```

用文本编辑器填写：

- `ALPACA_API_KEY`、`ALPACA_SECRET_KEY`：行情与新闻。
- `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`：实际 AI 分析。
- `NEWSLENS_ADMIN_TOKEN`：在实时页面修改提醒规则所需的管理员口令。

`.env` **不会被程序自动读取**。在 macOS/Linux 终端执行：

```bash
set -a
source .env
set +a
python -m newslens.worker --once
```

查看日志、数据时间、接口权限和延迟。确认后启动持续监控：

```bash
python -m newslens.worker
```

另开一个终端，激活虚拟环境、加载同一份 `.env`，再运行 `python -m streamlit run app.py`。

**目前没有你的密钥，所以真实接口权限、实时延迟和 AI 判断质量仍需实测。**缺少配置时，程序会显示错误或无数据，不会把合成演示冒充真实结果。

## 4．开启提醒

在 **Alerts & Validation** 设置股票、上穿／下穿价位及冷却时间。实时模式下先输入管理员口令；综合提醒还可要求近期同方向的证据充分新闻。

Telegram **默认关闭**。需要发送时，在 `.env` 中填好机器人与聊天 ID，并主动设置：

```text
NEWSLENS_ENABLE_TELEGRAM=1
```

更改后重启后台。不要把密钥写入代码或提交到 Git。

## 5．上线时记住这一点

最简单的稳定部署是：**一台持续运行的主机，同时运行 worker 和 Streamlit，二者共用持久化 SQLite。**关闭网页后，只要 worker 仍在运行，就能继续监控。

只把页面上传到 `streamlit.app`，不会自动拥有全天后台；该平台本地文件也不保证永久保存。若页面与 worker 部署在不同机器上，需要改为共享持久化存储，不能各用一份本地 SQLite。

详细运行方式、评价方法与限制见 `README.md`。每个里程碑的冻结证据见 `MILESTONES.md`。
