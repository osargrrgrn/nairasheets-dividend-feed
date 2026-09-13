"""
collector/diagnose_access.py — round 3: SharePoint paging rules

Round 2 proved doclib's SharePoint REST API is anonymously readable.
This round determines HOW to enumerate it: paging, filters, and whether
the filename number prefix is the SharePoint item ID.

Run manually:  python -u -m collector.diagnose_access
Read-only.
"""

import re
import json
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
H = {"Accept": "application/json;odata=verbose", "User-Agent": UA}
BASE = "https://doclib.ngxgroup.com"
NUM_RE = re.compile(r"^(\d{3,6})_")
DIV_RE = re.compile(r"DIVIDEND|DISTRIBUTION|CORPORATE_ACTION|NGX_NOTIFICATION", re.I)


def section(t):
    print("\n" + "=" * 70 + "\n" + t + "\n" + "=" * 70, flush=True)


def call(url, timeout=(10, 60)):
    try:
        r = requests.get(url, headers=H, timeout=timeout)
    except Exception as exc:
        print(f"  EXC {exc!r}", flush=True)
        return None, None
    try:
        data = r.json()
    except Exception:
        data = None
    return r, data


def results(data):
    if not data:
        return [], None
    d = data.get("d", {})
    res = d.get("results", d if isinstance(d, list) else [])
    nxt = d.get("__next")
    return res, nxt


def stats(names):
    nums = sorted(int(m.group(1)) for n in names for m in [NUM_RE.match(n or "")] if m)
    div = [n for n in names if DIV_RE.search(n or "")]
    gap = [x for x in nums if 42000 <= x <= 46023]
    return (nums[0] if nums else None), (nums[-1] if nums else None), len(div), len(gap)


def show(label, r, data, name_key):
    if r is None:
        return []
    res, nxt = results(data)
    names = [x.get(name_key, "") for x in res]
    lo, hi, div, gap = stats(names)
    err = ""
    if data and "error" in data:
        err = str(data["error"].get("message", {}).get("value", ""))[:160]
    print(f"  HTTP {r.status_code}  {label}", flush=True)
    print(f"     count={len(res)} num_range={lo}-{hi} dividend_like={div} in_gap={gap} has_next={bool(nxt)} {('ERR: '+err) if err else ''}", flush=True)
    if res:
        print(f"     first={names[0][:70]}", flush=True)
        print(f"     last ={names[-1][:70]}", flush=True)
    return res


def main():
    F = f"{BASE}/_api/web/GetFolderByServerRelativeUrl('/Financial_NewsDocs')"
    L = f"{BASE}/_api/web/GetList('/Financial_NewsDocs')"

    section("A. Folder item count")
    r, d = call(f"{F}/ItemCount")
    print(f"  HTTP {r.status_code if r else None}  ItemCount={(d or {}).get('d',{}).get('ItemCount')}", flush=True)

    section("B. Files: $top=5000 (does it page, does it throttle?)")
    resB = show("Files?$select=Name,TimeCreated&$top=5000", *call(f"{F}/Files?$select=Name,TimeCreated&$top=5000"), "Name")

    section("C. Files: $skip=5000 (is $skip honoured?)")
    show("Files?$top=5000&$skip=5000", *call(f"{F}/Files?$select=Name,TimeCreated&$top=5000&$skip=5000"), "Name")

    section("D. Files: filter TimeCreated >= 2026-01-01")
    show("Files?$filter=TimeCreated ge 2026-01-01", *call(
        f"{F}/Files?$select=Name,TimeCreated&$filter=TimeCreated ge datetime'2026-01-01T00:00:00Z'&$top=5000"), "Name")

    section("E. Files: filter substringof('DIVIDEND', Name)")
    show("Files?$filter=substringof('DIVIDEND',Name)", *call(
        f"{F}/Files?$select=Name,TimeCreated&$filter=substringof('DIVIDEND',Name)&$top=5000"), "Name")

    section("F. List items ordered by ID desc (is filename prefix == item ID?)")
    resF = show("items?$orderby=ID desc&$top=20", *call(
        f"{L}/items?$select=ID,FileLeafRef,Created&$orderby=ID desc&$top=20"), "FileLeafRef")
    for x in resF[:8]:
        print(f"     ID={x.get('ID')}  name={str(x.get('FileLeafRef',''))[:60]}  created={x.get('Created')}", flush=True)

    section("G. List items: ID in gap 42000..46023 (the killer query if F says ID==prefix)")
    resG = show("items?$filter=ID ge 42000 and ID le 46023&$top=5000", *call(
        f"{L}/items?$select=ID,FileLeafRef,Created&$filter=ID ge 42000 and ID le 46023&$orderby=ID&$top=5000"), "FileLeafRef")
    div = [x for x in resG if DIV_RE.search(str(x.get("FileLeafRef", "")))]
    print(f"     dividend_like_in_gap={len(div)}", flush=True)
    for x in div[:15]:
        print(f"     ID={x.get('ID')}  {str(x.get('FileLeafRef',''))[:80]}", flush=True)

    section("H. List items: Created >= 2026-01-01 (alternative if ID != prefix)")
    show("items?$filter=Created ge 2026-01-01&$top=5000", *call(
        f"{L}/items?$select=ID,FileLeafRef,Created&$filter=Created ge datetime'2026-01-01T00:00:00Z'&$orderby=ID&$top=5000"), "FileLeafRef")

    print("\nDone. Read-only.", flush=True)


if __name__ == "__main__":
    main()
