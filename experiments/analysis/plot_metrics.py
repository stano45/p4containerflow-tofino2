#!/usr/bin/env python3
"""
plot_metrics.py — Generate charts from experiment CSV data.

Reads the CSV produced by the Go collector and the migration timing files,
then generates publication-ready matplotlib charts.

Plots generated:
  - server_metrics.png    — connected clients, bytes sent, uptime
  - ws_rtt.png            — WebSocket RTT (avg, P50, P95, P99) over time
  - ping_rtt.png          — ICMP ping RTT per host
  - container_stats.png   — container CPU usage
  - migration_timing.png  — phase durations (mean+stddev for multi-migration)

Usage:
    python3 plot_metrics.py --csv results/metrics.csv --migration-flag results/run_dir --output-dir results/run_dir
"""

import argparse
import glob
import os
import re
import sys

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
except Exception as e:
    print("Plot dependencies unavailable:", e, file=sys.stderr)
    print("Install with: pip install matplotlib pandas numpy", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Plot experiment metrics")
parser.add_argument("--csv", default="results/metrics.csv", help="Path to collector CSV file")
parser.add_argument("--migration-flag", default="/tmp/migration_event",
                    help="Path to migration event file or directory (dir: load all migration_timing*.txt)")
parser.add_argument("--output-dir", default="results", help="Directory for output charts")
parser.add_argument("--show", action="store_true", help="Show charts interactively")

# ---------------------------------------------------------------------------
# Color palette for migration lines
# ---------------------------------------------------------------------------
MIGRATION_COLORS = plt.cm.tab10.colors  # 10 distinct colors

PHASE_COLORS = {
    "Checkpoint": "#4CAF50",
    "Transfer": "#03A9F4",
    "Restore": "#FF9800",
    "Switch Update": "#9C27B0",
}

# ---------------------------------------------------------------------------
# Migration event loading
# ---------------------------------------------------------------------------

def load_migration_event(path: str):
    if not os.path.exists(path) or not os.path.isfile(path):
        return None
    data = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data if data else None


def _migration_file_sort_key(p: str):
    basename = os.path.basename(p)
    m = re.match(r"migration_timing_(\d+)\.txt", basename)
    if m:
        return (0, int(m.group(1)))
    if basename == "migration_timing.txt":
        return (1, 0)
    return (2, 0)


def load_all_migration_events(path: str):
    events = []
    if os.path.isfile(path):
        ev = load_migration_event(path)
        if ev:
            events.append(ev)
        return events
    if os.path.isdir(path):
        files = glob.glob(os.path.join(path, "migration_timing_*.txt"))
        files.sort(key=_migration_file_sort_key)
        for f in files:
            ev = load_migration_event(f)
            if ev:
                events.append(ev)
        if not events:
            single = load_migration_event(os.path.join(path, "migration_timing.txt"))
            if single:
                events.append(single)
        return events
    return []


def migration_time_ms(event):
    if event and "migration_start_ns" in event:
        return int(event["migration_start_ns"]) // 1_000_000
    return None


def _migration_times_sec(df, migration_ms_list):
    """Convert migration timestamps to seconds relative to CSV start."""
    if not migration_ms_list or "timestamp_unix_milli" not in df.columns:
        return []
    t0 = float(df["timestamp_unix_milli"].iloc[0])
    return [(m - t0) / 1000.0 for m in migration_ms_list]


def _draw_migration_lines(ax, m_times, label_prefix="Migration"):
    """Draw color-coded vertical dashed lines for each migration."""
    for i, m_sec in enumerate(m_times):
        color = MIGRATION_COLORS[i % len(MIGRATION_COLORS)]
        ax.axvline(x=m_sec, color=color, linestyle="--", alpha=0.7,
                   label=f"{label_prefix} {i + 1}")

# ---------------------------------------------------------------------------
# Plot: Server metrics
# ---------------------------------------------------------------------------

def plot_server_metrics(df, migration_ms_list, output_dir, show):
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    fig.suptitle("Server Metrics Over Time", fontsize=14)

    t = df["t_sec"]

    # Connected clients
    col = next((c for c in ["connected_clients", "active_peers"] if c in df.columns), None)
    ax = axes[0]
    if col:
        ax.plot(t, pd.to_numeric(df[col], errors="coerce"), "b-", linewidth=1)
    ax.set_ylabel("Connected Clients")
    ax.grid(True, alpha=0.3)

    # Bytes sent
    col = next((c for c in ["bytes_sent", "server_bytes_sent"] if c in df.columns), None)
    ax = axes[1]
    if col:
        ax.plot(t, pd.to_numeric(df[col], errors="coerce"), "g-", linewidth=1)
    ax.set_ylabel("Total Bytes Sent")
    ax.grid(True, alpha=0.3)

    # Uptime
    col = next((c for c in ["uptime_s", "server_uptime_s"] if c in df.columns), None)
    ax = axes[2]
    if col:
        ax.plot(t, pd.to_numeric(df[col], errors="coerce"), "r-", linewidth=1)
    ax.set_ylabel("Server Uptime (s)")
    ax.set_xlabel("Experiment Time (s)")
    ax.grid(True, alpha=0.3)

    m_times = _migration_times_sec(df, migration_ms_list)
    for ax in axes:
        _draw_migration_lines(ax, m_times)
    if m_times:
        axes[0].legend(loc="upper left", fontsize=8)

    plt.tight_layout()
    _save_or_show(fig, output_dir, "server_metrics.png", show)

# ---------------------------------------------------------------------------
# Plot: WebSocket RTT
# ---------------------------------------------------------------------------

def plot_ws_rtt(df, migration_ms_list, output_dir, show):
    """Plot WebSocket RTT metrics (avg, P50, P95, P99) over time."""
    rtt_cols = {
        "ws_rtt_avg_ms": ("Avg RTT", "b"),
        "ws_rtt_p50_ms": ("P50 RTT", "g"),
        "ws_rtt_p95_ms": ("P95 RTT", "orange"),
        "ws_rtt_p99_ms": ("P99 RTT", "r"),
    }
    present = {k: v for k, v in rtt_cols.items() if k in df.columns}
    if not present:
        return

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    fig.suptitle("WebSocket RTT Over Time", fontsize=14)

    t = df["t_sec"]

    # RTT
    ax = axes[0]
    for col, (label, color) in present.items():
        vals = pd.to_numeric(df[col], errors="coerce")
        vals = vals.where(vals > 0)  # filter zeros
        ax.plot(t, vals, color=color, linewidth=1, label=label, alpha=0.8)
    ax.set_ylabel("RTT (ms)")
    ax.set_yscale("log")
    ax.grid(True, alpha=0.3)

    m_times = _migration_times_sec(df, migration_ms_list)
    _draw_migration_lines(ax, m_times)
    ax.legend(loc="upper right", fontsize=8)

    # Jitter
    ax = axes[1]
    if "ws_jitter_ms" in df.columns:
        jitter = pd.to_numeric(df["ws_jitter_ms"], errors="coerce")
        jitter = jitter.where(jitter > 0)
        ax.plot(t, jitter, "purple", linewidth=1, label="Jitter", alpha=0.8)
    if "connection_drops" in df.columns:
        drops = pd.to_numeric(df["connection_drops"], errors="coerce")
        drops_diff = drops.diff().fillna(0)
        if drops_diff.sum() > 0:
            ax2 = ax.twinx()
            ax2.bar(t[drops_diff > 0], drops_diff[drops_diff > 0], width=0.5, color="red", alpha=0.5, label="Drops")
            ax2.set_ylabel("Connection Drops")
            ax2.legend(loc="upper left", fontsize=8)

    ax.set_ylabel("Jitter (ms)")
    ax.set_xlabel("Experiment Time (s)")
    ax.grid(True, alpha=0.3)
    _draw_migration_lines(ax, m_times)
    if "ws_jitter_ms" in df.columns:
        ax.legend(loc="upper right", fontsize=8)

    plt.tight_layout()
    _save_or_show(fig, output_dir, "ws_rtt.png", show)

# ---------------------------------------------------------------------------
# Plot: Ping RTT
# ---------------------------------------------------------------------------

def plot_ping_rtt(df, migration_ms_list, output_dir, show):
    rtt_cols = [c for c in df.columns if c.startswith("ping_rtt_ms_") or c.startswith("ping_ms_")]
    if not rtt_cols:
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    fig.suptitle("ICMP Ping RTT Over Time", fontsize=14)

    t = df["t_sec"]
    for col in rtt_cols:
        host = col.replace("ping_rtt_ms_", "").replace("ping_ms_", "").replace("_", ".")
        vals = pd.to_numeric(df[col], errors="coerce")
        vals = vals.where(vals >= 0)
        ax.plot(t, vals, linewidth=1, label=host)

    m_times = _migration_times_sec(df, migration_ms_list)
    _draw_migration_lines(ax, m_times)

    ax.set_ylabel("RTT (ms)")
    ax.set_xlabel("Experiment Time (s)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    _save_or_show(fig, output_dir, "ping_rtt.png", show)

# ---------------------------------------------------------------------------
# Plot: Container stats
# ---------------------------------------------------------------------------

def plot_container_stats(df, migration_ms_list, output_dir, show):
    cpu_cols = [c for c in df.columns if c.endswith("_cpu") or (c.startswith("cpu_") and len(c) > 4)]
    if not cpu_cols:
        return

    fig, axes = plt.subplots(len(cpu_cols), 1, figsize=(12, 4 * len(cpu_cols)), sharex=True)
    if len(cpu_cols) == 1:
        axes = [axes]

    fig.suptitle("Container Resource Usage", fontsize=14)
    t = df["t_sec"]

    for i, col in enumerate(cpu_cols):
        name = col.replace("container_", "").replace("_cpu", "").replace("cpu_", "")
        ax = axes[i]
        cpu_vals = df[col].astype(str).str.rstrip("%").str.strip()
        cpu_vals = pd.to_numeric(cpu_vals, errors="coerce")
        ax.plot(t, cpu_vals, "b-", linewidth=1, label="CPU %")
        ax.set_ylabel(f"{name}\nCPU %")
        ax.grid(True, alpha=0.3)

        m_times = _migration_times_sec(df, migration_ms_list)
        _draw_migration_lines(ax, m_times)
        ax.legend(loc="upper right", fontsize=8)

    axes[-1].set_xlabel("Experiment Time (s)")

    plt.tight_layout()
    _save_or_show(fig, output_dir, "container_stats.png", show)

# ---------------------------------------------------------------------------
# Plot: Migration timing
# ---------------------------------------------------------------------------

def plot_migration_timing(events, output_dir, show):
    if not events:
        return

    # Phases (no "IP Edit" — same-IP migration doesn't have this step)
    phases = ["Checkpoint", "Transfer", "Restore", "Switch Update"]
    phase_keys = ["checkpoint_ms", "transfer_ms", "restore_ms", "switch_ms"]

    if len(events) == 1:
        event = events[0]
        try:
            time_to_ready = int(event.get("time_to_ready_ms", event["total_ms"]))
            total = int(event["total_ms"])
            durations = [int(event.get(k, 0)) for k in phase_keys]
        except (KeyError, ValueError):
            return

        fig, ax = plt.subplots(figsize=(8, 5))
        colors = [PHASE_COLORS.get(p, "#888") for p in phases]
        bars = ax.barh(phases, durations, color=colors)
        ax.set_xlabel("Duration (ms)")
        title = f"Migration Phases — client downtime: {time_to_ready} ms"
        if total != time_to_ready:
            title += f"  |  full: {total} ms"
        ax.set_title(title)
        ax.grid(True, axis="x", alpha=0.3)
        for bar, val in zip(bars, durations):
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                    f"{val:.0f} ms", va="center", fontsize=10)
    else:
        # Multiple migrations: mean + stddev horizontal bars with percentile annotations
        all_durations = {p: [] for p in phases}
        all_totals = []

        for event in events:
            try:
                for p, k in zip(phases, phase_keys):
                    all_durations[p].append(int(event.get(k, 0)))
                all_totals.append(int(event.get("time_to_ready_ms", event.get("total_ms", 0))))
            except (KeyError, ValueError):
                continue

        if not all_totals:
            return

        means = [np.mean(all_durations[p]) for p in phases]
        stds = [np.std(all_durations[p]) for p in phases]

        fig, ax = plt.subplots(figsize=(10, 6))
        colors = [PHASE_COLORS.get(p, "#888") for p in phases]
        y_pos = np.arange(len(phases))
        bars = ax.barh(y_pos, means, xerr=stds, height=0.6, color=colors,
                       capsize=3, ecolor="gray", alpha=0.9)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(phases)
        ax.set_xlabel("Duration (ms)")

        # Percentile annotations for total migration time
        p50 = np.percentile(all_totals, 50)
        p95 = np.percentile(all_totals, 95)
        p99 = np.percentile(all_totals, 99)
        avg_total = np.mean(all_totals)

        title = (f"Migration Phases — {len(events)} migrations "
                 f"(mean total: {avg_total:.0f} ms)")
        ax.set_title(title)

        # Add mean+std text labels
        for bar, m, s in zip(bars, means, stds):
            ax.text(bar.get_width() + s + 2, bar.get_y() + bar.get_height() / 2,
                    f"{m:.0f} ± {s:.0f} ms", va="center", fontsize=9)

        # Percentile summary
        summary = f"Total downtime: P50={p50:.0f}ms  P95={p95:.0f}ms  P99={p99:.0f}ms"
        ax.text(0.5, -0.12, summary, transform=ax.transAxes, ha="center",
                fontsize=10, style="italic", color="#333")

        ax.grid(True, axis="x", alpha=0.3)

    plt.tight_layout()
    _save_or_show(fig, output_dir, "migration_timing.png", show)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_or_show(fig, output_dir, filename, show):
    if show:
        plt.show()
    else:
        path = os.path.join(output_dir, filename)
        fig.savefig(path, dpi=150, bbox_inches="tight")
        print(f"Saved: {path}")
    plt.close(fig)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    events = load_all_migration_events(args.migration_flag)
    migration_ms_list = [migration_time_ms(e) for e in events]
    migration_ms_list = [m for m in migration_ms_list if m is not None]

    if not os.path.exists(args.csv):
        print(f"CSV file not found: {args.csv}")
        sys.exit(1)

    df = pd.read_csv(args.csv)
    if df.empty:
        print("CSV is empty, nothing to plot.")
        sys.exit(1)

    # Relative time
    if "timestamp_unix_milli" in df.columns:
        df["timestamp_unix_milli"] = pd.to_numeric(df["timestamp_unix_milli"], errors="coerce")
        t0 = float(df["timestamp_unix_milli"].iloc[0])
        df["t_sec"] = (df["timestamp_unix_milli"] - t0) / 1000.0
    elif "elapsed_s" in df.columns:
        df["t_sec"] = pd.to_numeric(df["elapsed_s"], errors="coerce")
    else:
        print("CSV needs 'timestamp_unix_milli' or 'elapsed_s'")
        sys.exit(1)

    print(f"Loaded {len(df)} rows from {args.csv}")
    print(f"Duration: {df['t_sec'].iloc[-1]:.1f}s")
    if migration_ms_list and "timestamp_unix_milli" in df.columns:
        t0 = float(df["timestamp_unix_milli"].iloc[0])
        for i, m_ms in enumerate(migration_ms_list):
            msg = f"Migration {i + 1} at t={((m_ms - t0) / 1000.0):.1f}s"
            if i < len(events) and "time_to_ready_ms" in events[i]:
                msg += f" — downtime: {int(events[i]['time_to_ready_ms'])} ms"
            print(msg)

    plot_server_metrics(df, migration_ms_list, args.output_dir, args.show)
    plot_ws_rtt(df, migration_ms_list, args.output_dir, args.show)
    plot_ping_rtt(df, migration_ms_list, args.output_dir, args.show)
    plot_container_stats(df, migration_ms_list, args.output_dir, args.show)
    plot_migration_timing(events, args.output_dir, args.show)

    print("Done.")


if __name__ == "__main__":
    main()
