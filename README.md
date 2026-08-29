# NairaSheets NGX Dividend Feed

A central, automated dividend-data pipeline designed for NairaSheets.

## Goal

Official NGX corporate disclosures (PDFs)
→ automated discovery
→ dividend extraction and validation
→ `docs/dividends.csv`
→ Google Sheets via `IMPORTDATA`

Customers do not run the scraper and do not authorize Apps Script for dividend retrieval.

## Architecture

1. GitHub Actions runs on a schedule.
2. Playwright opens the official NGX Corporate Disclosures page.
3. The collector gathers links hosted at:
   `https://doclib.ngxgroup.com/Financial_NewsDocs/...pdf`
4. New/changed PDFs are downloaded once centrally.
5. Text is extracted with `pypdf`.
6. Dividend/corporate-action fields are normalized.
7. Validation prevents obviously bad records from being published.
8. Clean records are written to `docs/dividends.csv`.
9. GitHub Pages can publish the `/docs` folder as a public static feed.

## Google Sheets

Once GitHub Pages is enabled, NairaSheets can use:

    =IMPORTDATA("https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPO/dividends.csv")

No buyer API key is needed.

## Important design choice

Each dividend event has its own event ID. Do not match only by ticker because one
company can declare multiple dividends in the same year.

## Status

This scaffold intentionally uses conservative extraction. Ambiguous PDFs are held
out of the published feed instead of guessing.
