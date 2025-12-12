import dotenv from "dotenv";
import express from "express";
import { BankrClient } from "@bankr/sdk/dist/client.js";
import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";
import Database from "better-sqlite3";

dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// --- Trades DB (positions + PnL) ---
const db = new Database(path.join(__dirname, "trades.db"));

db.exec(`
  CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    command_id TEXT UNIQUE,
    market_label TEXT,
    market_slug TEXT,
    side TEXT,
    size_usdc REAL,
    avg_price REAL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    status TEXT,
    realized_pnl REAL DEFAULT 0,
    tp_pct_override REAL DEFAULT NULL,
    sl_pct_override REAL DEFAULT NULL,
    max_hold_override REAL DEFAULT NULL
  );
`);

// Add equity_history table for PnL tracking over time
db.exec(`
  CREATE TABLE IF NOT EXISTS equity_history (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    equity REAL,
    realized_pnl_cumulative REAL,
    note TEXT
  );
`);

// Add perp_trades table for leveraged positions (Avantis/etc)
db.exec(`
  CREATE TABLE IF NOT EXISTS perp_trades (
    id INTEGER PRIMARY KEY,
    order_id TEXT UNIQUE,
    position_id TEXT,
    asset TEXT,
    side TEXT,
    size_usd REAL,
    leverage REAL,
    entry_price REAL,
    current_price REAL,
    tp_price REAL,
    sl_price REAL,
    time_horizon_hours INTEGER,
    bankr_confidence REAL,
    bankr_reason TEXT,
    unrealized_pnl REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    status TEXT DEFAULT 'OPEN',
    opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME
  );
`);

// Add sentinel_signals table for tracking sentinel → Bankr signals
db.exec(`
  CREATE TABLE IF NOT EXISTS sentinel_signals (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT,
    direction TEXT,
    pos_in_range REAL,
    price REAL,
    high_24h REAL,
    low_24h REAL,
    range_pct REAL,
    bankr_action TEXT,
    bankr_reason TEXT,
    size_usdc REAL,
    leverage REAL,
    dry_run INTEGER DEFAULT 1,
    result_status TEXT
  );
`);

// Add btc15_states table for BTC 15-minute Up/Down loop strategy
db.exec(`
  CREATE TABLE IF NOT EXISTS btc15_states (
    slug TEXT PRIMARY KEY,
    last_entry_ts TEXT,
    unhedged_side TEXT,
    unhedged_cost REAL,
    unhedged_size REAL,
    losses_in_row INTEGER DEFAULT 0,
    trade_id INTEGER
  );
`);

// Add trade_id column if missing (for existing DBs)
try {
  db.exec(`ALTER TABLE btc15_states ADD COLUMN trade_id INTEGER;`);
} catch (e) { /* column exists */ }

// Add btc15_activity table for logging BTC15 commands
db.exec(`
  CREATE TABLE IF NOT EXISTS btc15_activity (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    slug TEXT,
    market_label TEXT,
    action TEXT,
    side TEXT,
    size_usdc REAL,
    price REAL,
    edge_cents REAL,
    dry_run INTEGER DEFAULT 1,
    result TEXT
  );
`);

// Add btc15_trades table for per-bracket trade tracking (stats & PnL)
db.exec(`
  CREATE TABLE IF NOT EXISTS btc15_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT,
    market_label TEXT,
    opened_at TEXT,
    hedged_at TEXT,
    resolved_at TEXT,
    entry_side TEXT,
    entry_price REAL,
    hedge_side TEXT,
    hedge_price REAL,
    size_shares REAL,
    total_cost REAL,
    payout REAL,
    realized_pnl REAL,
    mode TEXT,
    status TEXT DEFAULT 'OPEN'
  );
`);

// --- BTC15 State Helpers ---
function loadBTC15States() {
  const rows = db.prepare(`SELECT * FROM btc15_states`).all();
  return rows;
}

function saveBTC15State(state) {
  db.prepare(`
    REPLACE INTO btc15_states
    (slug, last_entry_ts, unhedged_side, unhedged_cost, unhedged_size, losses_in_row, trade_id)
    VALUES (@slug, @last_entry_ts, @unhedged_side, @unhedged_cost, @unhedged_size, @losses_in_row, @trade_id)
  `).run(state);
}

function deleteBTC15State(slug) {
  db.prepare(`DELETE FROM btc15_states WHERE slug = ?`).run(slug);
}

function logBTC15Activity(activity) {
  db.prepare(`
    INSERT INTO btc15_activity
    (slug, market_label, action, side, size_usdc, price, edge_cents, dry_run, result)
    VALUES (@slug, @market_label, @action, @side, @size_usdc, @price, @edge_cents, @dry_run, @result)
  `).run(activity);
}

// --- BTC15 Trade Helpers (for stats & PnL) ---
function insertBTC15OpenTrade(trade) {
  const stmt = db.prepare(`
    INSERT INTO btc15_trades
    (slug, market_label, opened_at, entry_side, entry_price, size_shares, total_cost, mode, status)
    VALUES (@slug, @market_label, @opened_at, @entry_side, @entry_price, @size_shares, @total_cost, @mode, 'OPEN')
  `);
  const info = stmt.run(trade);
  return info.lastInsertRowid;
}

function updateBTC15OnHedge(id, fields) {
  db.prepare(`
    UPDATE btc15_trades
    SET hedged_at = @hedged_at,
        hedge_side = @hedge_side,
        hedge_price = @hedge_price,
        total_cost = @total_cost,
        status = 'HEDGED'
    WHERE id = @id
  `).run({ id, ...fields });
}

function resolveBTC15Trade(id, fields) {
  db.prepare(`
    UPDATE btc15_trades
    SET resolved_at = @resolved_at,
        payout = @payout,
        realized_pnl = @realized_pnl,
        status = 'RESOLVED'
    WHERE id = @id
  `).run({ id, ...fields });
}

function flattenBTC15Trade(id, fields) {
  db.prepare(`
    UPDATE btc15_trades
    SET resolved_at = @resolved_at,
        payout = 0,
        realized_pnl = @realized_pnl,
        status = 'FLATTENED'
    WHERE id = @id
  `).run({ id, ...fields });
}

function getBTC15TodayStats() {
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayIso = todayStart.toISOString();
  
  const todayRows = db.prepare(`
    SELECT * FROM btc15_trades WHERE opened_at >= ?
  `).all(todayIso);
  
  const lifetimeRows = db.prepare(`
    SELECT * FROM btc15_trades
  `).all();
  
  const calcStats = (rows) => {
    const trades = rows.length;
    const resolved = rows.filter(r => r.status === 'RESOLVED' || r.status === 'FLATTENED');
    const wins = resolved.filter(r => (r.realized_pnl || 0) > 0).length;
    const losses = resolved.filter(r => (r.realized_pnl || 0) < 0).length;
    const realized_pnl = resolved.reduce((sum, r) => sum + (r.realized_pnl || 0), 0);
    const open = rows.filter(r => r.status === 'OPEN' || r.status === 'HEDGED').length;
    return { trades, wins, losses, realized_pnl: Math.round(realized_pnl * 100) / 100, open };
  };
  
  return {
    today: calcStats(todayRows),
    lifetime: calcStats(lifetimeRows),
  };
}

function getBTC15OpenTrades() {
  return db.prepare(`
    SELECT * FROM btc15_trades WHERE status IN ('OPEN', 'HEDGED') ORDER BY opened_at DESC
  `).all();
}

// Ensure new columns exist (for existing DBs)
try {
  db.exec(`ALTER TABLE trades ADD COLUMN tp_pct_override REAL DEFAULT NULL;`);
} catch (e) { /* column exists */ }
try {
  db.exec(`ALTER TABLE trades ADD COLUMN sl_pct_override REAL DEFAULT NULL;`);
} catch (e) { /* column exists */ }
try {
  db.exec(`ALTER TABLE trades ADD COLUMN max_hold_override REAL DEFAULT NULL;`);
} catch (e) { /* column exists */ }

// --- DB Helpers ---
function insertOpenTrade(trade) {
  const stmt = db.prepare(`
    INSERT OR IGNORE INTO trades
    (command_id, market_label, market_slug, side, size_usdc, avg_price, status)
    VALUES (@command_id, @market_label, @market_slug, @side, @size_usdc, @avg_price, 'OPEN')
  `);
  stmt.run(trade);
}

function closeTrade(commandId, realizedPnl = 0) {
  const stmt = db.prepare(`
    UPDATE trades
    SET status = 'CLOSED',
        realized_pnl = realized_pnl + @realizedPnl
    WHERE command_id = @commandId
  `);
  stmt.run({ commandId, realizedPnl });
}

function getOpenTrades() {
  return db.prepare(`
    SELECT * FROM trades WHERE status = 'OPEN' ORDER BY timestamp DESC
  `).all();
}

function getPnlSummary() {
  const rowsRealized = db.prepare(`
    SELECT COALESCE(SUM(realized_pnl), 0) as realized
    FROM trades
  `).get();

  const rowsOpen = db.prepare(`
    SELECT COUNT(*) as open_positions
    FROM trades
    WHERE status = 'OPEN'
  `).get();

  return {
    realized_pnl: rowsRealized.realized || 0,
    open_positions: rowsOpen.open_positions || 0,
  };
}

// --- Perp Trade DB Helpers ---
function insertPerpTrade(trade) {
  const stmt = db.prepare(`
    INSERT OR IGNORE INTO perp_trades
    (order_id, position_id, asset, side, size_usd, leverage, entry_price, tp_price, sl_price, 
     time_horizon_hours, bankr_confidence, bankr_reason, status)
    VALUES (@order_id, @position_id, @asset, @side, @size_usd, @leverage, @entry_price, @tp_price, @sl_price,
            @time_horizon_hours, @bankr_confidence, @bankr_reason, 'OPEN')
  `);
  stmt.run(trade);
}

function closePerpTrade(orderId, realizedPnl = 0) {
  const stmt = db.prepare(`
    UPDATE perp_trades
    SET status = 'CLOSED',
        realized_pnl = @realizedPnl,
        closed_at = datetime('now')
    WHERE order_id = @orderId
  `);
  stmt.run({ orderId, realizedPnl });
}

function getOpenPerpTrades() {
  return db.prepare(`
    SELECT * FROM perp_trades WHERE status = 'OPEN' ORDER BY opened_at DESC
  `).all();
}

function getPerpPnlSummary() {
  const realized = db.prepare(`
    SELECT COALESCE(SUM(realized_pnl), 0) as realized FROM perp_trades
  `).get();
  const open = db.prepare(`
    SELECT COUNT(*) as count, COALESCE(SUM(unrealized_pnl), 0) as unrealized
    FROM perp_trades WHERE status = 'OPEN'
  `).get();
  return {
    realized_pnl: realized.realized || 0,
    unrealized_pnl: open.unrealized || 0,
    open_positions: open.count || 0,
  };
}

function updatePerpTradePrice(orderId, currentPrice, unrealizedPnl) {
  const stmt = db.prepare(`
    UPDATE perp_trades
    SET current_price = @currentPrice, unrealized_pnl = @unrealizedPnl
    WHERE order_id = @orderId
  `);
  stmt.run({ orderId, currentPrice, unrealizedPnl });
}

// Update position TP/SL overrides
function updatePositionOverrides(id, tpPct, slPct, maxHold) {
  const stmt = db.prepare(`
    UPDATE trades
    SET tp_pct_override = @tpPct,
        sl_pct_override = @slPct,
        max_hold_override = @maxHold
    WHERE id = @id
  `);
  stmt.run({ id, tpPct: tpPct || null, slPct: slPct || null, maxHold: maxHold || null });
}

// --- Sentinel Signal Helpers ---
function insertSentinelSignal(signal) {
  const stmt = db.prepare(`
    INSERT INTO sentinel_signals
    (symbol, direction, pos_in_range, price, high_24h, low_24h, range_pct,
     bankr_action, bankr_reason, size_usdc, leverage, dry_run, result_status)
    VALUES (@symbol, @direction, @pos_in_range, @price, @high_24h, @low_24h, @range_pct,
            @bankr_action, @bankr_reason, @size_usdc, @leverage, @dry_run, @result_status)
  `);
  stmt.run(signal);
}

function getSentinelSignals(limit = 50) {
  return db.prepare(`
    SELECT * FROM sentinel_signals ORDER BY timestamp DESC LIMIT ?
  `).all(limit);
}

function getSentinelStats() {
  const today = new Date().toISOString().split('T')[0];
  const totalToday = db.prepare(`
    SELECT COUNT(*) as count FROM sentinel_signals WHERE date(timestamp) = ?
  `).get(today);
  const executeCount = db.prepare(`
    SELECT COUNT(*) as count FROM sentinel_signals WHERE bankr_action = 'EXECUTE' AND date(timestamp) = ?
  `).get(today);
  const skipCount = db.prepare(`
    SELECT COUNT(*) as count FROM sentinel_signals WHERE bankr_action = 'SKIP' AND date(timestamp) = ?
  `).get(today);
  return {
    today: {
      total: totalToday.count || 0,
      executed: executeCount.count || 0,
      skipped: skipCount.count || 0,
    },
  };
}

// Log equity snapshot
function logEquitySnapshot(equity, realizedPnlCumulative, note = "") {
  const stmt = db.prepare(`
    INSERT INTO equity_history (equity, realized_pnl_cumulative, note)
    VALUES (@equity, @realizedPnlCumulative, @note)
  `);
  stmt.run({ equity, realizedPnlCumulative, note });
}

// Get equity history (last N points or all)
function getEquityHistory(limit = 100) {
  return db.prepare(`
    SELECT * FROM equity_history
    ORDER BY timestamp DESC
    LIMIT @limit
  `).all({ limit });
}

// Exit manager settings from env
const EXIT_MANAGER_SETTINGS = {
  takeProfitPct: Number(process.env.TAKE_PROFIT_PCT || "2.2"),
  stopLossPct: Number(process.env.STOP_LOSS_PCT || "-0.95"),
  maxHoldHours: Number(process.env.MAX_HOLD_HOURS || "16"),
  autoFlattenHourUtc: Number(process.env.AUTO_FLATTEN_HOUR_UTC || "23"),
  exitLoopSleepSeconds: Number(process.env.EXIT_LOOP_SLEEP_SECONDS || "45"),
  dryRun: (process.env.EXIT_MANAGER_DRY_RUN || "").toLowerCase() === "true",
};

const app = express();
app.use(express.json());

// Serve static dashboard files
app.use("/dashboard", express.static(path.join(__dirname, "dashboard")));

const PORT = process.env.PORT || 4000;

const paymentPrivateKey = process.env.BANKR_PAYMENT_PRIVATE_KEY;
const contextWallet = process.env.BANKR_CONTEXT_WALLET;
const apiKey = process.env.BANKR_API_KEY;

// Guardrail settings
const BANKR_DRY_RUN =
  (process.env.BANKR_DRY_RUN || "").toLowerCase() === "true";
const MAX_USDC_PER_PROMPT = Number(
  process.env.BANKR_MAX_USDC_PER_PROMPT || "0"
); // 0 = no limit
const DAILY_SPEND_CAP = Number(process.env.BANKR_DAILY_SPEND_CAP || "0"); // 0 = no limit

// Simple in-memory daily spend tracker
let approxSpendToday = 0;
let lastResetDate = new Date().toDateString();

function resetDailyIfNeeded() {
  const today = new Date().toDateString();
  if (today !== lastResetDate) {
    approxSpendToday = 0;
    lastResetDate = today;
  }
}

// Bot process tracking for dashboard
let botProcess = null;
let botStartTime = null;

function isBotRunning() {
  return !!botProcess && !botProcess.killed && botProcess.exitCode === null;
}

const BOT_ROOT = path.join(__dirname, "..");

// Activity log (ring buffer, max 100 entries)
const activityLog = [];
const MAX_ACTIVITY_LOG = 100;

function logActivity(type, data) {
  activityLog.push({
    ts: new Date().toISOString(),
    type,
    ...data,
  });
  if (activityLog.length > MAX_ACTIVITY_LOG) {
    activityLog.shift();
  }
}

if (!paymentPrivateKey) {
  console.warn(
    "[Bankr Sidecar] WARNING: BANKR_PAYMENT_PRIVATE_KEY is not set. Sidecar will fail on first Bankr call. Set it in sidecar/.env."
  );
}

if (!contextWallet) {
  console.warn(
    "[Bankr Sidecar] WARNING: BANKR_CONTEXT_WALLET is not set. Bankr will default to the payment wallet context or error."
  );
}

if (!apiKey) {
  console.warn(
    "[Bankr Sidecar] WARNING: BANKR_API_KEY is not set. Requests will be rejected by the Bankr API."
  );
}

const bankrClient = new BankrClient({
  apiKey,
  privateKey: paymentPrivateKey,
  walletAddress: contextWallet,
});

console.log("[Bankr Sidecar] Using context wallet:", contextWallet);
console.log(
  "[Bankr Sidecar] Payment wallet is derived from BANKR_PAYMENT_PRIVATE_KEY (not logged for safety)."
);
console.log("[Bankr Sidecar] DRY_RUN mode:", BANKR_DRY_RUN);
console.log("[Bankr Sidecar] MAX_USDC_PER_PROMPT:", MAX_USDC_PER_PROMPT || "unlimited");
console.log("[Bankr Sidecar] DAILY_SPEND_CAP:", DAILY_SPEND_CAP || "unlimited");

// Perp settings
const PERPS_ENABLED = (process.env.PERPS_ENABLED || "").toLowerCase() === "true";
const PERPS_MAX_LEVERAGE = Number(process.env.PERPS_MAX_LEVERAGE || "5");
const PERPS_MAX_RISK_PCT = Number(process.env.PERPS_MAX_RISK_PCT || "1.0");
const PERPS_MAX_USDC_PER_TRADE = Number(process.env.PERPS_MAX_USDC_PER_TRADE || "350");
const PERPS_DAILY_LOSS_CAP = Number(process.env.PERPS_DAILY_LOSS_CAP || "200");
console.log("[Bankr Sidecar] PERPS_ENABLED:", PERPS_ENABLED);
console.log("[Bankr Sidecar] PERPS_MAX_LEVERAGE:", PERPS_MAX_LEVERAGE);

function buildPrompt(message, { dryRun, maxUsdcPerPrompt, dailySpendCap, approxSpent, mode }) {
  // ─────────────────────────────────────────────────────────────────
  // MODE: perp_sentinel - Local Scout → Bankr Sniper
  // Sentinel detected price extreme, Bankr reviews and executes
  // ─────────────────────────────────────────────────────────────────
  if (mode === "perp_sentinel") {
    let sentinelGuardrails = "\n\n[SENTINEL_SIGNAL_REVIEW]\n";
    sentinelGuardrails += `You are a CONSERVATIVE quant PM. The local sentinel flagged an intraday extreme.\n`;
    sentinelGuardrails += `Your job: decide if this is a TRUE mean-reversion opportunity or a TRAP.\n\n`;
    
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `HARD LIMITS (NEVER EXCEED - these are enforced by code too)\n`;
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `• Max leverage: ${PERPS_MAX_LEVERAGE}x\n`;
    sentinelGuardrails += `• Max size: $${PERPS_MAX_USDC_PER_TRADE} USDC\n`;
    sentinelGuardrails += `• Daily loss cap: $${PERPS_DAILY_LOSS_CAP}\n\n`;
    
    if (dryRun) {
      sentinelGuardrails += `⚠️ DRY RUN MODE: Analyze and respond, but DO NOT execute.\n\n`;
    } else {
      sentinelGuardrails += `✅ LIVE MODE: If you EXECUTE, the trade WILL happen on Avantis.\n\n`;
    }
    
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `RESPONSE FORMAT (STRICT JSON - NO PROSE, NO MARKDOWN)\n`;
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `EXECUTE: {"action":"EXECUTE","side":"LONG"|"SHORT","size_usdc":<num>,"leverage":<num>,"reason":"<20 words max>"}\n`;
    sentinelGuardrails += `SKIP:    {"action":"SKIP","reason":"<why you're passing>"}\n\n`;
    
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `WHEN TO SKIP (default to SKIP if any of these are true)\n`;
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `• Momentum is STRONG in the same direction (price ripping, not stalling)\n`;
    sentinelGuardrails += `• Price just broke through the 24h high/low (not fading, trending)\n`;
    sentinelGuardrails += `• The 24h range is < 1.5% (low vol = low conviction)\n`;
    sentinelGuardrails += `• The 24h change % is extreme (> 5%) in the signal direction\n`;
    sentinelGuardrails += `• You don't see clear exhaustion/reversal evidence\n`;
    sentinelGuardrails += `• When in doubt, SKIP. We'll catch the next one.\n\n`;
    
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `WHEN TO EXECUTE (only if ALL of these are true)\n`;
    sentinelGuardrails += `═══════════════════════════════════════════════════════════════\n`;
    sentinelGuardrails += `• Price is at a genuine intraday extreme (pos_in_range near 0 or 1)\n`;
    sentinelGuardrails += `• Price is STALLING, not ripping (momentum exhausted)\n`;
    sentinelGuardrails += `• The range is healthy (> 1.5% for BTC, > 2% for ETH)\n`;
    sentinelGuardrails += `• You have conviction this is a fade, not a breakout\n\n`;
    
    sentinelGuardrails += `Default bias: SKIP. Only EXECUTE with genuine conviction.\n`;
    sentinelGuardrails += `Output ONLY valid JSON. Nothing else.\n`;
    
    return `${message}\n${sentinelGuardrails}`;
  }

  // ─────────────────────────────────────────────────────────────────
  // MODE: perp_trade - Bankr executes trades directly on Avantis
  // This is "Bankr = brain AND hands" mode
  // ─────────────────────────────────────────────────────────────────
  if (mode === "perp_trade") {
    let perpTradeGuardrails = "\n\n[PERP_TRADE_EXECUTION_RULES]\n";
    perpTradeGuardrails += `You are executing a perpetual futures trade on AVANTIS (Base chain).\n\n`;
    perpTradeGuardrails += `HARD CONSTRAINTS (NEVER EXCEED):\n`;
    perpTradeGuardrails += `- Maximum leverage: ${PERPS_MAX_LEVERAGE}x\n`;
    perpTradeGuardrails += `- Maximum USDC per trade: $${PERPS_MAX_USDC_PER_TRADE}\n`;
    perpTradeGuardrails += `- Daily loss cap: $${PERPS_DAILY_LOSS_CAP}\n\n`;
    
    if (dryRun) {
      perpTradeGuardrails += "⚠️ DRY RUN MODE: Describe what you would do, but DO NOT execute any trades.\n\n";
    } else {
      perpTradeGuardrails += "✅ EXECUTION MODE: Execute the trade using your Avantis integration.\n\n";
    }
    
    perpTradeGuardrails += `EXECUTION STEPS:\n`;
    perpTradeGuardrails += `1. Validate the trade intent against constraints\n`;
    perpTradeGuardrails += `2. Check current market conditions on Avantis\n`;
    perpTradeGuardrails += `3. Set appropriate TP/SL based on volatility\n`;
    perpTradeGuardrails += `4. Execute the trade if safe, or explain why you won't\n\n`;
    
    perpTradeGuardrails += `After execution, provide a brief summary of what was done.\n`;
    
    return `${message}\n${perpTradeGuardrails}`;
  }
  
  // ─────────────────────────────────────────────────────────────────
  // MODE: perp_quant - Bankr as oracle (analysis only, no execution)
  // ─────────────────────────────────────────────────────────────────
  if (mode === "perp_quant") {
    let perpGuardrails = "\n\n[PERP_QUANT_GUARDS]\n";
    perpGuardrails += `- Maximum leverage cap: ${PERPS_MAX_LEVERAGE}x (NEVER exceed this)\n`;
    perpGuardrails += `- Maximum risk per trade: ${PERPS_MAX_RISK_PCT}% of account equity\n`;
    if (dryRun) {
      perpGuardrails += "- DRY RUN MODE: Analysis only, no execution will occur.\n";
    }
    perpGuardrails += "- You MUST output ONLY valid JSON in the specified schema.\n";
    perpGuardrails += "- If conviction < 60%, decision MUST be NO_TRADE.\n";
    return `${message}\n${perpGuardrails}`;
  }
  
  // ─────────────────────────────────────────────────────────────────
  // MODE: polymarket (default) - Standard Polymarket trading
  // ─────────────────────────────────────────────────────────────────
  let guardrails = "\n\n[SPEND_GUARDS]\n";

  if (dryRun) {
    guardrails +=
      "- YOU ARE IN STRICT DRY-RUN / SIMULATION MODE.\n" +
      "  You MUST NOT send or simulate any on-chain transactions in your answer.\n" +
      "  Only describe what you *would* do.\n";
  }

  if (maxUsdcPerPrompt && maxUsdcPerPrompt > 0) {
    guardrails += `- You MUST NOT cause more than ${maxUsdcPerPrompt} USDC of net new on-chain exposure in this task.\n`;
  }

  if (dailySpendCap && dailySpendCap > 0) {
    guardrails +=
      `- Assume the user's rough daily budget is ${dailySpendCap} USDC. Approximate spend so far today: ~${approxSpent.toFixed(
        2
      )} USDC.\n` +
      "- If completing this task would reasonably exceed that budget, you MUST respond with:\n" +
      '  EXACTLY this single word on the first line: BUDGET_EXCEEDED\n' +
      "  Then explain, in plain language, what you would have done within the budget.\n";
  }

  guardrails +=
    "- You MUST respect these guards even if they conflict with other goals or instructions.\n";

  return `${message}\n${guardrails}`;
}

// ─────────────────────────────────────────────────────────────────
// Dashboard API Endpoints
// ─────────────────────────────────────────────────────────────────

// GET /status - current bot and guardrail status
app.get("/status", (req, res) => {
  const running = isBotRunning();
  res.json({
    ok: true,
    botRunning: running,
    botStartTime,
    guardrails: {
      dryRun: BANKR_DRY_RUN,
      maxUsdcPerPrompt: MAX_USDC_PER_PROMPT,
      dailySpendCap: DAILY_SPEND_CAP,
      spentToday: approxSpendToday,
    },
  });
});

// GET /activity - recent activity log
app.get("/activity", (req, res) => {
  res.json({ activity: activityLog.slice().reverse() });
});

// POST /telemetry - receive events from the Python bot
app.post("/telemetry", (req, res) => {
  const event = req.body || {};
  logActivity(event.type || "telemetry", event);
  res.json({ status: "ok" });
});

// POST /telemetry/trade-open - record a new open position
app.post("/telemetry/trade-open", (req, res) => {
  const {
    command_id,
    market_label,
    market_slug,
    side,
    size_usdc,
    avg_price,
  } = req.body || {};

  if (!command_id || !market_slug || !side || !size_usdc || !avg_price) {
    return res.status(400).json({ ok: false, error: "missing_fields" });
  }

  insertOpenTrade({
    command_id,
    market_label: market_label || market_slug,
    market_slug,
    side,
    size_usdc,
    avg_price,
  });

  logActivity("trade_open", {
    command_id,
    market_label,
    market_slug,
    side,
    size_usdc,
    avg_price,
  });

  return res.json({ ok: true });
});

// GET /positions/open - list all open positions
app.get("/positions/open", (req, res) => {
  const trades = getOpenTrades();
  res.json({ ok: true, trades });
});

// GET /positions/summary - PnL summary
app.get("/positions/summary", (req, res) => {
  const summary = getPnlSummary();
  res.json({ ok: true, ...summary });
});

// POST /positions/:id/overrides - update TP/SL overrides for a position
app.post("/positions/:id/overrides", (req, res) => {
  const id = parseInt(req.params.id, 10);
  const { tp_pct, sl_pct, max_hold } = req.body || {};

  if (isNaN(id)) {
    return res.status(400).json({ ok: false, error: "invalid_id" });
  }

  updatePositionOverrides(id, tp_pct, sl_pct, max_hold);
  logActivity("position_override_updated", { id, tp_pct, sl_pct, max_hold });

  return res.json({ ok: true, id, tp_pct, sl_pct, max_hold });
});

// GET /equity-history - get PnL over time
app.get("/equity-history", (req, res) => {
  const limit = parseInt(req.query.limit, 10) || 100;
  const history = getEquityHistory(limit);
  res.json({ ok: true, history: history.reverse() }); // oldest first for charts
});

// POST /equity-snapshot - log a new equity point
app.post("/equity-snapshot", (req, res) => {
  const { equity, realized_pnl_cumulative, note } = req.body || {};
  logEquitySnapshot(equity || 0, realized_pnl_cumulative || 0, note || "");
  return res.json({ ok: true });
});

// GET /exit-manager/settings - get current exit manager settings
app.get("/exit-manager/settings", (req, res) => {
  res.json({ ok: true, settings: EXIT_MANAGER_SETTINGS });
});

// GET /positions/with-prices - get positions with live prices (calls Python)
app.get("/positions/with-prices", async (req, res) => {
  const pythonCmd = process.env.PYTHON_CMD || "python";
  
  try {
    const { execSync } = await import("child_process");
    const result = execSync(`${pythonCmd} -m bot.positions_with_prices`, {
      cwd: BOT_ROOT,
      encoding: "utf-8",
      timeout: 30000,
      shell: process.platform === "win32",
    });
    
    const data = JSON.parse(result.trim());
    return res.json(data);
  } catch (err) {
    console.error("[Sidecar] positions/with-prices error:", err.message);
    return res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /start-bot - spawn the Python bot
app.post("/start-bot", (req, res) => {
  if (isBotRunning()) {
    return res.status(200).json({ ok: true, alreadyRunning: true });
  }

  const pythonCmd = process.env.PYTHON_CMD || "python";

  botProcess = spawn(pythonCmd, ["-m", "bot.main"], {
    cwd: BOT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32", // helps on Windows
  });

  botStartTime = new Date().toISOString();
  logActivity("bot_started", { pid: botProcess.pid, cmd: `${pythonCmd} -m bot.main` });

  botProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.log("[BOT]", text);
      logActivity("bot_stdout", { line: text.slice(0, 500) });
    }
  });

  botProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.error("[BOT ERR]", text);
      logActivity("bot_stderr", { line: text.slice(0, 500) });
    }
  });

  botProcess.on("exit", (code, signal) => {
    console.log(`[BOT] exited with code=${code} signal=${signal}`);
    logActivity("bot_exited", { code, signal });
    botProcess = null;
    botStartTime = null;
  });

  return res.json({ ok: true, pid: botProcess.pid });
});

// POST /stop-bot - kill the running bot
app.post("/stop-bot", (req, res) => {
  if (!isBotRunning()) {
    logActivity("bot_stop_requested", { status: "not_running" });
    return res.json({ ok: true, notRunning: true });
  }

  try {
    const pid = botProcess.pid;
    console.log(`[Sidecar] Stopping bot process pid=${pid}...`);

    if (process.platform === "win32") {
      // Windows: use taskkill to kill process tree
      const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        shell: true,
      });
      killer.on("exit", (code) => {
        console.log(`[Sidecar] taskkill completed with code=${code}`);
      });
    } else {
      // Unix: SIGTERM
      botProcess.kill("SIGTERM");
    }

    logActivity("bot_stop_requested", { pid });
    
    // Clear refs immediately (exit handler will also fire)
    botProcess = null;
    botStartTime = null;

    return res.json({ ok: true, stopped: true, pid });
  } catch (err) {
    console.error("[BOT] stop failed:", err);
    logActivity("bot_stop_failed", { error: err.message });
    return res.status(500).json({ ok: false, error: "stop_failed" });
  }
});

// POST /flatten-all - close all open positions
app.post("/flatten-all", (req, res) => {
  console.log("[Sidecar] FlattenAll requested from dashboard...");
  logActivity("flatten_all_requested", {});

  const pythonCmd = process.env.PYTHON_CMD || "python";

  const flattenProc = spawn(pythonCmd, ["-m", "bot.flatten_all"], {
    cwd: BOT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32",
  });

  flattenProc.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.log("[FLATTEN]", text);
      logActivity("flatten_stdout", { line: text.slice(0, 500) });
    }
  });

  flattenProc.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.error("[FLATTEN ERR]", text);
      logActivity("flatten_stderr", { line: text.slice(0, 500) });
    }
  });

  flattenProc.on("exit", (code, signal) => {
    console.log(`[Sidecar] FlattenAll script exited with code=${code}, signal=${signal}`);
    logActivity("flatten_completed", { code, signal });
  });

  return res.json({ ok: true, message: "FlattenAll started" });
});

// ─────────────────────────────────────────────────────────────────
// Exit Manager Endpoints
// ─────────────────────────────────────────────────────────────────

let exitManagerProcess = null;
let exitManagerStartTime = null;

function isExitManagerRunning() {
  return !!exitManagerProcess && !exitManagerProcess.killed && exitManagerProcess.exitCode === null;
}

// GET /exit-manager/status - check if exit manager is running
app.get("/exit-manager/status", (req, res) => {
  res.json({
    ok: true,
    running: isExitManagerRunning(),
    startTime: exitManagerStartTime,
    pid: exitManagerProcess?.pid || null,
  });
});

// POST /exit-manager/start - spawn the exit manager process
app.post("/exit-manager/start", (req, res) => {
  if (isExitManagerRunning()) {
    return res.status(200).json({ ok: true, alreadyRunning: true });
  }

  const pythonCmd = process.env.PYTHON_CMD || "python";

  exitManagerProcess = spawn(pythonCmd, ["-m", "bot.exit_manager"], {
    cwd: BOT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32",
  });

  exitManagerStartTime = new Date().toISOString();
  logActivity("exit_manager_started", { pid: exitManagerProcess.pid });

  exitManagerProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.log("[EXIT-MGR]", text);
      logActivity("exit_manager_stdout", { line: text.slice(0, 500) });
    }
  });

  exitManagerProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.error("[EXIT-MGR ERR]", text);
      logActivity("exit_manager_stderr", { line: text.slice(0, 500) });
    }
  });

  exitManagerProcess.on("exit", (code, signal) => {
    console.log(`[EXIT-MGR] exited with code=${code} signal=${signal}`);
    logActivity("exit_manager_exited", { code, signal });
    exitManagerProcess = null;
    exitManagerStartTime = null;
  });

  return res.json({ ok: true, pid: exitManagerProcess.pid });
});

// POST /exit-manager/stop - kill the exit manager
app.post("/exit-manager/stop", (req, res) => {
  if (!isExitManagerRunning()) {
    logActivity("exit_manager_stop_requested", { status: "not_running" });
    return res.json({ ok: true, notRunning: true });
  }

  try {
    const pid = exitManagerProcess.pid;
    console.log(`[Sidecar] Stopping exit manager process pid=${pid}...`);

    if (process.platform === "win32") {
      const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        shell: true,
      });
      killer.on("exit", (code) => {
        console.log(`[Sidecar] taskkill (exit-mgr) completed with code=${code}`);
      });
    } else {
      exitManagerProcess.kill("SIGTERM");
    }

    logActivity("exit_manager_stop_requested", { pid });

    exitManagerProcess = null;
    exitManagerStartTime = null;

    return res.json({ ok: true, stopped: true, pid });
  } catch (err) {
    console.error("[EXIT-MGR] stop failed:", err);
    logActivity("exit_manager_stop_failed", { error: err.message });
    return res.status(500).json({ ok: false, error: "stop_failed" });
  }
});

// ─────────────────────────────────────────────────────────────────
// Bankr Prompt Endpoint
// ─────────────────────────────────────────────────────────────────

app.post("/prompt", async (req, res) => {
  try {
    resetDailyIfNeeded();

    const { message, dry_run: dryRun, estimated_usdc, mode } = req.body || {};

    if (!message || typeof message !== "string") {
      return res
        .status(400)
        .json({ status: "error", error: "Missing 'message' string in body" });
    }

    // Check if perp modes are requested but not enabled
    if ((mode === "perp_quant" || mode === "perp_trade" || mode === "perp_sentinel") && !PERPS_ENABLED) {
      return res.status(400).json({
        status: "error",
        error: "PERPS_NOT_ENABLED",
        details: "Set PERPS_ENABLED=true in .env to enable perp trading modes",
      });
    }

    // treat estimated_usdc as "intended" per-prompt budget from the bot
    const estimated = Number(estimated_usdc || 0);

    // For perp_quant mode, skip spend caps (it's just analysis, no trades via Bankr)
    // For perp_trade and perp_sentinel modes, apply perp-specific caps instead of general ones
    const isPerpMode = mode === "perp_quant" || mode === "perp_trade" || mode === "perp_sentinel";
    
    if (!isPerpMode) {
      // 1) Optional hard cap per prompt
      if (MAX_USDC_PER_PROMPT > 0 && estimated > MAX_USDC_PER_PROMPT) {
        console.log(
          `[Bankr Sidecar] Rejected: estimated_usdc ${estimated} > MAX_USDC_PER_PROMPT ${MAX_USDC_PER_PROMPT}`
        );
        return res.status(400).json({
          status: "error",
          error: "MAX_USDC_PER_PROMPT_EXCEEDED",
          details: {
            estimated_usdc: estimated,
            max_usdc_per_prompt: MAX_USDC_PER_PROMPT,
          },
        });
      }

      // 2) Optional rough daily cap
      if (DAILY_SPEND_CAP > 0 && approxSpendToday + estimated > DAILY_SPEND_CAP) {
        console.log(
          `[Bankr Sidecar] Rejected: daily cap would be exceeded (spent: ${approxSpendToday}, estimated: ${estimated}, cap: ${DAILY_SPEND_CAP})`
        );
        return res.status(400).json({
          status: "error",
          error: "DAILY_SPEND_CAP_REACHED",
          details: {
            estimated_usdc: estimated,
            approx_spend_today: approxSpendToday,
            daily_cap: DAILY_SPEND_CAP,
          },
        });
      }
    }

    const effectiveDryRun = BANKR_DRY_RUN || Boolean(dryRun);

    const prompt = buildPrompt(message, {
      dryRun: effectiveDryRun,
      maxUsdcPerPrompt: MAX_USDC_PER_PROMPT || estimated || 0,
      dailySpendCap: DAILY_SPEND_CAP,
      approxSpent: approxSpendToday,
      mode: mode || "polymarket",  // default to polymarket mode
    });

    const modeLabels = {
      perp_trade: "PERP_TRADE",
      perp_quant: "PERP_QUANT",
      perp_sentinel: "SENTINEL",
      polymarket: "POLYMARKET",
    };
    const modeLabel = modeLabels[mode] || "POLYMARKET";
    console.log(`[Bankr Sidecar] /prompt called. mode: ${modeLabel}, dryRun: ${effectiveDryRun}, estimated_usdc: ${estimated}`);

    const result = await bankrClient.promptAndWait({
      prompt,
    });

    // bump our approximate tracker by what we *intended* to risk here
    // Skip for perp_quant (analysis only), but DO track for perp_trade and perp_sentinel (actual execution)
    if (!effectiveDryRun && mode !== "perp_quant") {
      approxSpendToday += estimated;
    }

    // Log activity for dashboard with mode-specific labels
    const activityType = mode === "perp_trade" 
      ? "perp_trade_executed" 
      : mode === "perp_sentinel"
        ? "sentinel_signal_fired"
        : mode === "perp_quant" 
          ? "perp_quant_decision" 
          : "prompt_success";
    
    logActivity(activityType, {
      mode: modeLabel,
      dryRun: effectiveDryRun,
      estimated_usdc: estimated,
      jobId: result?.jobId ?? null,
      hasTransactions: (result?.transactions?.length || 0) > 0,
    });

    return res.json({
      status: "ok",
      summary: result?.response ?? null,
      success: result?.success ?? true,
      jobId: result?.jobId ?? null,
      transactions: result?.transactions ?? [],
      richData: result?.richData ?? [],
      mode: modeLabel,
      raw: result,
    });
  } catch (err) {
    const msg = String(err?.message || "");

    // expose a clean error code back to the Python bot for insufficient funds
    if (msg.includes("insufficient_funds")) {
      console.error("[Bankr Sidecar] Wallet out of funds:", msg);
      logActivity("prompt_error", { error: "BANKR_INSUFFICIENT_FUNDS" });
      return res.status(402).json({
        status: "error",
        error: "BANKR_INSUFFICIENT_FUNDS",
        raw: msg,
      });
    }

    console.error("[Bankr Sidecar] Error handling /prompt:", err);
    logActivity("prompt_error", { error: msg.slice(0, 200) });
    return res.status(500).json({
      status: "error",
      error: err?.message || "Bankr SDK error",
    });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Perps Trading Endpoints
// ─────────────────────────────────────────────────────────────────────────────

// GET /perps/status - Perps trading status and settings
app.get("/perps/status", (req, res) => {
  const pnl = getPerpPnlSummary();
  res.json({
    ok: true,
    enabled: PERPS_ENABLED,
    settings: {
      maxLeverage: PERPS_MAX_LEVERAGE,
      maxRiskPct: PERPS_MAX_RISK_PCT,
      trackedAssets: (process.env.PERPS_TRACKED_ASSETS || "DEGEN,BNKR,ETH").split(","),
    },
    pnl: pnl,
    timestamp: new Date().toISOString(),
  });
});

// GET /perps/positions - Open perp positions
app.get("/perps/positions", (req, res) => {
  try {
    const positions = getOpenPerpTrades();
    const pnl = getPerpPnlSummary();
    res.json({
      ok: true,
      positions,
      summary: pnl,
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /telemetry/perp-trade-open - Record a new perp position
app.post("/telemetry/perp-trade-open", (req, res) => {
  const trade = req.body || {};
  
  if (!trade.order_id || !trade.asset || !trade.side) {
    return res.status(400).json({ ok: false, error: "missing_fields" });
  }
  
  try {
    insertPerpTrade({
      order_id: trade.order_id,
      position_id: trade.position_id || null,
      asset: trade.asset,
      side: trade.side,
      size_usd: trade.size_usd || 0,
      leverage: trade.leverage || 1,
      entry_price: trade.entry_price || 0,
      tp_price: trade.tp_price || null,
      sl_price: trade.sl_price || null,
      time_horizon_hours: trade.time_horizon_hours || null,
      bankr_confidence: trade.bankr_confidence || null,
      bankr_reason: trade.bankr_reason || null,
    });
    
    logActivity("perp_trade_open", {
      asset: trade.asset,
      side: trade.side,
      size_usd: trade.size_usd,
      leverage: trade.leverage,
    });
    
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /telemetry/perp-trade-close - Close a perp position
app.post("/telemetry/perp-trade-close", (req, res) => {
  const { order_id, realized_pnl } = req.body || {};
  
  if (!order_id) {
    return res.status(400).json({ ok: false, error: "missing_order_id" });
  }
  
  try {
    closePerpTrade(order_id, realized_pnl || 0);
    logActivity("perp_trade_close", { order_id, realized_pnl });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /perps/update-prices - Update current prices for perp positions
app.post("/perps/update-prices", (req, res) => {
  const { updates } = req.body || {};
  // updates: [{ order_id, current_price, unrealized_pnl }, ...]
  
  if (!Array.isArray(updates)) {
    return res.status(400).json({ ok: false, error: "updates must be array" });
  }
  
  try {
    for (const u of updates) {
      if (u.order_id && u.current_price !== undefined) {
        updatePerpTradePrice(u.order_id, u.current_price, u.unrealized_pnl || 0);
      }
    }
    res.json({ ok: true, updated: updates.length });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Perps Trade Execution via Bankr (brain + hands mode)
// ─────────────────────────────────────────────────────────────────────────────

// POST /perps/execute - Execute a perp trade via Bankr
app.post("/perps/execute", async (req, res) => {
  const { symbol, direction, size_usdc, reason, dry_run } = req.body || {};
  
  if (!symbol || !direction || !size_usdc) {
    return res.status(400).json({ ok: false, error: "missing required fields: symbol, direction, size_usdc" });
  }
  
  if (!["LONG", "SHORT"].includes(direction.toUpperCase())) {
    return res.status(400).json({ ok: false, error: "direction must be LONG or SHORT" });
  }
  
  if (!PERPS_ENABLED) {
    return res.status(400).json({ ok: false, error: "PERPS_NOT_ENABLED" });
  }
  
  // Size validation against constraints
  if (size_usdc > PERPS_MAX_USDC_PER_TRADE) {
    return res.status(400).json({ 
      ok: false, 
      error: `size_usdc ${size_usdc} exceeds max ${PERPS_MAX_USDC_PER_TRADE}` 
    });
  }
  
  const effectiveDryRun = BANKR_DRY_RUN || Boolean(dry_run);
  
  // Build the perp trade prompt
  const tradePrompt = `Execute a perpetual futures trade on AVANTIS.

TRADE INTENT:
- Symbol: ${symbol}
- Direction: ${direction.toUpperCase()}
- Size: $${size_usdc.toFixed(2)} USDC
- Reason: ${reason || 'Dashboard trade request'}

CONSTRAINTS (MUST NOT EXCEED):
- Max Leverage: ${PERPS_MAX_LEVERAGE}x
- Max USDC per trade: $${PERPS_MAX_USDC_PER_TRADE}
- Daily Loss Cap: $${PERPS_DAILY_LOSS_CAP}

WALLET: ${contextWallet}

Execute this trade on AVANTIS (Base chain). Use appropriate leverage and set reasonable TP/SL based on market conditions.
If the trade cannot be executed safely within these constraints, explain why and do NOT execute.`;

  try {
    console.log(`[Sidecar] /perps/execute called: ${direction} ${symbol} $${size_usdc} (dryRun: ${effectiveDryRun})`);
    
    const prompt = buildPrompt(tradePrompt, {
      dryRun: effectiveDryRun,
      maxUsdcPerPrompt: PERPS_MAX_USDC_PER_TRADE,
      dailySpendCap: PERPS_DAILY_LOSS_CAP,
      approxSpent: approxSpendToday,
      mode: "perp_trade",
    });
    
    const result = await bankrClient.promptAndWait({ prompt });
    
    // Log activity
    logActivity("perp_trade_executed", {
      symbol,
      direction: direction.toUpperCase(),
      size_usdc,
      dryRun: effectiveDryRun,
      jobId: result?.jobId ?? null,
      hasTransactions: (result?.transactions?.length || 0) > 0,
    });
    
    // Track spend (only if not dry run)
    if (!effectiveDryRun) {
      approxSpendToday += size_usdc;
    }
    
    return res.json({
      ok: true,
      symbol,
      direction: direction.toUpperCase(),
      size_usdc,
      dryRun: effectiveDryRun,
      summary: result?.response ?? null,
      success: result?.success ?? true,
      jobId: result?.jobId ?? null,
      transactions: result?.transactions ?? [],
    });
  } catch (err) {
    console.error("[Sidecar] /perps/execute error:", err);
    logActivity("perp_trade_error", { symbol, direction, error: err.message });
    return res.status(500).json({ ok: false, error: err.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Perps Signal Loop & Exit Manager Process Control
// ─────────────────────────────────────────────────────────────────────────────

let perpsSignalProcess = null;
let perpsSignalStartTime = null;
let perpsExitMgrProcess = null;
let perpsExitMgrStartTime = null;

function isPerpsSignalRunning() {
  return !!perpsSignalProcess && !perpsSignalProcess.killed && perpsSignalProcess.exitCode === null;
}

function isPerpsExitMgrRunning() {
  return !!perpsExitMgrProcess && !perpsExitMgrProcess.killed && perpsExitMgrProcess.exitCode === null;
}

// GET /perps/signal-loop/status
app.get("/perps/signal-loop/status", (req, res) => {
  res.json({
    ok: true,
    running: isPerpsSignalRunning(),
    startTime: perpsSignalStartTime,
    pid: perpsSignalProcess?.pid || null,
  });
});

// POST /perps/signal-loop/start
app.post("/perps/signal-loop/start", (req, res) => {
  if (isPerpsSignalRunning()) {
    return res.json({ ok: true, alreadyRunning: true });
  }
  
  const pythonCmd = process.env.PYTHON_CMD || "python";
  const args = req.body?.live ? ["--live"] : [];
  
  perpsSignalProcess = spawn(pythonCmd, ["-m", "perps.signal_loop", ...args], {
    cwd: BOT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32",
  });
  
  perpsSignalStartTime = new Date().toISOString();
  logActivity("perps_signal_loop_started", { pid: perpsSignalProcess.pid });
  
  perpsSignalProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) console.log("[PERPS-SIGNAL]", text);
  });
  
  perpsSignalProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) console.error("[PERPS-SIGNAL ERR]", text);
  });
  
  perpsSignalProcess.on("exit", (code, signal) => {
    logActivity("perps_signal_loop_stopped", { code, signal });
    perpsSignalProcess = null;
    perpsSignalStartTime = null;
  });
  
  res.json({ ok: true, pid: perpsSignalProcess.pid });
});

// POST /perps/signal-loop/stop
app.post("/perps/signal-loop/stop", (req, res) => {
  if (!isPerpsSignalRunning()) {
    return res.json({ ok: true, alreadyStopped: true });
  }
  
  const pid = perpsSignalProcess.pid;
  
  if (process.platform === "win32") {
    spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { shell: true });
  } else {
    perpsSignalProcess.kill("SIGTERM");
  }
  
  logActivity("perps_signal_loop_stopping", { pid });
  res.json({ ok: true, stoppedPid: pid });
});

// GET /perps/exit-manager/status
app.get("/perps/exit-manager/status", (req, res) => {
  res.json({
    ok: true,
    running: isPerpsExitMgrRunning(),
    startTime: perpsExitMgrStartTime,
    pid: perpsExitMgrProcess?.pid || null,
  });
});

// POST /perps/exit-manager/start
app.post("/perps/exit-manager/start", (req, res) => {
  if (isPerpsExitMgrRunning()) {
    return res.json({ ok: true, alreadyRunning: true });
  }
  
  const pythonCmd = process.env.PYTHON_CMD || "python";
  const args = req.body?.live ? ["--live"] : [];
  
  perpsExitMgrProcess = spawn(pythonCmd, ["-m", "perps.perps_exit_manager", ...args], {
    cwd: BOT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32",
  });
  
  perpsExitMgrStartTime = new Date().toISOString();
  logActivity("perps_exit_manager_started", { pid: perpsExitMgrProcess.pid });
  
  perpsExitMgrProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) console.log("[PERPS-EXIT-MGR]", text);
  });
  
  perpsExitMgrProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) console.error("[PERPS-EXIT-MGR ERR]", text);
  });
  
  perpsExitMgrProcess.on("exit", (code, signal) => {
    logActivity("perps_exit_manager_stopped", { code, signal });
    perpsExitMgrProcess = null;
    perpsExitMgrStartTime = null;
  });
  
  res.json({ ok: true, pid: perpsExitMgrProcess.pid });
});

// POST /perps/exit-manager/stop
app.post("/perps/exit-manager/stop", (req, res) => {
  if (!isPerpsExitMgrRunning()) {
    return res.json({ ok: true, alreadyStopped: true });
  }
  
  const pid = perpsExitMgrProcess.pid;
  
  if (process.platform === "win32") {
    spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { shell: true });
  } else {
    perpsExitMgrProcess.kill("SIGTERM");
  }
  
  logActivity("perps_exit_manager_stopping", { pid });
  res.json({ ok: true, stoppedPid: pid });
});

// GET /perps/exit-manager/settings
app.get("/perps/exit-manager/settings", (req, res) => {
  res.json({
    ok: true,
    settings: {
      takeProfitPct: Number(process.env.PERPS_TAKE_PROFIT_PCT || "5.0"),
      stopLossPct: Number(process.env.PERPS_STOP_LOSS_PCT || "-3.0"),
      maxHoldHours: Number(process.env.PERPS_MAX_HOLD_HOURS || "24"),
      autoFlattenHour: Number(process.env.PERPS_AUTO_FLATTEN_HOUR || "-1"),
      dryRun: (process.env.PERPS_EXIT_DRY_RUN || "true").toLowerCase() === "true",
    },
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Sentinel Process Control (Local Scout → Bankr Sniper)
// ─────────────────────────────────────────────────────────────────────────────

let sentinelProcess = null;
let sentinelStartTime = null;

function isSentinelRunning() {
  return !!sentinelProcess && !sentinelProcess.killed && sentinelProcess.exitCode === null;
}

// GET /sentinel/status
app.get("/sentinel/status", (req, res) => {
  res.json({
    ok: true,
    running: isSentinelRunning(),
    startTime: sentinelStartTime,
    pid: sentinelProcess?.pid || null,
    settings: {
      loopInterval: Number(process.env.SENTINEL_LOOP_INTERVAL || "15"),
      dryRun: (process.env.SENTINEL_DRY_RUN || "true").toLowerCase() === "true",
      globalDailyLossCap: Number(process.env.SENTINEL_GLOBAL_DAILY_LOSS_CAP || "500"),
    },
  });
});

// GET /sentinel/prices - Get current BTC/ETH prices from CryptoCompare
app.get("/sentinel/prices", async (req, res) => {
  try {
    const fetch = (await import('node-fetch')).default;
    const ccUrl = "https://min-api.cryptocompare.com/data/pricemultifull?fsyms=BTC,ETH&tsyms=USD";
    const response = await fetch(ccUrl, { timeout: 5000 });
    const data = await response.json();
    
    const btc = data?.RAW?.BTC?.USD || {};
    const eth = data?.RAW?.ETH?.USD || {};
    
    // Calculate position in range
    const btcRange = btc.HIGH24HOUR - btc.LOW24HOUR;
    const ethRange = eth.HIGH24HOUR - eth.LOW24HOUR;
    
    // Zone configs (match sentinel_config.py)
    const btcZones = { top: 0.97, bottom: 0.03 };
    const ethZones = { top: 0.965, bottom: 0.035 };
    
    res.json({
      ok: true,
      prices: {
        BTC: {
          price: btc.PRICE || 0,
          high_24h: btc.HIGH24HOUR || 0,
          low_24h: btc.LOW24HOUR || 0,
          pos_in_range: btcRange > 0 ? (btc.PRICE - btc.LOW24HOUR) / btcRange : 0.5,
          change_24h: btc.CHANGEPCT24HOUR || 0,
          zones: btcZones,
        },
        ETH: {
          price: eth.PRICE || 0,
          high_24h: eth.HIGH24HOUR || 0,
          low_24h: eth.LOW24HOUR || 0,
          pos_in_range: ethRange > 0 ? (eth.PRICE - eth.LOW24HOUR) / ethRange : 0.5,
          change_24h: eth.CHANGEPCT24HOUR || 0,
          zones: ethZones,
        },
      },
      timestamp: new Date().toISOString(),
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /sentinel/start
app.post("/sentinel/start", (req, res) => {
  if (isSentinelRunning()) {
    return res.json({ ok: true, alreadyRunning: true });
  }
  
  if (!PERPS_ENABLED) {
    return res.status(400).json({ ok: false, error: "PERPS_NOT_ENABLED" });
  }
  
  const pythonCmd = process.env.PYTHON_CMD || "python";
  const args = [];
  
  // Add symbols if specified
  if (req.body?.symbols) {
    args.push("--symbols", req.body.symbols);
  }
  
  // Add interval if specified
  if (req.body?.interval) {
    args.push("--interval", String(req.body.interval));
  }
  
  // Live mode
  if (req.body?.live) {
    args.push("--live");
  }
  
  sentinelProcess = spawn(pythonCmd, ["-m", "perps.sentinel", ...args], {
    cwd: BOT_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32",
  });
  
  sentinelStartTime = new Date().toISOString();
  logActivity("sentinel_started", { pid: sentinelProcess.pid, args });
  
  sentinelProcess.stdout.on("data", (data) => {
    const text = data.toString().trim();
    if (text) {
      console.log("[SENTINEL]", text);
      // Log signal fires to activity feed
      if (text.includes("Firing")) {
        logActivity("sentinel_signal", { message: text.slice(0, 200) });
      }
    }
  });
  
  sentinelProcess.stderr.on("data", (data) => {
    const text = data.toString().trim();
    if (text) console.error("[SENTINEL ERR]", text);
  });
  
  sentinelProcess.on("exit", (code, signal) => {
    logActivity("sentinel_stopped", { code, signal });
    sentinelProcess = null;
    sentinelStartTime = null;
  });
  
  res.json({ ok: true, pid: sentinelProcess.pid });
});

// POST /sentinel/stop
app.post("/sentinel/stop", (req, res) => {
  if (!isSentinelRunning()) {
    return res.json({ ok: true, alreadyStopped: true });
  }
  
  const pid = sentinelProcess.pid;
  
  if (process.platform === "win32") {
    spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { shell: true });
  } else {
    sentinelProcess.kill("SIGTERM");
  }
  
  logActivity("sentinel_stopping", { pid });
  res.json({ ok: true, stoppedPid: pid });
});

// GET /sentinel/signals - Get recent sentinel signals
app.get("/sentinel/signals", (req, res) => {
  const limit = Math.min(Number(req.query.limit || 50), 200);
  const signals = getSentinelSignals(limit);
  const stats = getSentinelStats();
  res.json({ ok: true, signals, stats });
});

// POST /sentinel/signal - Log a sentinel signal (called by Python sentinel)
app.post("/sentinel/signal", (req, res) => {
  try {
    const {
      symbol, direction, pos_in_range, price, high_24h, low_24h, range_pct,
      bankr_action, bankr_reason, size_usdc, leverage, dry_run, result_status
    } = req.body;
    
    insertSentinelSignal({
      symbol: symbol || "",
      direction: direction || "",
      pos_in_range: pos_in_range || 0,
      price: price || 0,
      high_24h: high_24h || 0,
      low_24h: low_24h || 0,
      range_pct: range_pct || 0,
      bankr_action: bankr_action || "UNKNOWN",
      bankr_reason: bankr_reason || "",
      size_usdc: size_usdc || 0,
      leverage: leverage || 0,
      dry_run: dry_run ? 1 : 0,
      result_status: result_status || "",
    });
    
    logActivity("sentinel_signal_logged", { symbol, direction, bankr_action });
    res.json({ ok: true });
  } catch (err) {
    console.error("[sentinel/signal] Error:", err.message);
    res.status(500).json({ ok: false, error: err.message });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// GET /fleet-status - Show status of all wallet instances (for multi-wallet mode)
// ─────────────────────────────────────────────────────────────────────────────
app.get("/fleet-status", (req, res) => {
  // This endpoint is a placeholder for fleet coordination.
  // In a full implementation, each wallet instance would register itself here.
  // For now, we return basic info about the current sidecar instance.
  res.json({
    ok: true,
    instance: {
      port: PORT,
      botRunning: isBotRunning(),
      botPid: botProcess?.pid || null,
      botStartTime: botStartTime,
      walletId: process.env.WALLET_ID || "default",
    },
    timestamp: new Date().toISOString(),
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// BTC 15-MINUTE LOOP ENDPOINTS
// ═══════════════════════════════════════════════════════════════════════════════

// GET /btc15/states - Get all BTC15 bracket states
app.get("/btc15/states", (req, res) => {
  try {
    const states = loadBTC15States();
    res.json({ ok: true, states });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /btc15/state - Save a BTC15 bracket state
app.post("/btc15/state", (req, res) => {
  try {
    const state = req.body;
    if (!state.slug) {
      return res.status(400).json({ ok: false, error: "Missing slug" });
    }
    saveBTC15State(state);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// DELETE /btc15/state/:slug - Delete a BTC15 bracket state
app.delete("/btc15/state/:slug", (req, res) => {
  try {
    deleteBTC15State(req.params.slug);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /btc15/activity - Log a BTC15 activity
app.post("/btc15/activity", (req, res) => {
  try {
    const activity = req.body;
    if (!activity.slug || !activity.action) {
      return res.status(400).json({ ok: false, error: "Missing slug or action" });
    }
    logBTC15Activity(activity);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// GET /btc15/activity - Get recent BTC15 activity
app.get("/btc15/activity", (req, res) => {
  try {
    const limit = parseInt(req.query.limit) || 50;
    const rows = db.prepare(`
      SELECT * FROM btc15_activity
      ORDER BY timestamp DESC
      LIMIT ?
    `).all(limit);
    res.json({ ok: true, activity: rows });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// GET /btc15/stats - Get BTC15 strategy stats
app.get("/btc15/stats", (req, res) => {
  try {
    const today = new Date().toISOString().split('T')[0];
    
    // Count today's commands
    const todayCount = db.prepare(`
      SELECT COUNT(*) as count FROM btc15_activity
      WHERE date(timestamp) = ?
    `).get(today);
    
    // Count total commands
    const totalCount = db.prepare(`
      SELECT COUNT(*) as count FROM btc15_activity
    `).get();
    
    // Get action breakdown for today
    const actionBreakdown = db.prepare(`
      SELECT action, COUNT(*) as count FROM btc15_activity
      WHERE date(timestamp) = ?
      GROUP BY action
    `).all(today);
    
    // Get open brackets
    const openBrackets = loadBTC15States().filter(s => s.unhedged_side);
    
    // Get trade stats from btc15_trades table
    const tradeStats = getBTC15TodayStats();
    
    res.json({
      ok: true,
      stats: {
        enabled: process.env.BTC15_ENABLED !== 'false',
        max_bracket_usdc: parseFloat(process.env.BTC15_MAX_BRACKET_USDC) || 40,
        today_commands: todayCount?.count || 0,
        total_commands: totalCount?.count || 0,
        action_breakdown: actionBreakdown,
        open_brackets: openBrackets.length,
        brackets: openBrackets,
      },
      today: tradeStats.today,
      lifetime: tradeStats.lifetime,
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /btc15/trade-open - Record a new BTC15 bracket entry
app.post("/btc15/trade-open", (req, res) => {
  try {
    const { slug, market_label, entry_side, entry_price, size_shares, opened_at, mode } = req.body;
    const total_cost = (size_shares || 0) * (entry_price || 0);
    const id = insertBTC15OpenTrade({
      slug: slug || '',
      market_label: market_label || '',
      opened_at: opened_at || new Date().toISOString(),
      entry_side: entry_side || '',
      entry_price: entry_price || 0,
      size_shares: size_shares || 0,
      total_cost,
      mode: mode || 'DRY_RUN',
    });
    res.json({ ok: true, id });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /btc15/trade-hedge - Record hedge for existing bracket
app.post("/btc15/trade-hedge", (req, res) => {
  try {
    const { id, hedge_side, hedge_price, hedged_at, hedge_cost } = req.body;
    
    // Get existing trade to calculate total cost
    const trade = db.prepare(`SELECT * FROM btc15_trades WHERE id = ?`).get(id);
    if (!trade) {
      return res.status(404).json({ ok: false, error: 'Trade not found' });
    }
    
    const total_cost = (trade.total_cost || 0) + (hedge_cost || 0);
    
    updateBTC15OnHedge(id, {
      hedged_at: hedged_at || new Date().toISOString(),
      hedge_side: hedge_side || '',
      hedge_price: hedge_price || 0,
      total_cost,
    });
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /btc15/trade-resolve - Resolve a completed bracket
app.post("/btc15/trade-resolve", (req, res) => {
  try {
    const { id, payout, resolved_at } = req.body;
    
    const trade = db.prepare(`SELECT * FROM btc15_trades WHERE id = ?`).get(id);
    if (!trade) {
      return res.status(404).json({ ok: false, error: 'Trade not found' });
    }
    
    const realized_pnl = (payout || 0) - (trade.total_cost || 0);
    
    resolveBTC15Trade(id, {
      resolved_at: resolved_at || new Date().toISOString(),
      payout: payout || 0,
      realized_pnl,
    });
    res.json({ ok: true, realized_pnl });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// POST /btc15/trade-flatten - Flatten an unhedged bracket
app.post("/btc15/trade-flatten", (req, res) => {
  try {
    const { id, sale_proceeds, resolved_at } = req.body;
    
    const trade = db.prepare(`SELECT * FROM btc15_trades WHERE id = ?`).get(id);
    if (!trade) {
      return res.status(404).json({ ok: false, error: 'Trade not found' });
    }
    
    const realized_pnl = (sale_proceeds || 0) - (trade.total_cost || 0);
    
    flattenBTC15Trade(id, {
      resolved_at: resolved_at || new Date().toISOString(),
      realized_pnl,
    });
    res.json({ ok: true, realized_pnl });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// GET /btc15/trades - Get BTC15 trades
app.get("/btc15/trades", (req, res) => {
  try {
    const status = req.query.status;
    let rows;
    if (status === 'open') {
      rows = getBTC15OpenTrades();
    } else {
      const limit = parseInt(req.query.limit) || 100;
      rows = db.prepare(`
        SELECT * FROM btc15_trades ORDER BY opened_at DESC LIMIT ?
      `).all(limit);
    }
    res.json({ ok: true, trades: rows });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.listen(PORT, () => {
  console.log(`Bankr sidecar listening on http://localhost:${PORT}`);
});
