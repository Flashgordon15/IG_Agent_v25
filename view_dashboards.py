import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

def render_option_2():
    plt.style.use('dark_background')
    fig = plt.figure(figsize=(12, 7.5), facecolor='#121214')
    gs = gridspec.GridSpec(3, 3, figure=fig, height_ratios=[0.8, 4.2, 1.2], width_ratios=[1.3, 1.1, 1.4])

    # Header
    ax_header = fig.add_subplot(gs[0, :], facecolor='#1e2025')
    ax_header.text(0.01, 0.5, "■ SYSTEM INTERFACE v29.0", color='#10b981', fontsize=13, weight='bold', va='center')
    ax_header.text(0.24, 0.5, "SUBSYSTEM INTEGRITY: SECURE", color='#ffffff', fontsize=9, va='center', alpha=0.6)
    ax_header.text(0.52, 0.5, "LIVE SOAK WINDOW: ACTIVE", color='#10b981', fontsize=9, va='center')
    ax_header.text(0.74, 0.5, "EXPECTANCY BAR: 72% FLOOR", color='#8b5cf6', fontsize=9, weight='bold', va='center')
    ax_header.axis('off')

    # Column 1: Portfolio Envelope Metrics
    ax_balance = fig.add_subplot(gs[1, 0], facecolor='#1e2025')
    ax_balance.set_title("PORTFOLIO ENVELOPE", color='#ffffff', fontsize=11, weight='bold', pad=12)
    balance_metrics = [
        ("Target Daily Milestone", "£1,000.00", "#10b981"),
        ("Concurrent Risk Cap", "£1,200.00", "#ffffff"),
        ("Active Open Exposure", "£80.00", "#8b5cf6"),
        ("Daily Loss Breaker", "£500.00", "#ffffff"),
        ("Total Realized Today", "+£142.10", "#10b981"),
    ]
    for i, (label, val, color) in enumerate(balance_metrics):
        y_pos = 0.82 - i*0.17
        rect = plt.Rectangle((0.02, y_pos-0.05), 0.96, 0.11, facecolor='#23252d', transform=ax_balance.transAxes, edgecolor='#2d3139')
        ax_balance.add_patch(rect)
        ax_balance.text(0.06, y_pos, label, color='#ffffff', fontsize=9, va='center', transform=ax_balance.transAxes, alpha=0.6)
        ax_balance.text(0.94, y_pos, val, color=color, fontsize=10, weight='bold', ha='right', va='center', transform=ax_balance.transAxes)
    ax_balance.axis('off')

    # Column 2: Quant Engine Chips
    ax_metrics = fig.add_subplot(gs[1, 1], facecolor='#1e2025')
    ax_metrics.set_title("QUANT CHIPS", color='#ffffff', fontsize=11, weight='bold', pad=12)
    ax_metrics.text(0.05, 0.88, "SPOT GOLD PROFILE", color='#ffffff', fontsize=9.5, weight='bold', transform=ax_metrics.transAxes)
    metrics_rows = [
        ("Raw Points Score", "91% [CALIBRATED]", "#10b981"),
        ("XGBoost ML Blend", "0.79 [HIGH_CONV]", "#8b5cf6"),
        ("Environment Fitness", "61% [STABLE]", "#10b981"),
        ("Live Broker Spread", "0.3 [OPTIMAL]", "#10b981"),
    ]
    for i, (m_label, m_val, m_col) in enumerate(metrics_rows):
        y_p = 0.74 - i*0.15
        rect = plt.Rectangle((0.02, y_p-0.05), 0.96, 0.11, facecolor='#23252d', transform=ax_metrics.transAxes, edgecolor='#2d3139')
        ax_metrics.add_patch(rect)
        ax_metrics.text(0.06, y_p, m_label, color='#ffffff', fontsize=8.5, transform=ax_metrics.transAxes, alpha=0.6, va='center')
        ax_metrics.text(0.94, y_p, m_val.split()[0], color=m_col, fontsize=9.5, weight='bold', transform=ax_metrics.transAxes, ha='right', va='center')

    box_pnl = plt.Rectangle((0.02, 0.04), 0.96, 0.18, facecolor='#2d3139', transform=ax_metrics.transAxes, edgecolor='#3a3f4d')
    ax_metrics.add_patch(box_pnl)
    ax_metrics.text(0.06, 0.14, "GOLD: +$26.60 USD  →  +£20.75 GBP", color='#10b981', fontsize=9, weight='bold', transform=ax_metrics.transAxes)
    ax_metrics.axis('off')

    # Column 3: Core Execution Register
    ax_positions = fig.add_subplot(gs[1, 2], facecolor='#1e2025')
    ax_positions.set_title("CORE REGISTER", color='#ffffff', fontsize=11, weight='bold', pad=12)
    pos_headers = ["Instrument", "Dir", "Risk Size", "Live P&L"]
    for c_idx, head_text in enumerate(pos_headers):
        ax_positions.text(0.05 + c_idx*0.25, 0.90, head_text, color='#ffffff', fontsize=9, weight='bold', alpha=0.5, transform=ax_positions.transAxes)
    mock_positions = [
        ("Spot Gold", "BUY", "£62 [PROBE]", "+£20.75"),
        ("Wall Street", "SELL", "£150 [CORE]", "+£119.62"),
        ("US Tech 100", "WAIT", "£0 [OFF]", "£0.00"),
        ("EUR/USD", "WAIT", "£0 [OFF]", "£0.00"),
    ]
    for r_idx, (epic, s_side, s_size, s_pnl) in enumerate(mock_positions):
        y_pos = 0.74 - r_idx * 0.16
        r_patch = plt.Rectangle((0.02, y_pos-0.03), 0.96, 0.12, facecolor='#2d3139' if r_idx%2==0 else '#1e2025', transform=ax_positions.transAxes)
        ax_positions.add_patch(r_patch)
        ax_positions.text(0.05, y_pos, epic, color='#ffffff', fontsize=9, transform=ax_positions.transAxes)
        ax_positions.text(0.32, y_pos, s_side, color='#10b981' if s_side in ["BUY","SELL"] else '#f59e0b', fontsize=9, weight='bold', transform=ax_positions.transAxes)
        ax_positions.text(0.55, y_pos, s_size.split()[0], color='#ffffff', fontsize=9, transform=ax_positions.transAxes)
        ax_positions.text(0.80, y_pos, s_pnl, color='#10b981' if s_pnl.startswith("+") else '#ffffff', fontsize=9, weight='bold', transform=ax_positions.transAxes)
    ax_positions.axis('off')

    # Bottom Logs
    ax_logs = fig.add_subplot(gs[2, :], facecolor='#121214')
    rect_terminal = plt.Rectangle((0.005, 0.05), 0.99, 0.9, facecolor='#1e2025', transform=ax_logs.transAxes, edgecolor='#8b5cf6', linewidth=1)
    ax_logs.add_patch(rect_terminal)
    ax_logs.text(0.02, 0.70, "■ CIAO CORE PERFORMANCE RUNTIME PROBES:", color='#8b5cf6', fontsize=9.5, weight='bold', transform=ax_logs.transAxes)
    ax_logs.text(0.33, 0.70, "loop_tick -> p50: 12ms, p95: 18ms  |  snapshot_publish -> p50: 5ms, p95: 9ms", color='#ffffff', fontsize=9.5, transform=ax_logs.transAxes)
    ax_logs.text(0.02, 0.35, "■ SYSTEM AUTO_REPAIR VERIFICATION GATEWAY:", color='#10b981', fontsize=9.5, weight='bold', transform=ax_logs.transAxes)
    ax_logs.text(0.33, 0.35, "[SUCCESS] PROMOTED STAGED OPTIMIZATION 'opt_001_clear_freeze' -> SUBPROCESS E2E 30/30 COMPLIANT PASS", color='#10b981', fontsize=9.5, transform=ax_logs.transAxes)
    ax_logs.axis('off')

    plt.suptitle("OPTION 2: THE STEALTH TACTICAL MONOLITH", fontsize=14, weight='bold', color='#ffffff', y=0.98)
    plt.tight_layout()

render_option_2()
plt.show()
