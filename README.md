# Expense Reports

A single-page expense report app that runs as a private Claude artifact and is used from the iPhone.

## What it does

- One report per entity and project, with a period, name, institution and position.
- Report numbers are assigned automatically per entity and year: `AEI-2026-0001`, `YMM-2026-0001`.
- Aren Energy Investment Pte. Ltd. reports total in SGD. Yayasan Mens et Manus reports total in IDR. Receipts in another currency keep their original amount and are converted with an exchange rate entered on the receipt.
- A receipt is added by photo. Claude reads the merchant, date, currency, total, line items and a suggested expense code. Every field stays editable before saving.
- Alcohol on an itemised receipt is split out, with its share of tax and service charge, and booked to its own expense code (6220 by default).
- Expense codes are a general travel and entertainment chart, editable under Setup. They are not an official standard.
- Export: a PDF with the receipt photos attached, and an Excel file of the line items.

## Files

- `index.html`: the whole app (markup, styles, script).
- `lib/`: jsPDF 2.5.1, jspdf-autotable 3.8.2 and SheetJS 0.18.5 (mini build), served with the page.
- `tests/test_app.py`: end-to-end test in a headless browser, with stand-ins for the Claude capabilities.

## Where the data lives

Inside Claude the page uses the artifact capabilities `db` (reports and setup, in the viewer's private area), `assets` (receipt photos), `sample` with images (receipt reading) and `downloads` (export files). Nothing is stored in this repository. Opened outside Claude, the page falls back to the browser's local storage and has no receipt reading.

## Test

```
python3 tests/test_app.py
```

Needs Python with `playwright`, `pillow`, `openpyxl`, `pypdf`, and Google Chrome at `/usr/bin/google-chrome`.

The app also has a self check under Setup. It reads a built-in sample receipt and checks photo storage and both export files without adding anything to the reports.
