"""HTML report builders for SMC backtest summaries and live scan snapshots."""

from __future__ import annotations

from datetime import datetime
from html import escape


def build_smc_report_html(report: dict, title: str = "SMC Backtest Report") -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    symbols = report.get("symbols") or []
    runs = report.get("latest_runs") or []

    symbol_rows = "".join(
        f"""
        <tr>
          <td>{escape(str(item.get('symbol') or ''))}</td>
          <td>{escape(str(item.get('market') or ''))}</td>
          <td>{int(item.get('trade_count') or 0)}</td>
          <td>{_pct(item.get('win_rate'))}</td>
          <td>{_num(item.get('expectancy_r'))}</td>
          <td>{_num(item.get('pnl'))}</td>
          <td>{_num(item.get('avg_holding_bars'))}</td>
        </tr>
        """
        for item in symbols
    ) or '<tr><td colspan="7" class="empty">尚無資料</td></tr>'

    run_rows = "".join(
        f"""
        <tr>
          <td>{escape(str(item.get('symbol') or ''))}</td>
          <td>{escape(str(item.get('period') or ''))}</td>
          <td>{int(item.get('total_trades') or 0)}</td>
          <td>{_pct(item.get('win_rate'))}</td>
          <td>{_num(item.get('profit_factor'))}</td>
          <td>{_num(item.get('expectancy_r'))}</td>
          <td>{escape(str(item.get('created_at') or ''))}</td>
        </tr>
        """
        for item in runs
    ) or '<tr><td colspan="7" class="empty">尚無 run 紀錄</td></tr>'

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #06131b;
      --panel: #0f1f29;
      --panel-2: #142935;
      --line: #28414d;
      --text: #d7e2e8;
      --muted: #8ca4af;
      --good: #3dd598;
      --warn: #f5b942;
      --bad: #ff6b6b;
      --accent: #49c6e5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "SF Pro Display", "Noto Sans TC", system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(73,198,229,0.18), transparent 24%),
        linear-gradient(180deg, #08141d 0%, var(--bg) 100%);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 32px 24px 48px; }}
    h1 {{ margin: 0 0 8px; font-size: 34px; }}
    .sub {{ color: var(--muted); margin-bottom: 24px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .card {{
      background: linear-gradient(180deg, rgba(20,41,53,0.95), rgba(15,31,41,0.95));
      border: 1px solid rgba(73,198,229,0.14);
      border-radius: 14px;
      padding: 14px 16px;
    }}
    .label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    .section {{
      margin-top: 18px;
      background: rgba(15,31,41,0.92);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 16px;
      overflow: hidden;
    }}
    .section h2 {{
      margin: 0;
      font-size: 18px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      text-align: center;
      white-space: nowrap;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    .empty {{ color: var(--muted); }}
    .pos {{ color: var(--good); }}
    .neg {{ color: var(--bad); }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 900px) {{
      .wrap {{ padding: 18px 12px 28px; }}
      table {{ font-size: 12px; }}
      th, td {{ padding: 10px 8px; }}
      .value {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{escape(title)}</h1>
    <div class="sub">Generated at {escape(generated_at)} · runs {int(report.get("run_count") or 0)} · trades {int(report.get("trade_count") or 0)}</div>
    <div class="cards">
      <div class="card"><div class="label">Run 數量</div><div class="value">{int(report.get("run_count") or 0)}</div></div>
      <div class="card"><div class="label">Trade 數量</div><div class="value">{int(report.get("trade_count") or 0)}</div></div>
      <div class="card"><div class="label">追蹤標的</div><div class="value">{len(symbols)}</div></div>
      <div class="card"><div class="label">最近 Run</div><div class="value">{len(runs)}</div></div>
    </div>
    <div class="section">
      <h2>Symbol Summary</h2>
      <table>
        <thead>
          <tr><th>Symbol</th><th>Market</th><th>Trades</th><th>Win Rate</th><th>Expectancy R</th><th>PnL</th><th>Avg Hold</th></tr>
        </thead>
        <tbody>{symbol_rows}</tbody>
      </table>
    </div>
    <div class="section">
      <h2>Latest Runs</h2>
      <table>
        <thead>
          <tr><th>Symbol</th><th>Period</th><th>Trades</th><th>Win Rate</th><th>PF</th><th>Expectancy R</th><th>Created</th></tr>
        </thead>
        <tbody>{run_rows}</tbody>
      </table>
    </div>
  </div>
</body>
</html>"""


def _num(value) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    value = float(value)
    cls = "pos" if value > 0 else ("neg" if value < 0 else "muted")
    return f'<span class="{cls}">{value:.2f}</span>'


def _pct(value) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    return _num(float(value) * 100) + "%"


def build_smc_scan_report_html(scan: dict, title: str = "SMC Scan Report") -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = scan.get("summary") or {}
    results = scan.get("results") or []
    universe = scan.get("universe") or []
    model_counts = summary.get("model_breakdown") or {}
    market_counts = summary.get("market_breakdown") or {}

    model_rows = "".join(
        f"""
        <tr>
          <td>{escape(str(model))}</td>
          <td>{int(count or 0)}</td>
        </tr>
        """
        for model, count in sorted(model_counts.items(), key=lambda item: (-int(item[1] or 0), item[0]))
    ) or '<tr><td colspan="2" class="empty">尚無模型分布</td></tr>'

    market_rows = "".join(
        f"""
        <tr>
          <td>{escape(str(market))}</td>
          <td>{int(count or 0)}</td>
        </tr>
        """
        for market, count in sorted(market_counts.items(), key=lambda item: (-int(item[1] or 0), item[0]))
    ) or '<tr><td colspan="2" class="empty">尚無市場分布</td></tr>'

    signal_rows = "".join(
        f"""
        <tr>
          <td>{escape(str(item.get('symbol') or ''))}</td>
          <td>{escape(str(item.get('name') or ''))}</td>
          <td>{escape(str(item.get('market') or ''))}</td>
          <td>{escape(str(item.get('model') or ''))}</td>
          <td>{escape(str(item.get('direction') or ''))}</td>
          <td>{_num(item.get('score'))}</td>
          <td>{_num(item.get('entry'))}</td>
          <td>{_num(item.get('stop'))}</td>
          <td>{_num(item.get('tp1'))}</td>
          <td>{_num(item.get('rr'))}</td>
          <td>{escape(str(((item.get('dol_target') or {}).get('type')) or '—'))}</td>
          <td>{escape(str(item.get('status') or ''))}</td>
        </tr>
        """
        for item in results
    ) or '<tr><td colspan="12" class="empty">尚無掃描訊號</td></tr>'

    universe_rows = "".join(
        f"""
        <tr>
          <td>{escape(str(item.get('symbol') or ''))}</td>
          <td>{escape(str(item.get('name') or ''))}</td>
        </tr>
        """
        for item in universe
    ) or '<tr><td colspan="2" class="empty">尚無掃描標的</td></tr>'

    avg_score = summary.get("avg_score")
    avg_rr = summary.get("avg_rr")
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #06131b;
      --panel: #0f1f29;
      --panel-2: #142935;
      --line: #28414d;
      --text: #d7e2e8;
      --muted: #8ca4af;
      --good: #3dd598;
      --warn: #f5b942;
      --bad: #ff6b6b;
      --accent: #49c6e5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "SF Pro Display", "Noto Sans TC", system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(73,198,229,0.18), transparent 24%),
        linear-gradient(180deg, #08141d 0%, var(--bg) 100%);
      min-height: 100vh;
    }}
    .wrap {{ max-width: 1440px; margin: 0 auto; padding: 32px 24px 48px; }}
    h1 {{ margin: 0 0 8px; font-size: 34px; }}
    .sub {{ color: var(--muted); margin-bottom: 24px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .card {{
      background: linear-gradient(180deg, rgba(20,41,53,0.95), rgba(15,31,41,0.95));
      border: 1px solid rgba(73,198,229,0.14);
      border-radius: 14px;
      padding: 14px 16px;
    }}
    .label {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    .grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
    }}
    .section {{
      margin-top: 18px;
      background: rgba(15,31,41,0.92);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 16px;
      overflow: hidden;
    }}
    .section h2 {{
      margin: 0;
      font-size: 18px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      text-align: center;
      white-space: nowrap;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    .empty {{ color: var(--muted); }}
    .pos {{ color: var(--good); }}
    .neg {{ color: var(--bad); }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 980px) {{
      .wrap {{ padding: 18px 12px 28px; }}
      .grid-2 {{ grid-template-columns: 1fr; }}
      table {{ font-size: 12px; }}
      th, td {{ padding: 10px 8px; }}
      .value {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{escape(title)}</h1>
    <div class="sub">Generated at {escape(generated_at)} · scope {escape(str(scan.get("scope") or "all"))} · period {escape(str(scan.get("period") or "6mo"))}</div>
    <div class="cards">
      <div class="card"><div class="label">掃描標的</div><div class="value">{int(summary.get("symbol_count") or 0)}</div></div>
      <div class="card"><div class="label">訊號總數</div><div class="value">{int(summary.get("signal_count") or 0)}</div></div>
      <div class="card"><div class="label">合格訊號</div><div class="value">{int(summary.get("qualified_count") or 0)}</div></div>
      <div class="card"><div class="label">平均分數</div><div class="value">{'—' if avg_score is None else f'{float(avg_score):.2f}'}</div></div>
      <div class="card"><div class="label">平均 RR</div><div class="value">{'—' if avg_rr is None else f'{float(avg_rr):.2f}'}</div></div>
    </div>
    <div class="grid-2">
      <div class="section">
        <h2>Model Breakdown</h2>
        <table>
          <thead><tr><th>Model</th><th>Signals</th></tr></thead>
          <tbody>{model_rows}</tbody>
        </table>
      </div>
      <div class="section">
        <h2>Market Breakdown</h2>
        <table>
          <thead><tr><th>Market</th><th>Signals</th></tr></thead>
          <tbody>{market_rows}</tbody>
        </table>
      </div>
    </div>
    <div class="section">
      <h2>Scanned Universe</h2>
      <table>
        <thead>
          <tr><th>Symbol</th><th>Name</th></tr>
        </thead>
        <tbody>{universe_rows}</tbody>
      </table>
    </div>
    <div class="section">
      <h2>Signal Ranking</h2>
      <table>
        <thead>
          <tr><th>Symbol</th><th>Name</th><th>Market</th><th>Model</th><th>Direction</th><th>Score</th><th>Entry</th><th>Stop</th><th>TP1</th><th>RR</th><th>DOL</th><th>Status</th></tr>
        </thead>
        <tbody>{signal_rows}</tbody>
      </table>
    </div>
  </div>
</body>
</html>"""
