"""
Visualization pipeline for a sim/live run directory (fills.csv + state.csv +
summary.csv from mm_sim.py / live_mm.py).

Per traded event, renders a 4-panel figure: odds (mid) with our fills
overlaid, the active alpha, signed exposure, and PnL components over time.
Also writes a run-level overview figure and report.json — the "final object"
holding per-game metrics for future analysis and parameter refits.

Usage: viz.py <run_dir> [--out <dir>]   (default out: <run_dir>/viz)
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from espn_clock import clocks_for


def _match_axis(clocks):
    """Football match-minute x-axis anchored at kickoff. Underlying x is
    wall-clock MINUTES SINCE KICKOFF (monotonic, to scale, so the half-time
    break and added time render at their true widths); tick LABELS are match
    minutes, with added time folded into the HT/FT markers and the half-time
    break shaded. Returns (x_of, decorate) or None if no kickoff is known."""
    if not clocks or "ko" not in clocks:
        return None
    ko = clocks["ko"]
    ht, sh, ft = clocks.get("ht"), clocks.get("sh"), clocks.get("ft")
    events = clocks.get("events", [])
    w2x = lambda e: (e - ko) / 60.0
    x_of = lambda lts: (lts - ko) / 60.0

    def decorate(axes):
        ticks, labels = [0.0], ["KO\n0'"]
        for m in ((15, 30) if ht else (15, 30, 45)):
            ticks.append(float(m)); labels.append(f"{m}'")
        if ht:
            ticks.append(w2x(ht)); labels.append(f"HT\n45+{round(w2x(ht) - 45)}'")
        if sh:
            shx = w2x(sh)
            ticks.append(shx); labels.append("2H\n45'")
            for m in ((60, 75) if ft else (60, 75, 90)):
                ticks.append(shx + (m - 45)); labels.append(f"{m}'")
            if ft:
                ticks.append(w2x(ft)); labels.append(f"FT\n90+{round(w2x(ft) - shx - 45)}'")
        for ax in axes:
            if ht and sh:                       # half-time break
                ax.axvspan(w2x(ht), w2x(sh), color = "gray", alpha = 0.13)
            ax.axvline(0, color = "dimgray", lw = 0.6, ls = ":")   # kickoff
        # Major events: goals (blue) and red cards (crimson), line across all
        # panels + a rotated label at the top of the first panel.
        for e in events:
            x, goal = w2x(e["wc"]), e["kind"] == "goal"
            col = "tab:blue" if goal else "crimson"
            for ax in axes:
                ax.axvline(x, color = col, lw = 0.8, ls = "--", alpha = 0.55, zorder = 1)
            tag = ("GOAL " if goal else "RED ") + (e.get("team") or "")
            axes[0].annotate(tag.strip() + f" {e.get('min', '')}".rstrip(),
                             xy = (x, 1.0), xycoords = ("data", "axes fraction"),
                             xytext = (0, -2), textcoords = "offset points",
                             fontsize = 6, rotation = 90, va = "top", ha = "center", color = col)
        axes[-1].set_xticks(ticks)
        axes[-1].set_xticklabels(labels, fontsize = 7)
    return x_of, decorate


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline = "") as f:
        return list(csv.DictReader(f))


def fnum(x, default = np.nan):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def plot_event(event: str, srows: list[dict], frows: list[dict], out_path: Path,
               threshold: float | None = None):
    ts0 = min(fnum(r["lts"]) for r in srows)
    # Football match-minute axis (minutes since kickoff) when the game clock is
    # known; otherwise fall back to wall-clock hours since the run start.
    ma = _match_axis(clocks_for(event.split(":")[0]))
    if ma:
        x_of, decorate = ma
        xlabel = "match minute (added time at HT/FT; half-time break shaded)"
    else:
        x_of, decorate, xlabel = lambda l: (l - ts0) / 3600, None, "hours since start"
    t = np.array([x_of(fnum(r["lts"])) for r in srows])
    mid = np.array([fnum(r["mid"]) for r in srows])
    alpha = np.array([fnum(r["alpha"]) for r in srows])
    expo = np.array([fnum(r["exposure"]) for r in srows])
    realized = np.array([fnum(r["realized_total"]) for r in srows])
    fees = np.array([fnum(r["fees_total"]) for r in srows])

    fig, axes = plt.subplots(4, 1, figsize = (12, 11), sharex = True)
    fig.suptitle(event, fontsize = 13)

    # Per-event PnL when the run logged it (newer runs); else global PnL
    has_event_pnl = "realized_event" in srows[0]
    if has_event_pnl:
        realized = np.array([fnum(r["realized_event"]) for r in srows])
        fees = np.array([fnum(r["fees_event"]) for r in srows])

    ax = axes[0]
    ax.plot(t, mid, lw = 0.8, color = "black", label = "mid")
    for r in frows:
        fx = x_of(fnum(r["lts"]))
        price = fnum(r["yes_space_price"])
        buy = r["side"] in ("yes", "buy")
        ax.scatter([fx], [price], marker = "^" if buy else "v",
                   color = "green" if buy else "red", s = 36, zorder = 5)
    ax.set_ylabel("odds (yes)")
    ax.legend(loc = "upper left", fontsize = 8)

    axes[1].plot(t, alpha, lw = 0.7, color = "purple")
    axes[1].axhline(0, color = "gray", lw = 0.5)
    # Skew threshold: above +thr the strategy quotes long-only, below -thr
    # short-only; in between (deadband) it quotes both sides symmetrically.
    if threshold:
        for s in (1, -1):
            axes[1].axhline(s * threshold, color = "darkorange", lw = 0.9,
                            ls = "--", label = "skew threshold" if s == 1 else None)
        axes[1].legend(loc = "upper left", fontsize = 8)
    axes[1].set_ylabel("alpha")

    axes[2].fill_between(t, expo, step = "pre", alpha = 0.5, color = "steelblue")
    axes[2].axhline(0, color = "gray", lw = 0.5)
    axes[2].set_ylabel("exposure (cts)")

    scope = "event" if has_event_pnl else "global"
    axes[3].plot(t, realized, lw = 1.0, color = "green", label = f"realized ({scope})")
    axes[3].plot(t, fees, lw = 1.0, color = "red", label = f"fees ({scope})")
    axes[3].plot(t, realized - fees, lw = 1.2, color = "black", label = "realized-fees")
    axes[3].set_ylabel("PnL $")
    axes[3].set_xlabel(xlabel)
    axes[3].legend(loc = "upper left", fontsize = 8)

    if decorate:
        decorate(axes)
    fig.tight_layout()
    fig.savefig(out_path, dpi = 110)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description = "Visualize a sim/live run directory")
    parser.add_argument("run_dir")
    parser.add_argument("--out", default = None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out) if args.out else run_dir / "viz"
    out_dir.mkdir(parents = True, exist_ok = True)

    state = load_csv(run_dir / "state.csv")
    fills = load_csv(run_dir / "fills.csv")
    summary_rows = load_csv(run_dir / "summary.csv")
    threshold = fnum(summary_rows[0]["skew_threshold"], None) if summary_rows else None

    state_by_event = defaultdict(list)
    for r in state:
        state_by_event[r["event"]].append(r)
    fills_by_event = defaultdict(list)
    for r in fills:
        fills_by_event[r["event_ticker"]].append(r)

    report = {
        "run_dir": str(run_dir),
        "summary": summary_rows[0] if summary_rows else {},
        "events": {},
    }
    for event, srows in sorted(state_by_event.items()):
        # SingleMM state keys are "EVENT:TICKER"; fills key on plain event
        frows = fills_by_event.get(event, fills_by_event.get(event.split(":")[0], []))
        if event.count(":"):
            frows = [r for r in frows if r["ticker"] == event.split(":")[1]]
        png = out_dir / f"{event.replace(':', '_')}.png"
        try:
            plot_event(event, srows, frows, png, threshold = threshold)
        except Exception as e:
            print(f"  plot failed for {event}: {e}")
            continue
        qty = sum(fnum(r["qty"], 0) for r in frows)
        mos = [fnum(r.get("markout_30s")) for r in frows if r.get("markout_30s") not in ("", None)]
        last = srows[-1]
        realized_net = (fnum(last.get("realized_event"), 0.0)
                        - fnum(last.get("fees_event"), 0.0)) if "realized_event" in last else None
        report["events"][event] = {
            "n_fills": len(frows),
            "contracts": round(qty, 1),
            "realized_net": round(realized_net, 4) if realized_net is not None else None,
            "markout_30s_mean": round(float(np.nanmean(mos)), 4) if mos else None,
            "max_abs_exposure": max((abs(fnum(r["exposure"], 0)) for r in srows), default = 0),
            "png": png.name,
        }

    # Run-level overview: deployed + realized-fees over time (global columns)
    if state:
        ts0 = min(fnum(r["lts"]) for r in state)
        ma = _match_axis(clocks_for(state[0]["event"].split(":")[0]))
        if ma:
            x_of, decorate = ma
            xlabel = "match minute (added time at HT/FT; half-time break shaded)"
        else:
            x_of, decorate, xlabel = lambda l: (l - ts0) / 3600, None, "hours since start"
        order = np.argsort([fnum(r["lts"]) for r in state])
        t = np.array([x_of(fnum(state[i]["lts"])) for i in order])
        dep = np.array([fnum(state[i]["deployed_total"]) for i in order])
        rlz = np.array([fnum(state[i]["realized_total"]) - fnum(state[i]["fees_total"]) for i in order])
        fig, ax1 = plt.subplots(figsize = (12, 5))
        ax1.plot(t, rlz, color = "black", lw = 1.2, label = "realized - fees ($)")
        ax1.set_ylabel("realized - fees $")
        ax1.set_xlabel(xlabel)
        ax2 = ax1.twinx()
        ax2.fill_between(t, dep, alpha = 0.25, color = "steelblue", label = "deployed $")
        ax2.set_ylabel("deployed $")
        if decorate:
            decorate([ax1])
        fig.legend(loc = "upper left", fontsize = 9)
        fig.tight_layout()
        fig.savefig(out_dir / "overview.png", dpi = 110)
        plt.close(fig)

    with open(out_dir / "report.json", "w") as f:
        json.dump(report, f, indent = 1)
    print(f"Wrote {len(report['events'])} event figures + overview.png + report.json to {out_dir}")


if __name__ == "__main__":
    main()
