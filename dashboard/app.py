"""
Polymarket Arbitrage Desk - Streamlit Dashboard
================================================
A pro-style trading desk UI for monitoring and controlling the arbitrage bot.

Run with: streamlit run dashboard/app.py
"""

import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime

# -------------------------
# Config
# -------------------------
SIDECAR_BASE_URL = "http://localhost:4000"

STATUS_URL = f"{SIDECAR_BASE_URL}/status"
POSITIONS_OPEN_URL = f"{SIDECAR_BASE_URL}/positions/open"
POSITIONS_SUMMARY_URL = f"{SIDECAR_BASE_URL}/positions/summary"
ACTIVITY_URL = f"{SIDECAR_BASE_URL}/activity"
START_BOT_URL = f"{SIDECAR_BASE_URL}/start-bot"
STOP_BOT_URL = f"{SIDECAR_BASE_URL}/stop-bot"

REFRESH_SECONDS = 5  # auto-refresh interval


# -------------------------
# Helpers
# -------------------------
def safe_get(url: str, default=None):
    if default is None:
        default = {}
    try:
        r = requests.get(url, timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return default


def safe_post(url: str, payload=None):
    try:
        r = requests.post(url, json=payload or {}, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fmt_usd(x):
    try:
        return f"${x:,.2f}"
    except Exception:
        return "$0.00"


def fmt_pct(x):
    try:
        return f"{x:.2f}%"
    except Exception:
        return "0.00%"


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# -------------------------
# Streamlit page config
# -------------------------
st.set_page_config(
    page_title="Polymarket Arbitrage Desk",
    page_icon="🧠",
    layout="wide",
)

st.markdown(
    """
    <style>
    /* Dark pro theme tweaks */
    .stApp {
        background-color: #0a0a1a;
    }
    .big-pnl {
        font-size: 2.4rem;
        font-weight: 700;
    }
    .big-pnl.green {
        color: #22c55e;
    }
    .big-pnl.red {
        color: #ef4444;
    }
    .pill {
        display: inline-block;
        padding: 0.15rem 0.55rem;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 500;
        background: #111827;
        border: 1px solid #1f2937;
        margin-left: 0.4rem;
    }
    .pill.online {
        border-color: #22c55e;
        color: #22c55e;
    }
    .pill.offline {
        border-color: #ef4444;
        color: #f97316;
    }
    .activity-log {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        font-size: 0.78rem;
        line-height: 1.4rem;
        max-height: 400px;
        overflow-y: auto;
    }
    .activity-item {
        padding: 4px 8px;
        margin: 2px 0;
        border-radius: 4px;
        background: rgba(255,255,255,0.03);
    }
    .activity-bot_started, .activity-prompt_success, .activity-trade_open {
        border-left: 3px solid #22c55e;
    }
    .activity-bot_stopped, .activity-bot_exited {
        border-left: 3px solid #f97316;
    }
    .activity-prompt_error, .activity-bot_stderr {
        border-left: 3px solid #ef4444;
    }
    .activity-bot_stdout {
        border-left: 3px solid #3b82f6;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------------
# Session state for PnL history
# -------------------------
if "pnl_history" not in st.session_state:
    st.session_state["pnl_history"] = []


# -------------------------
# Fetch data
# -------------------------
status = safe_get(STATUS_URL, default={"ok": False})
summary = safe_get(POSITIONS_SUMMARY_URL, default={"ok": False})
positions_raw = safe_get(POSITIONS_OPEN_URL, default={"trades": []})
activity_raw = safe_get(ACTIVITY_URL, default={"activity": []})

# Extract guardrails (handle both nested and flat format)
guardrails = status.get("guardrails", {}) or {}
if not guardrails:
    # Fallback to flat format
    guardrails = {
        "dryRun": status.get("dryRun"),
        "maxUsdcPerPrompt": status.get("maxUsdcPerPrompt"),
        "dailySpendCap": status.get("dailySpendCap"),
        "spentToday": status.get("approxSpendToday", 0),
    }

# Normalize summary fields
realized_pnl = summary.get("realized_pnl", 0) or summary.get("realizedPnl", 0) or 0
unrealized_pnl = summary.get("unrealized_pnl", 0) or summary.get("unrealizedPnl", 0) or 0
open_positions_count = summary.get("open_positions", 0) or summary.get("openPositions", 0) or 0
net_exposure = summary.get("net_exposure", 0) or summary.get("netExposure", 0) or 0

total_pnl = realized_pnl + unrealized_pnl

# Update PnL history (for sparkline)
now = datetime.utcnow()
st.session_state["pnl_history"].append({"ts": now, "pnl": total_pnl})
st.session_state["pnl_history"] = st.session_state["pnl_history"][-200:]


# -------------------------
# Top bar
# -------------------------
col_title, col_bankroll = st.columns([3, 1])

with col_title:
    st.markdown("### 🧠 Polymarket Arbitrage Desk")
    st.caption(
        "Powered by Bankr + your sidecar • Local control • Low-spam, high-signal execution"
    )

with col_bankroll:
    spent = guardrails.get("spentToday", 0) or 0
    daily_cap = guardrails.get("dailySpendCap", 0) or 0
    if daily_cap > 0:
        remaining = max(0, daily_cap - spent)
        st.markdown("##### Daily Budget")
        st.markdown(f"**{fmt_usd(remaining)}** remaining")
        st.progress(min(1.0, spent / daily_cap) if daily_cap > 0 else 0)
    else:
        st.markdown("##### Daily Budget")
        st.markdown("**Unlimited**")

st.markdown("---")

# -------------------------
# Row 1: Control / Live PnL / Key Metrics
# -------------------------
col_control, col_pnl, col_metrics = st.columns([1.1, 1.4, 1.1])

# ---- Control & Guardrails ----
with col_control:
    st.markdown("#### ⚙️ Control & Guardrails")

    bot_running = bool(status.get("botRunning"))
    started_at = status.get("botStartTime") or status.get("botStartedAt")

    # Buttons
    c1, c2 = st.columns(2)
    with c1:
        start_disabled = bot_running
        if st.button("▶ Start Bot", use_container_width=True, disabled=start_disabled):
            res = safe_post(START_BOT_URL)
            if res.get("ok"):
                st.success("Bot start requested.")
            else:
                st.error(f"Start failed: {res.get('error')}")
            time.sleep(0.5)
            st.rerun()
    with c2:
        stop_disabled = not bot_running
        if st.button("⏹ Stop Bot", use_container_width=True, disabled=stop_disabled):
            res = safe_post(STOP_BOT_URL)
            if res.get("ok"):
                st.warning("Bot stop requested.")
            else:
                st.error(f"Stop failed: {res.get('error')}")
            time.sleep(0.5)
            st.rerun()

    # Status pill
    if bot_running:
        st.markdown(
            '<span class="pill online">● Running</span>',
            unsafe_allow_html=True,
        )
        if started_at:
            ts = parse_ts(started_at)
            if ts:
                elapsed = (datetime.utcnow() - ts.replace(tzinfo=None)).total_seconds()
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                st.caption(f"Uptime: {mins}m {secs}s")
    else:
        st.markdown(
            '<span class="pill offline">● Stopped</span>',
            unsafe_allow_html=True,
        )

    st.markdown("##### Guardrails")
    dry_run = guardrails.get("dryRun", False)
    max_usdc = guardrails.get("maxUsdcPerPrompt", 0)
    daily_cap = guardrails.get("dailySpendCap", 0)
    spent_today = guardrails.get("spentToday", 0)

    st.markdown(f"- **DRY_RUN:** {'✅ Enabled' if dry_run else '❌ Disabled'}")
    st.markdown(f"- **MAX_USDC/PROMPT:** {fmt_usd(max_usdc) if max_usdc > 0 else 'Unlimited'}")
    st.markdown(f"- **DAILY_CAP:** {fmt_usd(daily_cap) if daily_cap > 0 else 'Unlimited'}")
    st.markdown(f"- **Spent Today:** {fmt_usd(spent_today or 0)}")

# ---- Live PnL ----
with col_pnl:
    st.markdown("#### 💰 Live P&L")

    pnl_class = "green" if total_pnl >= 0 else "red"
    st.markdown(
        f'<div class="big-pnl {pnl_class}">{fmt_usd(total_pnl)}</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Realized: {fmt_usd(realized_pnl)} • Unrealized: {fmt_usd(unrealized_pnl)}"
    )

    # Tiny PnL sparkline from session history
    hist_df = pd.DataFrame(st.session_state["pnl_history"])
    if not hist_df.empty and len(hist_df) > 1:
        hist_df["ts_str"] = hist_df["ts"].dt.strftime("%H:%M:%S")
        hist_df = hist_df.set_index("ts_str")
        st.line_chart(hist_df["pnl"], height=130)
    else:
        st.info("Waiting for PnL data...")

# ---- Key Metrics ----
with col_metrics:
    st.markdown("#### 📊 Key Metrics")

    m1, m2 = st.columns(2)
    with m1:
        st.metric("Open Positions", open_positions_count)
        st.metric("Net Exposure", fmt_usd(net_exposure))
    with m2:
        st.metric("Realized PnL", fmt_usd(realized_pnl))
        st.metric("Unrealized PnL", fmt_usd(unrealized_pnl))

st.markdown("---")

# -------------------------
# Row 2: Open Positions & Activity
# -------------------------
col_positions, col_activity = st.columns([1.4, 1.1])

# ---- Open Positions ----
with col_positions:
    st.markdown("#### 📂 Open Positions")

    # Normalize positions list
    if isinstance(positions_raw, dict):
        positions = positions_raw.get("trades", []) or positions_raw.get("positions", [])
    else:
        positions = positions_raw or []

    if positions:
        df = pd.DataFrame(positions)

        # Rename columns for display
        column_renames = {
            "command_id": "ID",
            "market_label": "Market",
            "market_slug": "Slug",
            "side": "Side",
            "size_usdc": "Size ($)",
            "avg_price": "Entry",
            "timestamp": "Time",
            "status": "Status",
        }
        df = df.rename(columns={k: v for k, v in column_renames.items() if k in df.columns})

        # Preferred column order
        preferred = ["Time", "Market", "Side", "Size ($)", "Entry", "Status"]
        existing = [c for c in preferred if c in df.columns]
        remaining = [c for c in df.columns if c not in existing]
        df = df[existing + remaining]

        # Format numeric columns
        if "Size ($)" in df.columns:
            df["Size ($)"] = df["Size ($)"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "-")
        if "Entry" in df.columns:
            df["Entry"] = df["Entry"].apply(lambda x: f"{x:.4f}" if pd.notnull(x) else "-")

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No open positions right now. Bot is flat.")

# ---- Activity Feed ----
with col_activity:
    st.markdown("#### 📜 Activity Feed")

    if isinstance(activity_raw, dict):
        events = activity_raw.get("activity", []) or activity_raw.get("events", [])
    else:
        events = activity_raw or []

    if not events:
        st.caption("No recent activity logged.")
    else:
        st.markdown('<div class="activity-log">', unsafe_allow_html=True)
        for e in events[:50]:
            ts = e.get("ts") or e.get("timestamp") or e.get("time") or ""
            evt_type = e.get("type", "unknown")
            
            # Build message from various possible fields
            msg_parts = []
            if e.get("line"):
                msg_parts.append(e["line"][:100])
            elif e.get("message"):
                msg_parts.append(e["message"][:100])
            elif e.get("market_label"):
                msg_parts.append(f"{e.get('side', '')} {e.get('market_label', '')}")
            elif e.get("pid"):
                msg_parts.append(f"PID: {e['pid']}")
            elif e.get("code") is not None:
                msg_parts.append(f"Exit code: {e['code']}")
            elif e.get("error"):
                msg_parts.append(e["error"])
            
            msg = " ".join(msg_parts) if msg_parts else ""
            
            # Format timestamp
            if ts:
                try:
                    ts_display = parse_ts(ts)
                    if ts_display:
                        ts = ts_display.strftime("%H:%M:%S")
                except:
                    pass

            st.markdown(
                f'<div class="activity-item activity-{evt_type}">'
                f'<strong>[{ts}]</strong> <em>{evt_type}</em>: {msg}'
                f'</div>',
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

# -------------------------
# Footer
# -------------------------
st.markdown("---")
st.caption(
    f"🔄 Auto-refreshing every {REFRESH_SECONDS}s • "
    f"Sidecar: {SIDECAR_BASE_URL} • "
    f"Last update: {datetime.now().strftime('%H:%M:%S')}"
)

# Auto-refresh
time.sleep(REFRESH_SECONDS)
st.rerun()
