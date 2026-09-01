
import csv, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ARCHIVE = ROOT / "disclosure_archive.json"
STATE = ROOT / "collector_state.json"
PENDING = DOCS / "pending_dividends.csv"
PUBLISHED = DOCS / "dividends.csv"
REVIEW = ROOT / "review_queue.json"

BENCHMARKS = {
    "IKEJAHOTEL": ["ikeja hotel", "ikejahotel"],
    "HONYFLOUR": ["honeywell flour", "honyflour"],
    "REDSTAREX": ["red star express", "redstarex"],
    "UPL": ["university press", " upl "],
    "LEARNAFRCA": ["learn africa", "learnafrca"],
    "ACADEMY": ["academy press", "academy"],
}

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def load_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()

def matches(blob, aliases):
    h = norm(blob)
    return any(norm(a) in h for a in aliases)

def main():
    archive = load_json(ARCHIVE, [])
    state = load_json(STATE, {})
    pending = load_csv(PENDING)
    published = load_csv(PUBLISHED)
    review = load_json(REVIEW, [])
    processed = state.get("processed", {}) if isinstance(state, dict) else {}

    print("NGX BENCHMARK DIAGNOSTIC — PATCH 22")
    print(f"Archive PDFs: {len(archive)}")
    print(f"Published: {len(published)} | Pending: {len(pending)} | Review: {len(review) if isinstance(review,list) else 0}")

    for name, aliases in BENCHMARKS.items():
        print("\n" + "="*70)
        print(name)

        pub = [r for r in published if matches(" ".join(map(str,r.values())), aliases)]
        pen = [r for r in pending if matches(" ".join(map(str,r.values())), aliases)]
        rev = []
        if isinstance(review, list):
            for item in review:
                p = item.get("parsed") or {}
                blob = " ".join([str(item.get("title","")), str(item.get("url","")), str(p)])
                if matches(blob, aliases):
                    rev.append(item)
        arc = [a for a in archive if matches(" ".join(map(str,a.values())), aliases)]

        if pub:
            status = "PUBLISHED"
        elif pen:
            status = "IN PENDING"
        elif rev:
            status = "PROCESSED / REVIEW"
        elif arc:
            states = {processed.get(a.get("url",""), "") for a in arc}
            if states & {"not_dividend","statement_noise","non_actionable_noise"}:
                status = "REJECTED AFTER DISCOVERY"
            elif states & {"pending","review","error",""}:
                status = "IN ARCHIVE / UNRESOLVED"
            else:
                status = "IN ARCHIVE"
        else:
            status = "NEVER DISCOVERED"

        print("STATUS:", status)
        print("Archive matches:", len(arc))
        if arc:
            for a in arc[:6]:
                u = a.get("url","")
                print(" -", processed.get(u, "<none>"), a.get("title","")[:120])
        if pen:
            print("Pending matches:")
            for r in pen[:4]:
                print(" -", r.get("dividend_per_share"), r.get("qualification_date"), r.get("payment_date"), r.get("company"))
        if rev:
            print("Review matches:")
            for item in rev[:4]:
                p = item.get("parsed") or {}
                print(" -", item.get("classification"), p.get("dividend_per_share"), item.get("reason_codes") or item.get("errors"))

    print("\n" + "="*70)
    print("SUSPECT PUBLISHED EVENTS")
    for ticker in ("FCMB","UNILEVER"):
        rows = [r for r in published if (r.get("ticker") or "").upper() == ticker]
        if not rows:
            print(ticker, ": not published")
            continue
        for r in rows:
            print(ticker, ":", r.get("dividend_per_share"), r.get("currency"), "|", r.get("source_title",""))

if __name__ == "__main__":
    main()
