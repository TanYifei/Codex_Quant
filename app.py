"""Local strategy workbench using yfinance for prices and Alpaca paper for trading."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "workbench.sqlite3"
HOST = "127.0.0.1"
PORT = int(os.environ.get("WORKBENCH_PORT", "8765"))
PAPER_API = "https://paper-api.alpaca.markets/v2"
TIMEFRAME_PERIODS = {"1Day": 252, "1Hour": 1638, "15Min": 6552}
INDICATOR_RE = re.compile(r"\b(sma|ema|rsi)_(\d+)\b")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
RUNNER_POLL_SECONDS = 60
RUNNER_EVENTS: dict[str, threading.Event] = {}
RUNNER_EVENTS_LOCK = threading.Lock()


class APIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_local_env() -> None:
    """Read simple KEY=value entries from .env without printing or overriding process env."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and not os.environ.get(key, "").strip():
            os.environ[key] = value


def credentials() -> tuple[str, str]:
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY", "")
    return key.strip(), secret.strip()


def paper_enabled() -> bool:
    return os.environ.get("ALPACA_PAPER_TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def db_connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def validate_rule(expression: str) -> str:
    expression = str(expression or "").strip()
    if not expression or len(expression) > 300:
        raise APIError("每条规则需填写，且不能超过 300 个字符。")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise APIError(f"规则语法错误：{exc.msg}") from exc
    allowed = (
        ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
        ast.UAdd, ast.USub, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.Mod, ast.Compare, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt,
        ast.LtE, ast.Call, ast.Name, ast.Load, ast.Constant,
    )
    allowed_names = {"open", "high", "low", "close", "volume", "bar_index", "cross_above", "cross_below"}
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise APIError("规则只支持比较、and/or/not、基础四则运算和 cross_above/cross_below。")
        if isinstance(node, ast.Name):
            if node.id not in allowed_names and not INDICATOR_RE.fullmatch(node.id):
                raise APIError(f"不支持的变量：{node.id}")
            if INDICATOR_RE.fullmatch(node.id):
                period = int(node.id.rsplit("_", 1)[1])
                if not 2 <= period <= 500:
                    raise APIError("指标周期必须在 2 到 500 之间。")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise APIError("规则中的常量必须是数字。")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in {"cross_above", "cross_below"} or len(node.args) != 2 or node.keywords:
                raise APIError("函数格式示例：cross_above(close, sma_20)。")
    return expression


def seed_strategies() -> None:
    defaults = [
        {
            "name": "SMA 趋势交叉",
            "description": "收盘价上穿 20 日均线买入，下穿时卖出。可以直接改写下面的进出场表达式。",
            "entry_rule": "cross_above(close, sma_20)",
            "exit_rule": "cross_below(close, sma_20)",
        },
        {
            "name": "RSI 均值回归",
            "description": "RSI 低于 30 时买入，高于 55 时卖出。",
            "entry_rule": "rsi_14 < 30",
            "exit_rule": "rsi_14 > 55",
        },
        {
            "name": "EMA 趋势过滤",
            "description": "短期 EMA 在长期 EMA 上方且收盘价高于长期 EMA 时持仓。",
            "entry_rule": "ema_12 > ema_26 and close > ema_26",
            "exit_rule": "ema_12 < ema_26",
        },
    ]
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
        if count:
            return
        for definition in defaults:
            sid = str(uuid.uuid4())
            stamp = utc_now()
            conn.execute(
                "INSERT INTO strategies(id,name,active_version,created_at,updated_at) VALUES(?,?,?,?,?)",
                (sid, definition["name"], 1, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO strategy_versions(id,strategy_id,version,definition,created_at) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), sid, 1, json.dumps(definition, ensure_ascii=False), stamp),
            )


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                active_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            );
            CREATE TABLE IF NOT EXISTS strategy_versions (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(strategy_id, version)
            );
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                settings TEXT NOT NULL,
                results TEXT NOT NULL,
                benchmark TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS paper_runners (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                qty REAL NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                strategy_snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                stopped_at TEXT,
                last_bar TEXT,
                position_open INTEGER NOT NULL DEFAULT 0,
                last_signal TEXT NOT NULL DEFAULT 'starting',
                last_order_id TEXT,
                last_error TEXT
            );
            """
        )
        strategy_columns = {row[1] for row in conn.execute("PRAGMA table_info(strategies)").fetchall()}
        if "archived_at" not in strategy_columns:
            conn.execute("ALTER TABLE strategies ADD COLUMN archived_at TEXT")
        backtest_columns = {row[1] for row in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()}
        if "benchmark" not in backtest_columns:
            conn.execute("ALTER TABLE backtest_runs ADD COLUMN benchmark TEXT NOT NULL DEFAULT '{}' ")
        conn.execute("UPDATE paper_runners SET status='interrupted', stopped_at=? WHERE status='running'", (utc_now(),))
    seed_strategies()


def version_definition(conn: sqlite3.Connection, strategy_id: str, version: int | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT active_version FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        raise APIError("找不到该策略。", 404)
    target_version = version if version is not None else row["active_version"]
    found = conn.execute(
        "SELECT definition,version,created_at FROM strategy_versions WHERE strategy_id=? AND version=?",
        (strategy_id, target_version),
    ).fetchone()
    if not found:
        raise APIError("找不到该策略版本。", 404)
    return {**json.loads(found["definition"]), "version": found["version"], "version_created_at": found["created_at"]}


def strategies_payload() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM strategies WHERE archived_at IS NULL ORDER BY updated_at DESC").fetchall()
        return [
            {
                "id": row["id"], "name": row["name"], "active_version": row["active_version"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "definition": version_definition(conn, row["id"]),
            }
            for row in rows
        ]


def clean_definition(payload: dict[str, Any]) -> dict[str, str]:
    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 80:
        raise APIError("策略名称需填写，且不能超过 80 个字符。")
    return {
        "name": name,
        "description": str(payload.get("description", "")).strip()[:500],
        "entry_rule": validate_rule(payload.get("entry_rule", "")),
        "exit_rule": validate_rule(payload.get("exit_rule", "")),
    }


def extract_indicators(definitions: list[dict[str, Any]]) -> set[tuple[str, int]]:
    indicators: set[tuple[str, int]] = set()
    for definition in definitions:
        for rule_name in ("entry_rule", "exit_rule"):
            for kind, period in INDICATOR_RE.findall(definition[rule_name]):
                indicators.add((kind, int(period)))
    return indicators


def add_indicators(bars: list[dict[str, Any]], indicators: set[tuple[str, int]]) -> list[dict[str, Any]]:
    output = [dict(bar) for bar in bars]
    closes = [float(bar["close"]) for bar in bars]
    for kind, period in indicators:
        values: list[float | None] = [None] * len(bars)
        if kind == "sma":
            running = 0.0
            for index, close in enumerate(closes):
                running += close
                if index >= period:
                    running -= closes[index - period]
                if index >= period - 1:
                    values[index] = running / period
        elif kind == "ema":
            alpha = 2.0 / (period + 1)
            ema = 0.0
            for index, close in enumerate(closes):
                if index == period - 1:
                    ema = sum(closes[:period]) / period
                    values[index] = ema
                elif index >= period:
                    ema = (close - ema) * alpha + ema
                    values[index] = ema
        else:  # simple rolling RSI
            for index in range(period, len(closes)):
                gains = 0.0
                losses = 0.0
                for offset in range(index - period + 1, index + 1):
                    delta = closes[offset] - closes[offset - 1]
                    if delta > 0:
                        gains += delta
                    else:
                        losses -= delta
                avg_gain, avg_loss = gains / period, losses / period
                values[index] = 100.0 if avg_loss == 0 and avg_gain > 0 else (50.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
        key = f"{kind}_{period}"
        for index, value in enumerate(values):
            output[index][key] = value
    return output


def safe_eval(node: ast.AST, current: dict[str, Any], previous: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return safe_eval(node.body, current, previous)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return current.get(node.id)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not bool(safe_eval(value, current, previous)):
                    return False
            return True
        for value in node.values:
            if bool(safe_eval(value, current, previous)):
                return True
        return False
    if isinstance(node, ast.UnaryOp):
        value = safe_eval(node.operand, current, previous)
        if value is None:
            return False
        if isinstance(node.op, ast.Not):
            return not bool(value)
        return +value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left, right = safe_eval(node.left, current, previous), safe_eval(node.right, current, previous)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if isinstance(node.op, ast.Div): return left / right
            if isinstance(node.op, ast.Mod): return left % right
        except (ZeroDivisionError, TypeError):
            return None
    if isinstance(node, ast.Compare):
        left = safe_eval(node.left, current, previous)
        for operator, comparator in zip(node.ops, node.comparators):
            right = safe_eval(comparator, current, previous)
            if left is None or right is None:
                return False
            try:
                ok = (
                    left == right if isinstance(operator, ast.Eq) else
                    left != right if isinstance(operator, ast.NotEq) else
                    left > right if isinstance(operator, ast.Gt) else
                    left >= right if isinstance(operator, ast.GtE) else
                    left < right if isinstance(operator, ast.Lt) else left <= right
                )
            except TypeError:
                return False
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        left_now = safe_eval(node.args[0], current, previous)
        right_now = safe_eval(node.args[1], current, previous)
        left_before = safe_eval(node.args[0], previous, previous)
        right_before = safe_eval(node.args[1], previous, previous)
        if None in (left_now, right_now, left_before, right_before):
            return False
        if node.func.id == "cross_above":
            return left_before <= right_before and left_now > right_now
        return left_before >= right_before and left_now < right_now
    return None


def rule_matches(expression: str, bars: list[dict[str, Any]], index: int) -> bool:
    try:
        tree = ast.parse(expression, mode="eval")
        current = bars[index]
        previous = bars[max(0, index - 1)]
        result = safe_eval(tree, current, previous)
        return bool(result)
    except (ArithmeticError, TypeError, ValueError):
        return False


def run_backtest(
    definition: dict[str, Any], bars: list[dict[str, Any]], capital: float,
    fee_bps: float, slippage_bps: float, timeframe: str,
) -> dict[str, Any]:
    expanded = add_indicators(bars, extract_indicators([definition]))
    fee_rate, slip_rate = fee_bps / 10000.0, slippage_bps / 10000.0
    cash, quantity = capital, 0.0
    entry_cost = 0.0
    entry_date = ""
    closed_trades: list[dict[str, Any]] = []
    raw_equity: list[tuple[str, float]] = [(bars[0]["timestamp"], capital)]
    for index in range(1, len(expanded)):
        previous_index = index - 1
        bar = expanded[index]
        if quantity > 0 and rule_matches(definition["exit_rule"], expanded, previous_index):
            execution_price = float(bar["open"]) * (1.0 - slip_rate)
            proceeds = quantity * execution_price * (1.0 - fee_rate)
            pnl = proceeds - entry_cost
            closed_trades.append({"entry": entry_date, "exit": bar["timestamp"], "pnl": round(pnl, 2), "return_pct": round(pnl / entry_cost * 100, 2) if entry_cost else 0})
            cash += proceeds
            quantity = 0.0
            entry_cost = 0.0
        elif quantity == 0 and rule_matches(definition["entry_rule"], expanded, previous_index):
            execution_price = float(bar["open"]) * (1.0 + slip_rate)
            quantity = cash / (execution_price * (1.0 + fee_rate))
            entry_cost = quantity * execution_price * (1.0 + fee_rate)
            cash -= entry_cost
            entry_date = bar["timestamp"]
        raw_equity.append((bar["timestamp"], cash + quantity * float(bar["close"])))

    equity_values = [value for _, value in raw_equity]
    peak = capital
    drawdowns: list[float] = []
    for value in equity_values:
        peak = max(peak, value)
        drawdowns.append((value / peak - 1.0) if peak else 0.0)
    final_equity = equity_values[-1]
    total_return = final_equity / capital - 1.0
    periods = max(len(equity_values) - 1, 1)
    annual_factor = TIMEFRAME_PERIODS[timeframe]
    annualized = (final_equity / capital) ** (annual_factor / periods) - 1.0 if final_equity > 0 else -1.0
    returns = [(equity_values[i] / equity_values[i - 1] - 1.0) for i in range(1, len(equity_values)) if equity_values[i - 1] > 0]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    sharpe = mean_return / math.sqrt(variance) * math.sqrt(annual_factor) if variance > 0 else 0.0
    step = max(1, math.ceil(len(raw_equity) / 750))
    curve = [
        {"timestamp": raw_equity[i][0], "equity": round(raw_equity[i][1], 2)}
        for i in range(0, len(raw_equity), step)
    ]
    if (len(raw_equity) - 1) % step:
        i = len(raw_equity) - 1
        curve.append({"timestamp": raw_equity[i][0], "equity": round(raw_equity[i][1], 2)})
    winning = sum(1 for trade in closed_trades if trade["pnl"] > 0)
    return {
        "strategy_id": definition["strategy_id"], "name": definition["name"], "version": definition["version"],
        "strategy_definition": {
            "description": definition.get("description", ""),
            "entry_rule": definition["entry_rule"], "exit_rule": definition["exit_rule"],
        },
        "metrics": {
            "total_return_pct": round(total_return * 100, 2),
            "annualized_return_pct": round(annualized * 100, 2),
            "max_drawdown_pct": round(min(drawdowns, default=0) * 100, 2),
            "sharpe": round(sharpe, 2), "closed_trades": len(closed_trades),
            "win_rate_pct": round(winning / len(closed_trades) * 100, 1) if closed_trades else None,
            "ending_equity": round(final_equity, 2),
        },
        "curve": curve,
        "trades": closed_trades[-100:],
    }


def parse_bar_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.combine(date.fromisoformat(value), time.min)


def yfinance_bars(symbol: str, start: date, end: date, timeframe: str) -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise APIError("缺少 yfinance 依赖。请运行 `python -m pip install -r requirements.txt` 并重启服务。", 503) from exc

    max_history_days = {"15Min": 60, "1Hour": 730}.get(timeframe)
    if max_history_days and start < date.today() - timedelta(days=max_history_days):
        range_label = "60 天" if timeframe == "15Min" else "730 天"
        raise APIError(f"yfinance 的{('15 分钟' if timeframe == '15Min' else '1 小时')}行情只能查询最近约 {range_label}，请缩短时间范围。", 422)

    interval = {"1Day": "1d", "1Hour": "1h", "15Min": "15m"}[timeframe]
    try:
        history = yf.Ticker(symbol).history(
            start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
            interval=interval, auto_adjust=True, actions=False, timeout=40,
        )
    except Exception as exc:
        detail = str(exc).strip()[:300]
        raise APIError(f"yfinance 获取 {symbol} 行情失败：{detail or type(exc).__name__}", 502) from exc

    if history.empty:
        raise APIError("Yahoo Finance 没有返回行情数据。请检查代码、日期范围，或确认该周期仍在可查询期限内。", 422)

    bars: list[dict[str, Any]] = []
    for stamp, row in history.iterrows():
        try:
            values = {
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": float(row["Close"]),
            }
            volume = float(row.get("Volume", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in values.values()) or values["close"] <= 0:
            continue
        if not math.isfinite(volume):
            volume = 0.0
        bars.append({"timestamp": stamp.isoformat(), **values, "volume": volume})
        if len(bars) > 10000:
            raise APIError("单次回测最多使用 10,000 根 K 线，请缩短时间范围或选择更大的周期。", 422)
    if len(bars) < 2:
        raise APIError("Yahoo Finance 返回的有效 K 线不足两根。请检查股票代码和日期范围。", 422)
    return bars


def validate_backtest(payload: dict[str, Any]) -> tuple[str, date, date, str, float, float, float, list[str]]:
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise APIError("请输入有效的美股代码，例如 AAPL。")
    try:
        start = date.fromisoformat(str(payload.get("start", "")))
        end = date.fromisoformat(str(payload.get("end", "")))
    except ValueError as exc:
        raise APIError("请填写有效的起止日期。") from exc
    if start >= end:
        raise APIError("开始日期必须早于结束日期。")
    if end > date.today():
        raise APIError("结束日期不能晚于今天。")
    timeframe = str(payload.get("timeframe", "1Day"))
    if timeframe not in TIMEFRAME_PERIODS:
        raise APIError("周期需为 1Day、1Hour 或 15Min。")
    try:
        capital = float(payload.get("capital", 100000))
        fee_bps = float(payload.get("fee_bps", 0))
        slippage_bps = float(payload.get("slippage_bps", 5))
    except (TypeError, ValueError) as exc:
        raise APIError("资金、手续费和滑点必须为数字。") from exc
    if not 100 <= capital <= 100000000 or not math.isfinite(capital):
        raise APIError("初始资金需在 100 到 100,000,000 之间。")
    if not 0 <= fee_bps <= 1000 or not 0 <= slippage_bps <= 1000:
        raise APIError("手续费和滑点需在 0 到 1,000 基点之间。")
    strategy_ids = payload.get("strategy_ids", [])
    if not isinstance(strategy_ids, list) or not 1 <= len(strategy_ids) <= 8:
        raise APIError("请选择 1 到 8 个策略进行比较。")
    return symbol, start, end, timeframe, capital, fee_bps, slippage_bps, strategy_ids


def execute_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    symbol, start, end, timeframe, capital, fee_bps, slip_bps, strategy_ids = validate_backtest(payload)
    bars = yfinance_bars(symbol, start, end, timeframe)
    definitions = []
    with db_connect() as conn:
        for strategy_id in dict.fromkeys(strategy_ids):
            definition = version_definition(conn, str(strategy_id))
            definition["strategy_id"] = str(strategy_id)
            definitions.append(definition)
    if not definitions:
        raise APIError("没有找到可运行的策略。", 404)
    results = [run_backtest(definition, bars, capital, fee_bps, slip_bps, timeframe) for definition in definitions]
    first_close = float(bars[0]["close"])
    if first_close <= 0:
        raise APIError("首根 K 线收盘价无效，无法计算股票持有基准。")
    benchmark_step = max(1, math.ceil(len(bars) / 750))
    benchmark_curve = [
        {
            "timestamp": bars[index]["timestamp"],
            "equity": round(capital * float(bars[index]["close"]) / first_close, 2),
        }
        for index in range(0, len(bars), benchmark_step)
    ]
    if (len(bars) - 1) % benchmark_step:
        last_bar = bars[-1]
        benchmark_curve.append({
            "timestamp": last_bar["timestamp"],
            "equity": round(capital * float(last_bar["close"]) / first_close, 2),
        })
    benchmark = {"name": f"{symbol} 持有基准", "curve": benchmark_curve}
    settings = {
        "symbol": symbol, "start": start.isoformat(), "end": end.isoformat(), "timeframe": timeframe,
        "capital": capital, "fee_bps": fee_bps, "slippage_bps": slip_bps, "source": "yfinance",
        "bar_count": len(bars),
    }
    run_id, created_at = str(uuid.uuid4()), utc_now()
    run = {"id": run_id, "created_at": created_at, "settings": settings, "results": results, "benchmark": benchmark}
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO backtest_runs(id,created_at,symbol,settings,results,benchmark) VALUES(?,?,?,?,?,?)",
            (run_id, created_at, symbol, json.dumps(settings), json.dumps(results), json.dumps(benchmark)),
        )
    return run


def alpaca_request(
    path: str, method: str = "GET", payload: dict[str, Any] | None = None,
    require_paper_enabled: bool = True,
) -> Any:
    key, secret = credentials()
    if not key or not secret:
        raise APIError("未配置 Alpaca API 密钥。请先在本机 .env 文件中填写密钥。", 400)
    if require_paper_enabled and not paper_enabled():
        raise APIError("模拟交易尚未启用。确认使用模拟账户后，在 .env 中设置 ALPACA_PAPER_TRADING_ENABLED=true 并重启服务。", 403)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        PAPER_API + path, data=body, method=method,
        headers={
            "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json", "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise APIError(f"Alpaca 模拟账户请求失败（HTTP {exc.code}）：{detail}", 502) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise APIError(f"无法连接 Alpaca 模拟账户：{exc}", 502) from exc


def safe_account() -> dict[str, Any]:
    account = alpaca_request("/account")
    keys = ("status", "currency", "cash", "equity", "buying_power", "portfolio_value", "pattern_day_trader")
    return {key: account.get(key) for key in keys}


def fetch_runner_bars(symbol: str, timeframe: str) -> list[dict[str, Any]]:
    today = date.today()
    if timeframe == "1Day":
        end = today - timedelta(days=1)
        start = end - timedelta(days=1100)
        return yfinance_bars(symbol, start, end, timeframe)
    start = today - timedelta(days=59 if timeframe == "15Min" else 240)
    bars = yfinance_bars(symbol, start, today, timeframe)
    step = timedelta(minutes=15) if timeframe == "15Min" else timedelta(hours=1)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=2)
    completed = []
    for bar in bars:
        stamp = parse_bar_time(bar["timestamp"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp + step <= cutoff:
            completed.append(bar)
    return completed


def runner_wait(event: threading.Event) -> None:
    event.wait(RUNNER_POLL_SECONDS)


def paper_runner_loop(runner_id: str, event: threading.Event) -> None:
    """Poll completed yfinance bars and submit long-only Alpaca paper orders on new signals."""
    try:
        while not event.is_set():
            with db_connect() as conn:
                row = conn.execute("SELECT * FROM paper_runners WHERE id=?", (runner_id,)).fetchone()
            if not row or row["status"] != "running":
                break
            try:
                bars = fetch_runner_bars(row["symbol"], row["timeframe"])
                if len(bars) < 2:
                    raise APIError("行情不足两根已完成 K 线，稍后重试。", 422)
                latest = bars[-1]
                if row["last_bar"] is None:
                    with db_connect() as conn:
                        conn.execute("UPDATE paper_runners SET last_bar=?,last_signal='等待下一根已完成 K 线',last_error=NULL WHERE id=?", (latest["timestamp"], runner_id))
                    runner_wait(event)
                    continue
                if latest["timestamp"] == row["last_bar"]:
                    runner_wait(event)
                    continue
                definition = json.loads(row["strategy_snapshot"])
                expanded = add_indicators(bars, extract_indicators([definition]))
                index = len(expanded) - 1
                is_open = bool(row["position_open"])
                exit_signal = rule_matches(definition["exit_rule"], expanded, index)
                entry_signal = rule_matches(definition["entry_rule"], expanded, index)
                action = "sell" if is_open and exit_signal else ("buy" if not is_open and entry_signal else "")
                if not action:
                    with db_connect() as conn:
                        conn.execute("UPDATE paper_runners SET last_bar=?,last_signal='无交易信号',last_error=NULL WHERE id=?", (latest["timestamp"], runner_id))
                    runner_wait(event)
                    continue
                bar_key = hashlib.sha1(latest["timestamp"].encode("utf-8")).hexdigest()[:12]
                client_order_id = f"wb-{runner_id[:8]}-{bar_key}"
                order_request = {
                    "symbol": row["symbol"], "qty": f"{row['qty']:.6f}".rstrip("0").rstrip("."),
                    "side": action, "type": "market", "time_in_force": "day",
                    "client_order_id": client_order_id,
                }
                try:
                    order = alpaca_request("/orders", "POST", order_request)
                except APIError as submit_error:
                    # If the POST response was lost, reconcile the stable client ID before retrying.
                    lookup = "/orders:by_client_order_id?" + urllib.parse.urlencode({"client_order_id": client_order_id})
                    try:
                        order = alpaca_request(lookup)
                    except APIError:
                        raise submit_error
                label = "已提交模拟买单" if action == "buy" else "已提交模拟卖单"
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE paper_runners SET last_bar=?,position_open=?,last_signal=?,last_order_id=?,last_error=NULL WHERE id=?",
                        (latest["timestamp"], int(action == "buy"), f"{label} · {latest['timestamp']}", order.get("id"), runner_id),
                    )
            except APIError as exc:
                with db_connect() as conn:
                    conn.execute("UPDATE paper_runners SET last_error=? WHERE id=?", (str(exc)[:500], runner_id))
            except Exception as exc:
                with db_connect() as conn:
                    conn.execute("UPDATE paper_runners SET last_error=? WHERE id=?", (str(exc)[:500], runner_id))
            runner_wait(event)
    finally:
        with RUNNER_EVENTS_LOCK:
            RUNNER_EVENTS.pop(runner_id, None)


def runner_payloads() -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM paper_runners ORDER BY created_at DESC LIMIT 20").fetchall()
    return [
        {
            "id": row["id"], "status": row["status"], "symbol": row["symbol"],
            "timeframe": row["timeframe"], "qty": row["qty"], "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"], "strategy_name": json.loads(row["strategy_snapshot"])["name"],
            "created_at": row["created_at"], "stopped_at": row["stopped_at"], "last_bar": row["last_bar"],
            "position_open": bool(row["position_open"]), "last_signal": row["last_signal"],
            "last_order_id": row["last_order_id"], "last_error": row["last_error"],
        }
        for row in rows
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "AlpacaWorkbench/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request paths are useful while developing; query strings are omitted.
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise APIError("无效的请求长度。") from exc
        if length < 0 or length > 1_000_000:
            raise APIError("请求内容过大。", 413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError("请求必须是有效的 JSON。") from exc
        if not isinstance(payload, dict):
            raise APIError("请求内容格式无效。")
        return payload

    def api(self, method: str, path: str) -> None:
        if method == "GET" and path == "/api/status":
            key, secret = credentials()
            self.send_json({
                "alpaca_keys_configured": bool(key and secret),
                "paper_trading_enabled": paper_enabled(), "trading_endpoint": "paper-only",
            })
            return
        if method == "GET" and path == "/api/strategies":
            self.send_json(strategies_payload())
            return
        if method == "GET" and path == "/api/runs":
            with db_connect() as conn:
                rows = conn.execute("SELECT id,created_at,symbol,settings FROM backtest_runs ORDER BY created_at DESC LIMIT 30").fetchall()
            self.send_json([{"id": row["id"], "created_at": row["created_at"], "symbol": row["symbol"], "settings": json.loads(row["settings"])} for row in rows])
            return
        match = re.fullmatch(r"/api/runs/([0-9a-fA-F-]{36})", path)
        if method == "GET" and match:
            with db_connect() as conn:
                row = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (match.group(1),)).fetchone()
            if not row:
                raise APIError("找不到这次回测。", 404)
            self.send_json({"id": row["id"], "created_at": row["created_at"], "settings": json.loads(row["settings"]), "results": json.loads(row["results"]), "benchmark": json.loads(row["benchmark"])})
            return
        match = re.fullmatch(r"/api/strategies/([0-9a-fA-F-]{36})", path)
        if method == "PUT" and match:
            definition = clean_definition(self.read_json())
            sid, stamp = match.group(1), utc_now()
            with db_connect() as conn:
                row = conn.execute("SELECT active_version FROM strategies WHERE id=?", (sid,)).fetchone()
                if not row:
                    raise APIError("找不到该策略。", 404)
                version = row["active_version"] + 1
                conn.execute("UPDATE strategies SET name=?,active_version=?,updated_at=? WHERE id=?", (definition["name"], version, stamp, sid))
                conn.execute("INSERT INTO strategy_versions(id,strategy_id,version,definition,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), sid, version, json.dumps(definition, ensure_ascii=False), stamp))
            self.send_json({"id": sid, "active_version": version, "definition": definition}, 201)
            return
        if method == "DELETE" and match:
            with db_connect() as conn:
                cursor = conn.execute("UPDATE strategies SET archived_at=? WHERE id=? AND archived_at IS NULL", (utc_now(), match.group(1)))
            if not cursor.rowcount:
                raise APIError("找不到该策略。", 404)
            self.send_json({"archived": True})
            return
        match = re.fullmatch(r"/api/strategies/([0-9a-fA-F-]{36})/duplicate", path)
        if method == "POST" and match:
            with db_connect() as conn:
                old = version_definition(conn, match.group(1))
            definition = clean_definition({**old, **self.read_json()})
            sid, stamp = str(uuid.uuid4()), utc_now()
            with db_connect() as conn:
                conn.execute("INSERT INTO strategies(id,name,active_version,created_at,updated_at) VALUES(?,?,1,?,?)", (sid, definition["name"], stamp, stamp))
                conn.execute("INSERT INTO strategy_versions(id,strategy_id,version,definition,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), sid, 1, json.dumps(definition, ensure_ascii=False), stamp))
            self.send_json({"id": sid, "name": definition["name"], "active_version": 1}, 201)
            return
        if method == "POST" and path == "/api/strategies":
            definition = clean_definition(self.read_json())
            sid, stamp = str(uuid.uuid4()), utc_now()
            with db_connect() as conn:
                conn.execute("INSERT INTO strategies(id,name,active_version,created_at,updated_at) VALUES(?,?,1,?,?)", (sid, definition["name"], stamp, stamp))
                conn.execute("INSERT INTO strategy_versions(id,strategy_id,version,definition,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), sid, 1, json.dumps(definition, ensure_ascii=False), stamp))
            self.send_json({"id": sid, "name": definition["name"], "active_version": 1}, 201)
            return
        if method == "POST" and path == "/api/backtests":
            self.send_json(execute_backtest(self.read_json()), 201)
            return
        if method == "POST" and path == "/api/paper/test-connection":
            account = alpaca_request("/account", require_paper_enabled=False)
            if not account:
                raise APIError("Alpaca 模拟账户没有返回账户信息。", 502)
            self.send_json({"connected": True})
            return
        if method == "GET" and path == "/api/paper/account":
            self.send_json(safe_account())
            return
        if method == "GET" and path == "/api/paper/orders":
            query = urllib.parse.urlencode({"status": "all", "limit": "30", "direction": "desc"})
            self.send_json(alpaca_request("/orders?" + query))
            return
        if method == "GET" and path == "/api/paper/runners":
            self.send_json(runner_payloads())
            return
        if method == "POST" and path == "/api/paper/orders":
            payload = self.read_json()
            symbol = str(payload.get("symbol", "")).strip().upper()
            side = str(payload.get("side", "")).lower()
            if not SYMBOL_RE.fullmatch(symbol) or side not in {"buy", "sell"}:
                raise APIError("请填写有效股票代码和买入/卖出方向。")
            try:
                quantity = float(payload.get("qty", 0))
            except (TypeError, ValueError) as exc:
                raise APIError("股数必须是正数。") from exc
            if not math.isfinite(quantity) or not 0 < quantity <= 100000:
                raise APIError("股数需大于 0 且不超过 100,000。")
            order = alpaca_request("/orders", "POST", {
                "symbol": symbol, "qty": f"{quantity:.6f}".rstrip("0").rstrip("."),
                "side": side, "type": "market", "time_in_force": "day",
            })
            self.send_json(order, 201)
            return
        if method == "POST" and path == "/api/paper/runners":
            key, secret = credentials()
            if not key or not secret:
                raise APIError("未配置 Alpaca API 密钥。请先在本机 .env 文件中填写密钥。", 400)
            if not paper_enabled():
                raise APIError("模拟策略运行器尚未启用。确认使用模拟账户后，在 .env 中设置 ALPACA_PAPER_TRADING_ENABLED=true 并重启服务。", 403)
            payload = self.read_json()
            symbol = str(payload.get("symbol", "")).strip().upper()
            timeframe = str(payload.get("timeframe", "1Day"))
            strategy_id = str(payload.get("strategy_id", ""))
            if not SYMBOL_RE.fullmatch(symbol):
                raise APIError("请输入有效的美股代码，例如 AAPL。")
            if timeframe not in TIMEFRAME_PERIODS:
                raise APIError("周期需为 1Day、1Hour 或 15Min。")
            try:
                quantity = float(payload.get("qty", 0))
            except (TypeError, ValueError) as exc:
                raise APIError("股数必须是正数。") from exc
            if not math.isfinite(quantity) or not 0 < quantity <= 100000:
                raise APIError("股数需大于 0 且不超过 100,000。")
            # Verify both the credential and the paper-account endpoint before starting a worker.
            safe_account()
            with db_connect() as conn:
                definition = version_definition(conn, strategy_id)
            runner_id, stamp = str(uuid.uuid4()), utc_now()
            with db_connect() as conn:
                conn.execute(
                    "INSERT INTO paper_runners(id,status,symbol,timeframe,qty,strategy_id,strategy_version,strategy_snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (runner_id, "running", symbol, timeframe, quantity, strategy_id, definition["version"], json.dumps(definition, ensure_ascii=False), stamp),
                )
            event = threading.Event()
            with RUNNER_EVENTS_LOCK:
                RUNNER_EVENTS[runner_id] = event
            thread = threading.Thread(target=paper_runner_loop, args=(runner_id, event), daemon=True, name=f"paper-runner-{runner_id[:8]}")
            thread.start()
            self.send_json({"id": runner_id, "status": "running", "created_at": stamp}, 201)
            return
        match = re.fullmatch(r"/api/paper/runners/([0-9a-fA-F-]{36})/stop", path)
        if method == "POST" and match:
            runner_id = match.group(1)
            with RUNNER_EVENTS_LOCK:
                event = RUNNER_EVENTS.get(runner_id)
                if event:
                    event.set()
            with db_connect() as conn:
                cursor = conn.execute(
                    "UPDATE paper_runners SET status='stopped',stopped_at=? WHERE id=? AND status='running'",
                    (utc_now(), runner_id),
                )
                if not cursor.rowcount:
                    exists = conn.execute("SELECT 1 FROM paper_runners WHERE id=?", (runner_id,)).fetchone()
                    if not exists:
                        raise APIError("找不到这个模拟策略运行器。", 404)
            self.send_json({"id": runner_id, "status": "stopped"})
            return
        raise APIError("接口不存在。", 404)

    def serve_file(self, path: str) -> None:
        files = {"/": (WEB / "index.html", "text/html; charset=utf-8"), "/app.js": (WEB / "app.js", "text/javascript; charset=utf-8"), "/styles.css": (WEB / "styles.css", "text/css; charset=utf-8")}
        item = files.get(path)
        if not item:
            raise APIError("页面不存在。", 404)
        file_path, content_type = item
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path.startswith("/api/"):
                self.api("GET", parsed.path)
            else:
                self.serve_file(parsed.path)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self.send_json({"error": f"服务器内部错误：{exc}"}, 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            self.api("POST", path)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self.send_json({"error": f"服务器内部错误：{exc}"}, 500)

    def do_PUT(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            self.api("PUT", path)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self.send_json({"error": f"服务器内部错误：{exc}"}, 500)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            self.api("DELETE", path)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self.send_json({"error": f"服务器内部错误：{exc}"}, 500)


def main() -> None:
    load_local_env()
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"Alpaca Workbench is running at http://{HOST}:{PORT}")
    env_path = ROOT / ".env"
    print(f"Configuration file: {env_path} ({'found' if env_path.is_file() else 'missing'})")
    key, secret = credentials()
    print(f"Alpaca API credentials: {'loaded' if key and secret else 'missing'}")
    print("Trading requests are restricted to Alpaca's paper API.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local workbench...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
