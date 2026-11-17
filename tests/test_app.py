"""End-to-end test of the expense report page in a headless browser.

The Claude capabilities (storage, receipt reading, file export, photo store) are
replaced by small stand-ins, so the test checks the page's own behaviour:
numbering, arithmetic, alcohol split, currency conversion, export files, layout.

Run:  python3 tests/test_app.py      (from the repository root)
"""
import base64, datetime, functools, http.server, io, json, os, subprocess, sys, threading
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw
import openpyxl, pypdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("TEST_OUT", "/tmp/expense-test")
os.makedirs(OUT, exist_ok=True)
YEAR = datetime.date.today().year
PORT = 8765
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("   [" + str(detail) + "]") if detail and not cond else ""))


MOCK = r"""
window.claude = (() => {
  const KEY = 'mockdb'; let data = JSON.parse(localStorage.getItem(KEY) || '{}'); const subs = [];
  const persist = () => localStorage.setItem(KEY, JSON.stringify(data));
  const notify = () => subs.forEach(f => setTimeout(f, 0));
  const snap = p => ({id: p.split('/').pop(), exists: p in data, data: () => data[p] ? JSON.parse(JSON.stringify(data[p])) : undefined, metadata: {fromCache: false, hasPendingWrites: false}});
  const q = p => { const docs = Object.keys(data).filter(k => k.startsWith(p + '/') && !k.slice(p.length + 1).includes('/')).sort().map(snap); return {docs, size: docs.length, empty: !docs.length, docChanges: () => [], metadata: {fromCache: false}}; };
  const docRef = p => ({id: p.split('/').pop(), path: p,
    get: async () => snap(p),
    set: async b => { if (p.split('/').length % 2) throw new TypeError('odd path'); if (JSON.stringify(b).length > 262144) throw {code: 'invalid_argument', message: 'too large'}; data[p] = JSON.parse(JSON.stringify(b)); persist(); notify(); (window.__writes = window.__writes || []).push(p); },
    delete: async () => { delete data[p]; persist(); notify(); },
    onSnapshot: next => { const f = () => next(snap(p)); subs.push(f); setTimeout(f, 30); return () => {}; },
    collection: s => colRef(p + '/' + s)});
  const colRef = p => ({path: p, doc: id => docRef(p + '/' + (id || Math.random().toString(36).slice(2))), get: async () => q(p),
    onSnapshot: next => { const f = () => next(q(p)); subs.push(f); setTimeout(f, 30); return () => {}; }});
  const sample = Object.assign(async () => ({text: 'x', truncated: false}), {
    json: async (input, o) => { window.__calls = (window.__calls || 0) + 1; window.__prompt = input; window.__imgs = o && o.images ? o.images.length : 0;
      await new Promise(r => setTimeout(r, 250)); if (window.__fail) throw {code: window.__fail, message: 'mock'}; return JSON.parse(JSON.stringify(window.__reply)); },
    limits: async () => ({maxPromptBytes: 262144, images: {maxCount: 4, maxInputBytes: 2e7, mediaTypes: ['image/jpeg', 'image/png']}})});
  const downloads = {save: async ({filename, data}) => { const u = new Uint8Array(await data.arrayBuffer()); let s = ''; for (let i = 0; i < u.length; i += 8192) s += String.fromCharCode.apply(null, u.subarray(i, i + 8192)); (window.__saved = window.__saved || {})[filename] = btoa(s); return {status: 'saved'}; }};
  const assets = {upload: async blob => { const id = 'asset' + Math.random().toString(36).slice(2); (window.__up = window.__up || []).push(id); return {id, url: URL.createObjectURL(blob), sizeBytes: blob.size, contentType: blob.type}; },
    delete: async id => { (window.__del = window.__del || []).push(id); return {deleted: true}; }, list: async () => ({assets: [], usage: {}})};
  const m = {db: {doc: docRef, collection: colRef}, user: {id: async () => 'u_test'}, sample, downloads, assets};
  return {use: n => new Promise(r => setTimeout(() => r(m[n] || null), 40))};
})();
"""

REPLY = {"readable": True, "merchant": "Harbour Lane Kitchen", "date": "2026-11-14", "currency": "SGD", "total": 151.07,
         "subtotal": 126.00, "tax": 12.47, "service": 12.6, "code": "6130", "purpose": "Dinner for three",
         "items": [{"d": "Grilled barramundi", "a": 38, "alc": False}, {"d": "Wagyu burger", "a": 32, "alc": False},
                   {"d": "Sauvignon Blanc glass x2", "a": 36, "alc": True}, {"d": "Tiger Beer pint", "a": 12, "alc": True},
                   {"d": "Sparkling water", "a": 8, "alc": False}], "alcohol_subtotal": 48, "notes": ""}


def make_receipt(path):
    im = Image.new("RGB", (900, 1400), "white"); d = ImageDraw.Draw(im)
    for i, t in enumerate(["HARBOUR LANE KITCHEN", "Grilled barramundi 38.00", "Wagyu burger 32.00", "Sauvignon Blanc x2 36.00",
                           "Tiger Beer 12.00", "Sparkling water 8.00", "Subtotal 126.00", "Service 12.60", "GST 12.47", "TOTAL SGD 151.07"]):
        d.text((60, 80 + i * 110), t, fill="black")
    im.save(path, "JPEG", quality=85)
    b = io.BytesIO(); im.save(b, "JPEG", quality=70); return b.getvalue()


def make_pdf(path):
    pages = []
    for n in (1, 2):
        im = Image.new("RGB", (1240, 1754), "white"); d = ImageDraw.Draw(im)
        d.text((100, 100), "TAX INVOICE  page %d of 2" % n, fill="black")
        for i in range(12):
            d.text((100, 220 + i * 90), ("Line item %d" % (i + 1)) if n == 1 else ("Total due SGD 151.07" if i == 11 else "Terms %d" % i), fill="black")
        pages.append(im)
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:], resolution=150)


def serve():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **k: None
    s = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()


def text(page, sel="#view"):
    return page.inner_text(sel)


def saved(page, name):
    b = page.evaluate("n => (window.__saved || {})[n] || ''", name)
    return base64.b64decode(b) if b else b""


def new_report(page, entity, project, name="Test Claimant", pos="Director", frm=None, to=None):
    page.click("[data-act=new]"); page.wait_for_selector("#m-project")
    if page.query_selector("#m-ent"):
        page.select_option("#m-ent", entity)
    page.fill("#m-project", project); page.fill("#m-name", name); page.fill("#m-pos", pos)
    if frm: page.fill("#m-from", frm)
    if to: page.fill("#m-to", to)
    page.click("[data-act=save-meta]"); page.wait_for_selector("h1.num")
    return page.inner_text("h1.num")


def manual(page, merchant, date, cur, total, code, rate=None, alc=None, purpose=""):
    page.click("[data-act=manual]"); page.wait_for_selector("#f-merchant")
    page.fill("#f-merchant", merchant); page.fill("#f-date", date); page.fill("#f-cur", cur); page.fill("#f-purpose", purpose)
    page.select_option("#f-code", code); page.fill("#f-total", str(total))
    if alc is not None: page.fill("#f-alc", str(alc))
    if rate is not None: page.fill("#f-rate", str(rate))
    page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")


def run():
    serve(); img_bytes = make_receipt(os.path.join(OUT, "receipt.jpg")); errors = []
    make_pdf(os.path.join(OUT, "invoice.pdf")); open(os.path.join(OUT, "broken.pdf"), "wb").write(b"%PDF-1.4 this is not really a pdf")
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])

        # ---------- A. inside Claude (stand-in capabilities), phone-sized ----------
        ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="light")
        ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort()); ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())
        ctx.route("**/_blob/**", lambda r: r.fulfill(status=200, content_type="image/jpeg", body=img_bytes))
        ctx.add_init_script(MOCK); ctx.add_init_script("window.__reply = %s;" % json.dumps(REPLY))
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "ERR_FAILED" not in m.text and "fonts" not in m.text else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        url = "http://127.0.0.1:%d/index.html" % PORT
        page.goto(url); page.wait_for_selector("text=No reports yet")
        check("libraries load from the page's own files", page.evaluate("!!(window.jspdf && window.jspdf.jsPDF && window.XLSX)"))
        check("empty state shown, storage is Claude storage", page.evaluate("ExpenseApp.store.mode") == "cloud")
        page.screenshot(path=os.path.join(OUT, "01-empty.png"))

        # numbering and header
        n1 = new_report(page, "AEI", "Workshop Jakarta", frm="%d-11-01" % YEAR, to="%d-11-30" % YEAR)
        check("first Aren Energy report number", n1 == "AEI-%d-0001" % YEAR, n1)
        t = text(page)
        check("header shows project, name, institution, position, period", all(x in t for x in ["Workshop Jakarta", "Test Claimant", "Aren Energy Investment Pte. Ltd.", "Director", "1 Nov %d to 30 Nov %d" % (YEAR, YEAR)]), t[:300])
        check("new report totals zero and exports are locked", "SGD 0.00" in t and page.is_disabled("[data-act=pdf]"))

        # receipt by photo: reading, alcohol split, code
        page.set_input_files("#lib", os.path.join(OUT, "receipt.jpg"))
        page.wait_for_selector("#f-reading"); check("fields are locked while the receipt is read", page.is_disabled("#f-merchant"))
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'")
        check("photo was sent to the reader once", page.evaluate("[window.__calls, window.__imgs]") == [1, 1])
        check("prompt carries the code chart and the Indonesian number rule", page.evaluate("window.__prompt.includes('6200 Entertainment: Client') && window.__prompt.includes('125000') && !window.__prompt.includes('6220 Entertainment: Alcohol')"))
        v = page.evaluate("['#f-date','#f-cur','#f-total','#f-alc','#f-code'].map(s => document.querySelector(s).value)")
        check("read values: date, currency, total, alcohol, code", v == ["2026-11-14", "SGD", "151.07", "57.55", "6130"], v)
        check("item list shows 5 items with 2 alcohol flags", page.locator(".items li").count() == 5 and page.locator(".items .chip").count() == 2)
        page.click("#f-quick button[data-code='6200']")
        c = page.inner_text("#f-calc")
        check("claim lines preview: 93.52 to 6200 and 57.55 to 6220", "SGD 93.52" in c and "6200" in c and "SGD 57.55" in c and "6220" in c, c)
        page.screenshot(path=os.path.join(OUT, "02-receipt-sheet.png"), full_page=False)
        page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        t = text(page)
        check("report total equals the receipt total", "SGD 151.07" in page.inner_text(".total"), page.inner_text(".total"))
        rows = page.eval_on_selector_all("section table tbody tr", "els => els.map(e => e.innerText.replace(/\\s+/g,' '))")
        check("summary by code: 6200 = 93.52, 6220 = 57.55", len(rows) == 2 and "6200" in rows[0] and "93.52" in rows[0] and "6220" in rows[1] and "57.55" in rows[1], rows)
        check("photo stored as an asset", page.evaluate("(window.__up || []).length") == 1 and page.evaluate("ExpenseApp.S.reports[ExpenseApp.S.curId].receipts[0].img.startsWith('a:')"))

        # foreign currency with rate, remembered rate
        manual(page, "Garuda Indonesia", "%d-11-10" % YEAR, "USD", 100, "6100", rate=1.35, purpose="Flight SIN to CGK")
        check("USD 100 at 1.35 adds SGD 135.00", "SGD 286.07" in page.inner_text(".total"), page.inner_text(".total"))
        page.click("[data-act=manual]"); page.fill("#f-cur", "USD")
        check("rate is remembered for the next USD receipt", page.input_value("#f-rate") == "1.35", page.input_value("#f-rate"))
        page.click("[data-act=save-rc]")
        check("saving without a merchant is refused with a message", page.is_visible("#f-err") and "merchant" in page.inner_text("#f-err").lower())
        page.click("[data-act=close]")

        # receipt in a foreign currency with no rate yet locks export
        page.evaluate("""(() => { const A = ExpenseApp, r = JSON.parse(JSON.stringify(A.S.reports[A.S.curId])); r.receipts.push({id:'norate', merchant:'Hotel', date:'2026-11-12', purpose:'', code:'6110', currency:'EUR', total:200, alcohol:0, rate:0, note:'', items:[], img:''}); return A.store.saveReport(r); })()""")
        page.wait_for_selector("text=no exchange rate yet")
        check("missing rate is flagged and export is locked", page.is_disabled("[data-act=pdf]") and page.is_disabled("[data-act=xlsx]"))
        page.click("button.rc[data-id=norate]"); page.fill("#f-rate", "1.45"); page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("after the rate: total 576.07", "SGD 576.07" in page.inner_text(".total"), page.inner_text(".total"))
        page.screenshot(path=os.path.join(OUT, "03-report.png"), full_page=True)

        # rupiah receipt in an SGD report: the rate is typed the market way, 1 SGD = 11,500 IDR, and divided
        page.click("[data-act=manual]"); page.wait_for_selector("#f-merchant")
        page.fill("#f-merchant", "Warung Test"); page.fill("#f-date", "%d-11-13" % YEAR); page.select_option("#f-code", "6130"); page.fill("#f-cur", "IDR"); page.fill("#f-total", "130140")
        check("an IDR receipt in an SGD report defaults to '1 SGD = ... IDR'", page.input_value("#f-dir") == "div" and "1 SGD = … IDR" in page.inner_text("#f-dir"), page.input_value("#f-dir"))
        page.fill("#f-rate", "11500"); c = page.inner_text("#f-calc"); h = page.inner_text("#f-rate-h")
        check("IDR 130,140 at 1 SGD = 11,500 IDR previews as SGD 11.32", "SGD 11.32" in c and "1 IDR = 0.000086956522 SGD" in h, [c, h])
        page.select_option("#f-dir", "mul"); check("switching the direction changes the result", "SGD 1,496,610,000.00" in page.inner_text("#f-calc"))
        page.select_option("#f-dir", "div"); page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("report total adds SGD 11.32", "SGD 587.39" in page.inner_text(".total"), page.inner_text(".total"))
        check("receipt row shows the rate as typed", "1 SGD = 11,500 IDR" in text(page))
        page.click("[data-act=manual]"); page.fill("#f-cur", "IDR")
        check("the next IDR receipt offers the same rate and direction", [page.input_value("#f-dir"), page.input_value("#f-rate")] == ["div", "11500"]); page.click("[data-act=close]")
        page.evaluate("""(() => { const A = ExpenseApp, r = JSON.parse(JSON.stringify(A.S.reports[A.S.curId])); r.receipts.push({id:'old', merchant:'Old entry', date:'2026-11-12', purpose:'', code:'6130', currency:'IDR', total:130140, alcohol:0, rate:11500, note:'', items:[], img:''}); return A.store.saveReport(r); })()""")
        page.wait_for_function("document.querySelector('.total').innerText.includes('1,496,610')")
        page.click("button.rc[data-id=old]")
        check("a receipt saved before this fix opens with the rate read the market way", page.input_value("#f-dir") == "div" and "SGD 11.32" in page.inner_text("#f-calc"))
        page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("saving it again corrects the total", "SGD 598.71" in page.inner_text(".total"), page.inner_text(".total"))
        page.click("button.rc[data-id=old]"); page.click("[data-act=del-rc]"); page.click("[data-act=del-rc]"); page.wait_for_selector("#sheet", state="hidden")
        page.click("button.rc:has-text('Warung Test')"); page.click("[data-act=del-rc]"); page.click("[data-act=del-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("after removing the two rupiah receipts the total is back to 576.07", "SGD 576.07" in page.inner_text(".total"))

        # exports
        page.click("[data-act=pdf]"); page.wait_for_function("(window.__saved || {})['AEI-%d-0001.pdf']" % YEAR, timeout=30000)
        pdf = saved(page, "AEI-%d-0001.pdf" % YEAR); open(os.path.join(OUT, "AEI.pdf"), "wb").write(pdf)
        rd = pypdf.PdfReader(io.BytesIO(pdf)); ptxt = "\n".join(p.extract_text() for p in rd.pages)
        check("PDF opens and has a receipt photo page", pdf[:5] == b"%PDF-" and len(rd.pages) == 2, len(rd.pages))
        check("PDF holds number, header fields, lines and total", all(x in ptxt for x in ["AEI-%d-0001" % YEAR, "Workshop Jakarta", "Test Claimant", "Director", "Harbour Lane Kitchen", "Alcohol portion", "USD 100.00", "1.35", "SGD per USD", "576.07", "93.52", "57.55", "Claimed by"]), ptxt[:600])
        check("PDF page 2 embeds an image", len(list(rd.pages[1].images)) == 1)
        page.click("[data-act=xlsx]"); page.wait_for_function("(window.__saved || {})['AEI-%d-0001.xlsx']" % YEAR)
        xb = saved(page, "AEI-%d-0001.xlsx" % YEAR); open(os.path.join(OUT, "AEI.xlsx"), "wb").write(xb)
        wb = openpyxl.load_workbook(io.BytesIO(xb)); ws = wb["Expense report"]; rows = [[c.value for c in r] for r in ws.iter_rows()]
        lines = [r for r in rows if isinstance(r[0], (int, float))]
        check("Excel has 4 lines, alcohol line on 6220", len(lines) == 4 and any(r[4] == "6220" and abs(r[9] - 57.55) < 1e-9 and r[10] == "Alcohol" for r in lines), lines)
        check("Excel amounts are numbers and sum to the total", abs(sum(r[9] for r in lines) - 576.07) < 1e-6 and abs([r for r in rows if r[8] == "Total"][0][9] - 576.07) < 1e-9)
        check("Excel header carries number and currency", rows[1][1] == "AEI-%d-0001" % YEAR and rows[9][1] == "SGD", rows[:10])
        s2 = [[c.value for c in r] for r in wb["Summary by code"].iter_rows()]
        check("Excel summary sheet totals 576.07", abs(s2[-1][2] - 576.07) < 1e-9 and len(s2) == 4 + 4 + 1, s2)

        # second entity: rupiah, no decimals, conversion and rounding
        page.click("a[href='#reports']"); page.wait_for_selector("ul.list")
        n2 = new_report(page, "YMM", "Riset Lapangan Bandung")
        check("first Yayasan report number, own series", n2 == "YMM-%d-0001" % YEAR, n2)
        check("institution defaults to the entity", "Yayasan Mens et Manus" in text(page))
        manual(page, "Kopi Kenangan", "%d-11-15" % YEAR, "IDR", 125000, "6130")
        manual(page, "Changi Taxi", "%d-11-16" % YEAR, "SGD", 20.50, "6120", rate=12345)
        check("IDR report: 125,000 + 20.50 x 12,345 = 378,073 with no decimals", "IDR 378,073" in page.inner_text(".total"), page.inner_text(".total"))
        page.click("[data-act=xlsx]"); page.wait_for_function("(window.__saved || {})['YMM-%d-0001.xlsx']" % YEAR)
        w2 = openpyxl.load_workbook(io.BytesIO(saved(page, "YMM-%d-0001.xlsx" % YEAR)))["Expense report"]
        tot = [r for r in w2.iter_rows(values_only=True) if r[8] == "Total"][0][9]
        check("IDR Excel total is 378073", tot == 378073, tot)
        page.click("[data-act=pdf]"); page.wait_for_function("(window.__saved || {})['YMM-%d-0001.pdf']" % YEAR, timeout=30000)
        p2 = "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(saved(page, "YMM-%d-0001.pdf" % YEAR))).pages)
        check("IDR PDF shows 378,073 and one page (no photos)", "378,073" in p2 and "253,073" in p2, p2[:400])

        page.click("a[href='#reports']"); page.wait_for_selector("ul.list")
        n3 = new_report(page, "AEI", "Board meeting")
        check("second Aren Energy report continues the series", n3 == "AEI-%d-0002" % YEAR, n3)
        page.click("a[href='#reports']"); page.wait_for_selector("ul.list"); page.click("[data-act=new]"); page.wait_for_selector("#m-project")
        check("name and position are prefilled from the entity's latest report", [page.input_value("#m-name"), page.input_value("#m-pos")] == ["Test Claimant", "Director"])
        page.select_option("#m-ent", "YMM"); check("switching entity switches the institution default", page.input_value("#m-inst") == "Yayasan Mens et Manus")
        check("earlier projects are offered as suggestions", page.locator("#projs option").count() == 3)
        page.click("[data-act=close]")
        n4 = new_report(page, "YMM", "Scratch"); page.click("[data-act=del-report]"); page.click("[data-act=del-report]"); page.wait_for_selector("ul.list")
        n5 = new_report(page, "YMM", "Second field trip")
        check("deleting the newest draft frees its number", n4 == n5 == "YMM-%d-0002" % YEAR, [n4, n5])
        page.click("[data-act=del-report]"); page.click("[data-act=del-report]"); page.wait_for_selector("ul.list")

        # persistence across a reload (same stand-in storage)
        page.goto(url + "#reports"); page.reload(); page.wait_for_selector("ul.list")
        check("the page really reloaded (stand-in counters reset)", page.evaluate("window.__calls === undefined"))
        lt = text(page)
        check("after reload all three reports are listed with totals", all(x in lt for x in ["AEI-%d-0001" % YEAR, "AEI-%d-0002" % YEAR, "YMM-%d-0001" % YEAR, "SGD 576.07", "IDR 378,073"]), lt)
        page.screenshot(path=os.path.join(OUT, "04-list.png"))
        page.click("button.rep:has-text('AEI-%d-0001')" % YEAR); page.wait_for_selector("h1.num")
        page.wait_for_function("document.querySelector('img.th') && document.querySelector('img.th').naturalWidth > 0")
        check("stored photo is shown again after reload", True)
        page.click("[data-act=pdf]"); page.wait_for_function("(window.__saved || {})['AEI-%d-0001.pdf']" % YEAR, timeout=30000)
        check("PDF after reload still embeds the photo", len(list(pypdf.PdfReader(io.BytesIO(saved(page, "AEI-%d-0001.pdf" % YEAR))).pages[1].images)) == 1)

        # a photo dropped onto the report (desktop) opens the same flow
        page.evaluate("""async () => { const b = await ExpenseApp.sampleReceipt(), dt = new DataTransfer(); dt.items.add(new File([b], 'r.jpg', {type: 'image/jpeg'})); document.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true})); }""")
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'")
        check("a dropped photo is read like a chosen one", True); page.click("[data-act=close]"); page.wait_for_timeout(200)
        page.evaluate("window.__del = []")

        # submitted state locks editing; reopen
        page.click("[data-act=status]"); page.wait_for_selector(".head .chip.ok")
        check("submitted report hides the add-receipt controls", page.locator("label[for=cam]").count() == 0 and page.locator("[data-act=edit-meta]").count() == 0)
        page.click("button.rc >> nth=0"); check("receipt fields are read-only when submitted", page.is_disabled("#f-merchant") and page.locator("[data-act=save-rc]").count() == 0); page.click("[data-act=close]")
        page.click("[data-act=status]"); page.wait_for_selector("label[for=cam]")

        # reading failure path
        page.evaluate("window.__fail = 'rate_limited'")
        page.set_input_files("#lib", os.path.join(OUT, "receipt.jpg")); page.wait_for_selector("[data-act=retry-read]")
        check("failed reading shows a message, a retry, and unlocked fields", "usage limit" in page.inner_text("#sheet") and not page.is_disabled("#f-merchant"))
        page.evaluate("window.__fail = ''"); page.click("[data-act=retry-read]")
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'")
        check("retry reads the receipt", True)
        page.click("[data-act=close]"); page.wait_for_timeout(300)
        check("closing an unsaved receipt removes its uploaded photo", page.evaluate("(window.__del || []).length") == 1)

        # delete receipt (two taps), delete report
        cnt = page.locator("button.rc").count()
        page.click("button.rc >> nth=0"); page.click("[data-act=del-rc]")
        check("first tap on delete only arms it", page.is_visible("#sheet") and "confirm" in page.inner_text("[data-act=del-rc]").lower())
        page.click("[data-act=del-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("second tap deletes the receipt", page.locator("button.rc").count() == cnt - 1)

        # setup: edit chart, validation, self test
        page.click("#nav-setup"); page.wait_for_selector("#setup-form")
        page.screenshot(path=os.path.join(OUT, "05-setup.png"), full_page=True)
        page.click("[data-act=add-code]"); page.locator("#codes .row >> nth=-1").locator("[name=ccode]").fill("6200")
        page.locator("#codes .row >> nth=-1").locator("[name=clabel]").fill("Duplicate"); page.click("[data-act=save-setup]")
        check("duplicate expense code is refused", "share the same code" in page.inner_text("#toast"))
        page.locator("#codes .row >> nth=-1").locator("[name=ccode]").fill("6950"); page.locator("#codes .row >> nth=-1").locator("[name=clabel]").fill("Site visit costs")
        page.locator("#codes .row >> nth=0").locator("[name=clabel]").fill("Travel: Flights")
        page.click("[data-act=save-setup]"); page.wait_for_function("document.querySelector('#toast').textContent.includes('Setup saved')")
        page.goto(url + "#setup"); page.reload(); page.wait_for_selector("#setup-form")
        codes = page.evaluate("ExpenseApp.S.settings.codes.map(c => c.code + '|' + c.label + '|' + (c.alcohol || 0) + '|' + (c.meal || 0))")
        check("chart edits persist: renamed, added, alcohol and meal marks kept", "6100|Travel: Flights|0|0" in codes and "6950|Site visit costs|0|0" in codes and "6220|Entertainment: Alcohol|1|0" in codes and "6200|Entertainment: Client|0|1" in codes, codes)
        nx = page.locator("#ents [name=enext] >> nth=0")
        check("setup shows the next Aren Energy number", nx.input_value() == "3" and "AEI-%d-0003" % YEAR in page.inner_text("#ents"), nx.input_value())
        nx.fill("2"); page.click("[data-act=save-setup]")
        check("a next number already used is refused", "already exists" in page.inner_text("#toast") and "3 or higher" in page.inner_text("#toast"), page.inner_text("#toast"))
        nx.fill("7"); page.click("[data-act=save-setup]"); page.wait_for_function("document.querySelector('#toast').textContent.includes('Setup saved')")
        page.reload(); page.wait_for_selector("#setup-form")
        check("the next number is kept after a restart", page.locator("#ents [name=enext] >> nth=0").input_value() == "7")

        page.click("[data-act=selftest]"); page.wait_for_function("document.querySelectorAll('#selftest table').length === 2", timeout=30000)
        st = page.inner_text("#selftest")
        check("self check: reading correct on 7 fields, storage and export correct on 5 steps", st.count("Correct") == 12 and "Differs" not in st and "Failed" not in st, st)
        check("self check leaves no report and removes its test photo", page.evaluate("Object.keys(ExpenseApp.S.reports).length") == 3 and page.evaluate("(window.__del || []).length") >= 1)

        # layout: nothing wider than a small phone; dark theme renders
        page.set_viewport_size({"width": 360, "height": 740})
        for h in ["#reports", "#setup"]:
            page.goto(url + h); page.wait_for_timeout(400)
            check("no sideways scroll at 360px on " + h, page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), page.evaluate("document.documentElement.scrollWidth"))
        page.goto(url + "#reports"); page.click("button.rep >> nth=0"); page.wait_for_selector("h1.num")
        check("no sideways scroll at 360px on a report", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        page.emulate_media(color_scheme="dark"); page.set_viewport_size({"width": 390, "height": 844}); page.wait_for_timeout(200)
        page.screenshot(path=os.path.join(OUT, "06-report-dark.png"), full_page=True)
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check("dark theme uses the night background", bg in ("rgb(16, 25, 30)",), bg)
        check("every stored write went under the viewer's private path", page.evaluate("(window.__writes || []).every(p => p.startsWith('data/users/u_test/'))"))

        # PDF receipt: two pages read together, stored, attached
        page.goto(url + "#reports"); page.wait_for_selector("ul.list")
        n6 = new_report(page, "AEI", "PDF receipts"); page.evaluate("window.__up = []; window.__del = []")
        check("the next report continues at the number set by hand", n6 == "AEI-%d-0007" % YEAR, n6)
        page.set_input_files("#doc", os.path.join(OUT, "invoice.pdf"))
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'", timeout=30000)
        check("a 2-page PDF is sent to the reader as 2 page images", page.evaluate("window.__imgs") == 2, page.evaluate("window.__imgs"))
        check("the first page is shown as the preview", page.evaluate("document.querySelector('#sheet img.shot').naturalWidth > 500"))
        page.click("#f-quick button[data-code='6200']"); page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("both pages are stored with the receipt", page.evaluate("(r => [r.img.startsWith('a:'), (r.more || []).length])(ExpenseApp.S.reports[ExpenseApp.S.curId].receipts[0])") == [True, 1] and page.evaluate("window.__up.length") == 2)
        page.click("button.rc >> nth=0"); check("the receipt notes that 2 pages are stored", "2 pages are stored" in page.inner_text("#sheet")); page.click("[data-act=close]")
        page.click("[data-act=pdf]"); page.wait_for_function("(window.__saved || {})['%s.pdf']" % n6, timeout=30000)
        rd = pypdf.PdfReader(io.BytesIO(saved(page, n6 + ".pdf"))); t = "\n".join(p.extract_text() for p in rd.pages)
        open(os.path.join(OUT, "pdfreceipt.pdf"), "wb").write(saved(page, n6 + ".pdf"))
        check("report PDF attaches both pages, each labelled", len(rd.pages) == 3 and len({len(im.data) for im in rd.pages[1].images}) == 2 and "(page 1 of 2)" in t and "(page 2 of 2)" in t, len(rd.pages))
        bk = page.evaluate("ExpenseApp.buildBackup().then(b => b.text()).then(t => Object.keys(JSON.parse(t).images).length)")
        check("backup includes the extra page", bk == 3, bk)
        page.click("button.rc >> nth=0"); page.click("[data-act=del-rc]"); page.click("[data-act=del-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("deleting the receipt removes both stored pages", page.evaluate("window.__del.length") == 2)
        page.set_input_files("#doc", os.path.join(OUT, "broken.pdf")); page.wait_for_selector("#sheet .note.bad")
        check("a damaged PDF gives a clear message", "could not be opened" in page.inner_text("#sheet .note.bad")); page.click("[data-act=close]")
        page.set_input_files("#doc", os.path.join(OUT, "receipt.jpg"))
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'")
        check("the file button also takes an ordinary photo", page.evaluate("window.__imgs") == 1); page.click("[data-act=close]")
        page.click("[data-act=del-report]"); page.click("[data-act=del-report]"); page.wait_for_selector("ul.list")

        # arithmetic unit checks through the page's own functions
        u = page.evaluate("""(() => { const A = ExpenseApp, r = {currency: 'IDR', project: ''};
          const a = A.normalise({merchant: 'Warung', currency: 'IDR', total: '1.250.000', subtotal: '1.000.000', code: '6130', items: [{d: 'Bintang', a: '200.000', alc: true}, {d: 'Nasi', a: '800.000'}]}, r);
          const b = A.normalise({merchant: 'X', total: 50, code: '9999', currency: 'usd$'}, {currency: 'SGD'});
          const c = A.calc({currency: 'SGD', receipts: [{date: '2026-01-02', code: '6130', currency: 'SGD', total: 10.005, alcohol: 0}, {date: '2026-01-01', code: '6130', currency: 'JPY', total: 1000, alcohol: 1000, rate: 0.0087}]});
          return {a: [a.total, a.alcohol, a.currency], b: [b.currency, b.code, b.alcohol], c: [c.total, c.rows.map(x => x.code + ':' + x.base), c.rows[0].rc.currency]}; })()""")
        check("string amounts with thousand dots parse; alcohol share scales with tax", u["a"] == [1250000, 250000, "IDR"], u["a"])
        check("unknown code is left empty, currency cleaned", u["b"] == ["USD", "", 0], u["b"])
        check("rounding, date order and an all-alcohol receipt", u["c"] == [18.71, ["6220:8.7", "6130:10.01"], "JPY"], u["c"])
        ctx.close()

        # ---------- B. installed version, outside Claude: on-device storage, own API key ----------
        KEY = "sk-ant-test-0123456789abcdef"; api = {"calls": [], "status": 200}

        def anthropic(route):
            rq = route.request; cors = {"access-control-allow-origin": "*", "access-control-allow-headers": "*", "access-control-allow-methods": "POST, OPTIONS"}
            if rq.method == "OPTIONS":
                return route.fulfill(status=204, headers=cors)
            body = json.loads(rq.post_data or "{}"); api["calls"].append({"h": rq.headers, "b": body})
            if api["status"] != 200:
                return route.fulfill(status=api["status"], headers=cors, content_type="application/json", body=json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
            route.fulfill(status=200, headers=cors, content_type="application/json", body=json.dumps({"content": [{"type": "text", "text": "Here is the result.\n```json\n" + json.dumps(REPLY) + "\n```"}]}))

        ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, accept_downloads=True, service_workers="block")
        ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort()); ctx.route("https://api.anthropic.com/**", anthropic)
        ctx.add_init_script("navigator.canShare = d => !!(d && d.files && d.files.length); navigator.share = async d => { window.__shared = d.files[0].name + '|' + d.files[0].size; };")
        page = ctx.new_page(); page.on("pageerror", lambda e: errors.append("pageerror(B): " + str(e)))
        page.on("console", lambda m: errors.append("B: " + m.text) if m.type == "error" and "ERR_FAILED" not in m.text and "401" not in m.text else None)
        page.goto(url); page.wait_for_selector("text=No reports yet")
        check("installed version stores on the device and says so", page.evaluate("[ExpenseApp.store.mode, ExpenseApp.store.idb]") == ["local", True] and "on this device only" in text(page))
        new_report(page, "AEI", "Local test")
        check("without a key, the report says where the key is added", "API key, which is added under Setup" in text(page))
        page.set_input_files("#lib", os.path.join(OUT, "receipt.jpg")); page.wait_for_selector("#sheet .note.bad")
        check("photo without a key: message, no API call, fields open", "API key" in page.inner_text("#sheet .note.bad") and not api["calls"] and not page.is_disabled("#f-merchant"))
        page.fill("#f-merchant", "Stationer"); page.select_option("#f-code", "6300"); page.fill("#f-total", "12.3"); page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("hand-filled receipt keeps its photo on the device", page.evaluate("ExpenseApp.S.reports[ExpenseApp.S.curId].receipts[0].img.startsWith('l:')"))

        page.click("#nav-setup"); page.wait_for_selector("#k-key")
        page.fill("#k-key", "not-a-key"); page.click("[data-act=save-key]")
        check("a malformed key is refused", "does not look like" in page.inner_text("#toast") and page.evaluate("localStorage.getItem('xp.key')") is None)
        page.fill("#k-key", KEY); page.select_option("#k-model", "claude-haiku-5-5"); page.click("[data-act=save-key]"); page.wait_for_function("document.querySelector('#toast').textContent.includes('saved on this device')")
        check("key and model are saved on the device and the key is not shown again", page.evaluate("[localStorage.getItem('xp.key'), localStorage.getItem('xp.model')]") == [KEY, "claude-haiku-5-5"] and page.input_value("#k-key") == "" and KEY not in page.content())
        page.reload(); page.wait_for_selector("#k-key")
        check("after a restart the key is still in place", "through the saved API key" in text(page) and page.evaluate("ExpenseApp.cap.model") == "claude-haiku-5-5")

        page.click("#nav-reports"); page.click("button.rep >> nth=0"); page.wait_for_selector("h1.num")
        page.set_input_files("#lib", os.path.join(OUT, "receipt.jpg"))
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'")
        c = api["calls"][-1]; img = c["b"]["messages"][0]["content"][0]
        check("API request: key header, browser-access header, version, model", c["h"].get("x-api-key") == KEY and c["h"].get("anthropic-dangerous-direct-browser-access") == "true" and c["h"].get("anthropic-version") == "2023-06-01" and c["b"]["model"] == "claude-haiku-5-5", {k: v for k, v in c["h"].items() if k.startswith("anthropic")})
        check("API request carries the photo as a JPEG and the reading prompt", img["type"] == "image" and img["source"]["media_type"] == "image/jpeg" and base64.b64decode(img["source"]["data"])[:2] == b"\xff\xd8" and "6200 Entertainment: Client" in c["b"]["messages"][0]["content"][1]["text"])
        check("reply wrapped in prose and a code fence is still parsed", page.input_value("#f-total") == "151.07" and page.input_value("#f-alc") == "57.55")
        page.click("#f-quick button[data-code='6200']"); page.click("[data-act=save-rc]"); page.wait_for_selector("#sheet", state="hidden")
        check("standalone report total", "SGD 163.37" in page.inner_text(".total"), page.inner_text(".total"))
        api["status"] = 401
        page.set_input_files("#lib", os.path.join(OUT, "receipt.jpg")); page.wait_for_selector("[data-act=retry-read]")
        check("a rejected key gives a clear message", "key was not accepted" in page.inner_text("#sheet"))
        api["status"] = 200; page.click("[data-act=close]")
        page.set_input_files("#doc", os.path.join(OUT, "invoice.pdf"))
        page.wait_for_function("document.querySelector('#f-merchant') && document.querySelector('#f-merchant').value === 'Harbour Lane Kitchen'", timeout=30000)
        blocks = api["calls"][-1]["b"]["messages"][0]["content"]
        check("installed version: a 2-page PDF goes to the API as 2 images plus the prompt", [b["type"] for b in blocks] == ["image", "image", "text"], [b["type"] for b in blocks])
        page.click("[data-act=close]")

        page.reload(); page.wait_for_selector("h1.num")
        page.wait_for_function("[...document.querySelectorAll('img.th')].length === 2 && [...document.querySelectorAll('img.th')].every(i => i.naturalWidth > 0)")
        check("after a restart, reports and both photos come back from the device", "SGD 163.37" in page.inner_text(".total"))
        page.click("[data-act=pdf]"); page.wait_for_selector("[data-act=share-file]")
        check("export opens a file-ready step with share and download", "AEI-%d-0001.pdf" % YEAR in page.inner_text("#sheet"))
        page.click("[data-act=share-file]"); page.wait_for_function("window.__shared")
        check("share hands over the PDF file", page.evaluate("window.__shared").startswith("AEI-%d-0001.pdf|" % YEAR))
        with page.expect_download() as dl:
            page.click("#sheet a[download]")
        pth = os.path.join(OUT, "standalone.pdf"); dl.value.save_as(pth); rd = pypdf.PdfReader(pth)
        check("downloaded PDF has both receipt photos", len(rd.pages) == 3 and all(len(list(rd.pages[i].images)) == 1 for i in (1, 2)), len(rd.pages))
        page.click("[data-act=close]")

        # backup, wipe, restore
        page.click("#nav-setup"); page.wait_for_selector("[data-act=backup]"); page.click("[data-act=backup]"); page.wait_for_selector("#sheet a[download]")
        with page.expect_download() as dl:
            page.click("#sheet a[download]")
        bpath = os.path.join(OUT, dl.value.suggested_filename); dl.value.save_as(bpath); bk = json.load(open(bpath))
        check("backup file holds setup, the report and both photos, and no key", bk["app"] == "expense-reports" and len(bk["reports"]) == 1 and len(bk["images"]) == 2 and KEY not in open(bpath).read(), [len(bk["reports"]), len(bk["images"])])
        page.click("[data-act=close]"); page.click("#nav-reports"); page.click("button.rep >> nth=0"); page.wait_for_selector("h1.num")
        page.click("[data-act=del-report]"); page.click("[data-act=del-report]"); page.wait_for_selector("text=No reports yet")
        page.reload(); page.wait_for_selector("text=No reports yet")
        page.click("#nav-setup"); page.wait_for_selector("label[for=restore]"); page.set_input_files("#restore", bpath); page.wait_for_selector("[data-act=do-restore]")
        check("restore asks before replacing and names what the file holds", "1 report" in page.inner_text("#sheet") and "2 receipt photos" in page.inner_text("#sheet"))
        page.click("[data-act=do-restore]"); page.wait_for_selector("ul.list"); page.reload(); page.wait_for_selector("ul.list")
        page.click("button.rep >> nth=0"); page.wait_for_selector("h1.num")
        page.wait_for_function("[...document.querySelectorAll('img.th')].length === 2 && [...document.querySelectorAll('img.th')].every(i => i.naturalWidth > 0)")
        check("restored report shows its total and photos after a restart", "SGD 163.37" in page.inner_text(".total"))
        page.set_input_files("#restore", os.path.join(OUT, "receipt.jpg")); page.wait_for_function("document.querySelector('#toast').textContent.includes('not a backup')")
        check("a wrong file is refused as a backup", True)

        page.click("#nav-setup"); page.wait_for_selector("[data-act=selftest]"); page.click("[data-act=selftest]")
        page.wait_for_function("document.querySelectorAll('#selftest table').length === 2", timeout=30000); st = page.inner_text("#selftest")
        check("self check in the installed version: 12 correct", st.count("Correct") == 12 and "Failed" not in st and "Differs" not in st, st)
        page.screenshot(path=os.path.join(OUT, "07-standalone-setup.png"), full_page=True)
        page.click("[data-act=del-key]"); page.click("[data-act=del-key]")
        check("the key can be removed from the device", page.evaluate("[localStorage.getItem('xp.key'), ExpenseApp.cap.key]") == [None, ""])
        check("no sideways scroll at 390px on standalone setup", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        ctx.close()

        # ---------- C. install files and offline start ----------
        ctx = br.new_context(viewport={"width": 390, "height": 844}); ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        page = ctx.new_page(); page.goto(url); page.wait_for_selector("text=No reports yet")
        man = page.evaluate("fetch('manifest.webmanifest').then(r => r.json())")
        check("manifest: standalone display, relative start, icons present", man["display"] == "standalone" and man["start_url"] == "./" and all(page.evaluate("u => fetch(u).then(r => r.ok)", i["src"]) for i in man["icons"]) and page.evaluate("fetch('icons/icon-180.png').then(r => r.ok)"))
        page.evaluate("navigator.serviceWorker.ready.then(() => true)"); page.wait_for_timeout(1200)
        check("offline cache holds the app shell, PDF reader included", page.evaluate("caches.keys().then(async ks => { const c = await caches.open(ks[0]); const k = (await c.keys()).map(r => r.url); return k.length >= 8 && k.some(u => u.endsWith('pdf.worker.min.js')); })"))
        ctx.set_offline(True); page.reload(); page.wait_for_selector("text=No reports yet")
        check("the app starts without a network connection, libraries included", page.evaluate("!!(window.jspdf && window.XLSX)"))
        ctx.set_offline(False); ctx.close(); br.close()

    check("no script errors in the browser console", not errors, errors[:5])
    bad = [r for r in results if not r[1]]
    print("\n%d checks, %d passed, %d failed" % (len(results), len(results) - len(bad), len(bad)))
    try:
        subprocess.run(["pdftoppm", "-r", "60", "-png", os.path.join(OUT, "AEI.pdf"), os.path.join(OUT, "pdf")], check=False)
    except Exception:
        pass
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    run()
