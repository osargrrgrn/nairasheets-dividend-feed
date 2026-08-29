import csv
from pathlib import Path
from .schema import CSV_FIELDS

def merge_events(existing, incoming):
    by_id = {e["event_id"]: e for e in existing}
    for event in incoming:
        by_id[event["event_id"]] = event
    return sorted(
        by_id.values(),
        key=lambda r: (r.get("payment_date", ""), r.get("ticker", ""), r.get("event_id", ""))
    )

def read_csv(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

def write_html(path: Path, rows):
    headers = [
        ("ticker", "Ticker"),
        ("company", "Company"),
        ("dividend_per_share", "Dividend / Share"),
        ("currency", "Currency"),
        ("dividend_type", "Type"),
        ("qualification_date", "Qualification Date"),
        ("payment_date", "Payment Date"),
        ("status", "Status"),
        ("source_url", "Official NGX Source"),
    ]

    trs = []
    for r in rows:
        cells = []
        for key, _ in headers:
            val = str(r.get(key, ""))
            if key == "source_url" and val:
                val = f'<a href="{val}" rel="noopener noreferrer">NGX PDF</a>'
            cells.append(f"<td>{val}</td>")
        trs.append("<tr>" + "".join(cells) + "</tr>")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NairaSheets NGX Dividend Calendar</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:1200px;margin:40px auto;padding:0 16px;color:#18231d}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border-bottom:1px solid #ddd;padding:10px;text-align:left}}
th{{position:sticky;top:0;background:#f6f8f7}}
a{{color:inherit}}
.small{{color:#66736c;font-size:13px}}
</style>
</head>
<body>
<h1>NairaSheets NGX Dividend Calendar</h1>
<p class="small">Dividend data normalized from official Nigerian Exchange corporate disclosures. Always verify important corporate actions against the linked NGX filing.</p>
<table>
<thead><tr>{''.join(f"<th>{label}</th>" for _,label in headers)}</tr></thead>
<tbody>{''.join(trs)}</tbody>
</table>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")
