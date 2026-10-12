# Alpaca Quant Workbench

一个从本机浏览器使用的个人量化研究工作台。历史行情和回测使用 Yahoo Finance 的 `yfinance`；Alpaca 只用于模拟账户、模拟订单和运行器下单。策略、策略版本和回测记录写入本地 SQLite。

## 当前版本包含什么

- **策略库**：内置 SMA、RSI 和 EMA 示例策略；可以创建、编辑、复制和归档策略。每次保存编辑都会新增版本，归档不会清除旧版本。
- **策略规则编辑器**：用受限表达式描述多头进出场规则，不执行任意 Python 代码。可用变量为 `open`、`high`、`low`、`close`、`volume`、`sma_N`、`ema_N`、`rsi_N`；`N` 为 2–500。支持比较、`and`、`or`、`not`、基础四则运算、`cross_above(a, b)` 和 `cross_below(a, b)`。
- **回测比较**：选择 AAPL 等美股代码、日期、日线/小时/15 分钟周期、起始资金、手续费、滑点和最多 8 个策略。行情来自 Yahoo Finance 的 `yfinance`；多策略共用同一份行情，资金曲线图同时显示各策略和按起始资金归一化的股票持有基准。
- **模拟账户**：连接后查看 paper 账户余额和订单；可以测试密钥连接，并手动提交带确认步骤的模拟市场订单。应用固定使用 `https://paper-api.alpaca.markets`，没有实盘交易端点。
- **自动模拟策略**：在模拟交易页启动/停止一个策略版本，服务每分钟轮询 `yfinance` 行情，对新完成的 K 线运行规则，并且仅在产生信号时向 Alpaca 提交 paper 市价单。启动时先记录最新 K 线，不追溯启动前信号。
- **持久化**：本机数据库位于 `data/workbench.sqlite3`。每次回测会保存参数、数据源、策略 ID、策略版本和进出场规则快照。

## 界面与数据流

1. 浏览器向本地 Python 服务请求策略和回测设置；浏览器不读取或保存 Alpaca 密钥。
2. Python 服务通过 `yfinance` 取得 Yahoo Finance 历史行情；该流程不需要 Alpaca 密钥。
3. 同一份 OHLCV K 线被送入选中的每个策略版本；规则解析器只接受白名单表达式。
4. 回测结果、参数快照、策略版本和股票持有基准曲线写入 SQLite，再由前端绘制比较图表。
5. 模拟账户查询、模拟订单和运行器下单只调用 Alpaca paper API。下单和启动运行器要求本机显式设置 `ALPACA_PAPER_TRADING_ENABLED=true`；模拟交易页的密钥测试按钮只发送只读账户查询。

## 启动

需要 Python 3.10 或更新版本，并安装 `requirements.txt` 中的依赖。首次使用时，在项目目录运行：

```powershell
python --version
python -m pip install -r requirements.txt
```

在项目目录打开 PowerShell 并启动服务：

```powershell
cd D:\OpenAI-Codex
py -3.14 -u app.py
```

服务启动后，在浏览器打开 <http://127.0.0.1:8765>。关闭服务时，在 PowerShell 窗口按 `Ctrl+C`。首次启动会创建 `data/workbench.sqlite3` 和三条示例策略。

### 历史行情

回测固定使用 Yahoo Finance 的 `yfinance` 下载行情，不会请求 Alpaca 行情接口。`yfinance` 返回的 OHLC 使用自动复权数据；Yahoo Finance 对日内历史范围有限制：15 分钟周期最多查询最近约 60 天，1 小时周期最多约 730 天。日期范围以用户选择的结束日期为含当日处理。

### 启用 Alpaca 模拟账户

在 `.env` 中配置 Alpaca paper account 的密钥，并将 `ALPACA_PAPER_TRADING_ENABLED` 改为 `true`，随后重启服务。模拟交易页的“测试密钥连接”按钮会向固定的 paper API 读取账户信息，不会下单。页面也会显示账户和最近订单；确认后可以提交买卖市场订单，或启动一个用 `yfinance` 行情判断、向 Alpaca paper API 下单的模拟策略。

## 回测假设与边界

- 当前只做多，每次每个策略最多持有一个仓位；买入使用可用现金，卖出清仓。
- 当前 K 线收盘后判断规则，下一根 K 线开盘执行，应用配置的滑点和手续费基点。回测结果保存已完成交易的摘要，未平仓仓位按最后一根收盘价计入期末资产。
- 自动模拟运行器在 K 线完成后评估策略，并提交固定股数的 paper 市价单。它仅在本机服务运行期间工作；服务重启会将旧运行标记为“中断”，不会自动恢复。
- 自动运行器为每个任务维护自己的持仓标记，不会读取或同步 Alpaca 账户中手动建立的持仓；同一账户中的多个运行器也彼此独立。
- 回测最多读取 10,000 根 K 线；Yahoo Finance 行情可能延迟、缺失或受服务限制，适合个人研究验证，不代表实时成交行情或投资建议。
- Sharpe 使用零无风险利率；年化周期按日线 252、小时线 1,638、15 分钟线 6,552 根/年估算。分红、税务、融资成本、订单排队和精确成交模拟尚未建模。

## 文件结构

```text
app.py                 本地 HTTP API、Alpaca 连接、策略规则解析、回测引擎
requirements.txt       yfinance 运行依赖
web/index.html         工作台页面结构
web/styles.css         本地界面样式
web/app.js             页面交互、图表和 Alpaca paper 操作
data/workbench.sqlite3 本机自动创建的策略和回测数据库
.env                   本机 Alpaca paper 密钥（已忽略）
```
