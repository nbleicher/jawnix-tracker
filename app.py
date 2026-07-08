#!/usr/bin/env python3
import base64
import hashlib
import hmac
import http.cookies
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime
from html import escape as xml_escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlparse


APP_DIR = Path(os.environ.get("APP_DIR", Path(__file__).resolve().parent))
API_PORT = int(os.environ.get("JAWNIX_API_PORT", "8001"))
TEMPLATE_PATH = Path(
    os.environ.get(
        "JAWNIX_INVOICE_TEMPLATE",
        APP_DIR / "templates" / "Jawnix_Invoice_Template.docx",
    )
)
INVOICE_DIR = Path(os.environ.get("JAWNIX_INVOICE_DIR", APP_DIR / "invoices"))
MAX_BODY_BYTES = int(os.environ.get("JAWNIX_MAX_INVOICE_JSON_BYTES", "1048576"))
AUTH_COOKIE_NAME = os.environ.get("JAWNIX_AUTH_COOKIE_NAME", "jawnix_session")
SESSION_TTL_SECONDS = int(os.environ.get("JAWNIX_SESSION_TTL_SECONDS", "86400"))
STRIPE_API_URL = "https://api.stripe.com/v1/checkout/sessions"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W = f"{{{W_NS}}}"
R = f"{{{R_NS}}}"
XML = f"{{{XML_NS}}}"
ARIAL_FONT_ATTRS = {
    W + "ascii": "Arial",
    W + "hAnsi": "Arial",
    W + "eastAsia": "Arial",
    W + "cs": "Arial",
}


for prefix, uri in {
    "w": W_NS,
    "r": R_NS,
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}.items():
    ET.register_namespace(prefix, uri)


def auth_is_enabled():
    return os.environ.get("JAWNIX_ALLOW_UNPROTECTED", "false").lower() != "true"


def auth_user():
    return os.environ.get("JAWNIX_BASIC_AUTH_USER", "").strip()


def auth_password():
    return os.environ.get("JAWNIX_BASIC_AUTH_PASSWORD", "")


def session_secret():
    configured = os.environ.get("JAWNIX_SESSION_SECRET", "")
    return configured or auth_password() or os.environ.get("JAWNIX_BASIC_AUTH_HASH", "")


def cookie_secure_flag():
    return os.environ.get("JAWNIX_COOKIE_SECURE", "true").lower() != "false"


def sign_session_payload(payload):
    secret = session_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_token():
    expires_at = str(int(time.time()) + SESSION_TTL_SECONDS)
    payload = f"{expires_at}:{token_urlsafe(18)}"
    signature = sign_session_payload(payload)
    raw = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_session_token(token):
    try:
        padded = token + ("=" * (-len(token) % 4))
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def valid_session_token(token):
    decoded = decode_session_token(str(token or ""))
    parts = decoded.split(":")
    if len(parts) != 3:
        return False
    expires_at, nonce, signature = parts
    if not expires_at.isdigit() or not nonce or int(expires_at) < int(time.time()):
        return False
    expected = sign_session_payload(f"{expires_at}:{nonce}")
    return bool(expected) and hmac.compare_digest(signature, expected)


def parse_cookie_header(value):
    cookies = http.cookies.SimpleCookie()
    try:
        cookies.load(value or "")
    except http.cookies.CookieError:
        return {}
    return {key: morsel.value for key, morsel in cookies.items()}


def make_auth_cookie(token, max_age=SESSION_TTL_SECONDS):
    parts = [
        f"{AUTH_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if cookie_secure_flag():
        parts.append("Secure")
    return "; ".join(parts)


def clear_auth_cookie():
    parts = [
        f"{AUTH_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if cookie_secure_flag():
        parts.append("Secure")
    return "; ".join(parts)


def money(value):
    return f"${float(value or 0):,.2f}"


def rate_text(amount, leads):
    if not leads:
        return ""
    return f"${float(amount or 0) / int(leads):,.4f}"


def int_text(value):
    return f"{int(value or 0):,}"


def display_invoice_date(value):
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return text


def safe_filename(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return value.strip("._-") or "invoice"


def required_string(payload, key):
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing required field: {key}")
    return value


def normalize_invoice(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invoice payload must be a JSON object.")

    days = payload.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError("Invoice payload must include at least one day.")

    normalized_days = []
    total_leads = 0
    computed_total = 0.0
    for idx, day in enumerate(days, start=1):
        if not isinstance(day, dict):
            raise ValueError(f"days[{idx}] must be an object.")
        leads = int(day.get("leads", 0))
        amount = float(day.get("amount", 0))
        if leads <= 0:
            raise ValueError(f"days[{idx}].leads must be greater than zero.")
        normalized_days.append(
            {
                "day": str(day.get("day", "")).strip(),
                "date": str(day.get("date", "")).strip(),
                "leads": leads,
                "amount": round(amount, 2),
            }
        )
        total_leads += leads
        computed_total += amount

    total_due = round(computed_total, 2)
    return {
        "invoiceNum": required_string(payload, "invoiceNum"),
        "invoiceDate": required_string(payload, "invoiceDate"),
        "weekRange": required_string(payload, "weekRange"),
        "clientName": required_string(payload, "clientName"),
        "clientEmail": str(payload.get("clientEmail", "")).strip(),
        "days": normalized_days,
        "totalLeads": total_leads,
        "totalDue": total_due,
        "paymentUrl": str(payload.get("paymentUrl", "")).strip(),
        "paymentSessionId": str(payload.get("paymentSessionId", "")).strip(),
    }


def public_base_url():
    configured = os.environ.get("JAWNIX_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return configured or "https://example.com"


def create_stripe_checkout_session(invoice):
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret_key:
        return "", ""

    amount_cents = int(round(float(invoice["totalDue"]) * 100))
    if amount_cents <= 0:
        raise ValueError("Invoice total must be greater than zero to create a Stripe payment link.")

    currency = os.environ.get("STRIPE_CURRENCY", "usd").strip().lower() or "usd"
    base_url = public_base_url()
    product_name = f"Jawnix Invoice {invoice['invoiceNum']}"
    description = f"{invoice['clientName']} - {invoice['weekRange']}"
    form = {
        "mode": "payment",
        "success_url": os.environ.get("STRIPE_SUCCESS_URL", f"{base_url}/?payment=success"),
        "cancel_url": os.environ.get("STRIPE_CANCEL_URL", f"{base_url}/?payment=cancelled"),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][unit_amount]": str(amount_cents),
        "line_items[0][price_data][product_data][name]": product_name,
        "line_items[0][price_data][product_data][description]": description,
        "metadata[invoice_num]": invoice["invoiceNum"],
        "metadata[client_name]": invoice["clientName"],
        "metadata[week_range]": invoice["weekRange"],
    }
    if invoice["clientEmail"]:
        form["customer_email"] = invoice["clientEmail"]

    request = urllib.request.Request(
        STRIPE_API_URL,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except Exception:
            detail = None
        raise RuntimeError(detail or "Stripe Checkout Session creation failed.") from exc

    checkout_url = str(data.get("url", "")).strip()
    checkout_session_id = str(data.get("id", "")).strip()
    if not checkout_url:
        raise RuntimeError("Stripe did not return a Checkout URL.")
    return checkout_url, checkout_session_id


def expire_stripe_checkout_session(session_id):
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("Missing Stripe Checkout Session ID.")

    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret_key:
        raise ValueError("STRIPE_SECRET_KEY is required to void Stripe checkout sessions.")

    quoted_id = urllib.parse.quote(session_id, safe="")
    request = urllib.request.Request(
        f"{STRIPE_API_URL}/{quoted_id}/expire",
        data=b"",
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"Stripe Checkout Session {session_id} could not be expired.") from exc

    return {
        "id": str(data.get("id", session_id)),
        "status": str(data.get("status", "")),
    }


def table_cell(text, align="left", bold=False):
    justification = "" if align == "left" else f'<w:jc w:val="{align}"/>'
    bold_tag = "<w:b/>" if bold else ""
    return (
        "<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/></w:tcPr>"
        f"<w:p><w:pPr>{justification}</w:pPr><w:r><w:rPr><w:rFonts "
        'w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Arial" w:cs="Arial"/>'
        f"{bold_tag}</w:rPr>"
        f"<w:t>{xml_escape(str(text))}</w:t></w:r></w:p></w:tc>"
    )


def invoice_rows(invoice):
    rows = []
    for day in invoice["days"]:
        rows.append(
            "<w:tr>"
            + table_cell(day["day"])
            + table_cell(day["date"])
            + table_cell(int_text(day["leads"]), "right")
            + table_cell(money(day["amount"]), "right")
            + "</w:tr>"
        )
    return "".join(rows)


def payment_link_xml(url):
    if not url:
        return ""
    return (
        "<w:p>"
        '<w:hyperlink r:id="rIdStripePayment">'
        '<w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Arial" w:cs="Arial"/>'
        '<w:rStyle w:val="Hyperlink"/><w:b/><w:color w:val="1F4E79"/><w:u w:val="single"/></w:rPr>'
        "<w:t>Pay now</w:t>"
        "</w:r></w:hyperlink>"
        "</w:p>"
    )


def add_payment_relationship(xml, url):
    if not url:
        return xml
    rel = (
        '<Relationship Id="rIdStripePayment" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{xml_escape(url)}" TargetMode="External"/>'
    )
    if xml.rstrip().endswith("/>"):
        return xml.rstrip()[:-2] + ">" + rel + "</Relationships>"
    return xml.replace("</Relationships>", rel + "</Relationships>")


def add_payment_link_to_document(xml, url):
    link = payment_link_xml(url)
    if not link:
        return xml
    if "xmlns:r=" not in xml:
        xml = xml.replace(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"',
        )
    if "<!-- PAYMENT_LINK -->" in xml:
        return xml.replace("<!-- PAYMENT_LINK -->", link)
    return xml.replace("<w:sectPr", link + "<w:sectPr")


def text_nodes(element):
    return list(element.iter(W + "t"))


def element_text(element):
    return "".join(node.text or "" for node in text_nodes(element))


def ensure_text_node(cell):
    nodes = text_nodes(cell)
    if nodes:
        return nodes[0]

    paragraph = cell.find(W + "p")
    if paragraph is None:
        paragraph = ET.SubElement(cell, W + "p")
    run = paragraph.find(W + "r")
    if run is None:
        run = ET.SubElement(paragraph, W + "r")
    return ET.SubElement(run, W + "t")


def set_element_text(element, value):
    nodes = text_nodes(element)
    if not nodes:
        nodes = [ensure_text_node(element)]

    nodes[0].text = str(value)
    if " " in str(value):
        nodes[0].set(XML + "space", "preserve")
    for node in nodes[1:]:
        node.text = ""


def set_prefixed_paragraph(root, prefix, value, output_prefix=None):
    label = output_prefix or prefix
    for paragraph in root.iter(W + "p"):
        if element_text(paragraph).startswith(prefix):
            nodes = text_nodes(paragraph)
            if len(nodes) >= 2:
                nodes[0].text = label
                nodes[0].set(XML + "space", "preserve")
                nodes[1].text = str(value)
                for node in nodes[2:]:
                    node.text = ""
            else:
                set_element_text(paragraph, f"{label}{value}")
            return True
    return False


def set_exact_paragraph(root, old_text, new_text):
    for paragraph in root.iter(W + "p"):
        if element_text(paragraph) == old_text:
            set_element_text(paragraph, new_text)
            return True
    return False


def table_rows(table):
    return table.findall(W + "tr")


def row_cells(row):
    return row.findall(W + "tc")


def width_attr(element):
    return int(float(element.get(W + "w", "0") or 0))


def cell_properties(cell):
    props = cell.find(W + "tcPr")
    if props is None:
        props = ET.Element(W + "tcPr")
        cell.insert(0, props)
    return props


def set_cell_width(cell, width_twips):
    props = cell_properties(cell)
    width = props.find(W + "tcW")
    if width is None:
        width = ET.Element(W + "tcW")
        props.insert(0, width)
    width.set(W + "w", str(width_twips))
    width.set(W + "type", "dxa")


def set_cell_no_wrap(cell):
    props = cell_properties(cell)
    if props.find(W + "noWrap") is None:
        props.append(ET.Element(W + "noWrap"))


def set_table_grid_min_width(table, column_idx, min_width_twips):
    grid = table.find(W + "tblGrid")
    if grid is None:
        return

    columns = grid.findall(W + "gridCol")
    if column_idx >= len(columns):
        return

    current_width = width_attr(columns[column_idx])
    if current_width >= min_width_twips:
        return

    delta = min_width_twips - current_width
    columns[column_idx].set(W + "w", str(min_width_twips))

    if column_idx > 0:
        donor = columns[0]
        donor_width = width_attr(donor)
        donor.set(W + "w", str(max(0, donor_width - delta)))


def find_table(root, header):
    for table in root.iter(W + "tbl"):
        rows = table_rows(table)
        if not rows:
            continue
        values = [element_text(cell) for cell in row_cells(rows[0])]
        if values == header:
            return table
    return None


def find_table_containing(root, value):
    for table in root.iter(W + "tbl"):
        if value in element_text(table):
            return table
    return None


def set_cell_values(row, values):
    for cell, value in zip(row_cells(row), values):
        set_element_text(cell, value)


def line_description(day):
    period = " ".join(part for part in [day["day"], day["date"]] if part)
    return f"Leads - {period}" if period else "Leads"


def fill_line_items(root, invoice):
    table = find_table(root, ["#", "Description", "Qty", "Rate", "Amount"])
    if table is None:
        return

    rows = table_rows(table)
    if len(rows) < 2:
        return

    body_rows = rows[1:]
    required_rows = max(5, len(invoice["days"]))
    while len(body_rows) < required_rows:
        new_row = deepcopy(body_rows[-1])
        table.append(new_row)
        body_rows.append(new_row)

    for idx, row in enumerate(body_rows, start=1):
        if idx <= len(invoice["days"]):
            day = invoice["days"][idx - 1]
            values = [
                str(idx),
                line_description(day),
                int_text(day["leads"]),
                rate_text(day["amount"], day["leads"]),
                money(day["amount"]),
            ]
        else:
            values = ["", "", "", "", ""]
        set_cell_values(row, values)


def fill_totals(root, invoice):
    table = find_table_containing(root, "TOTAL DUE")
    if table is None:
        return

    set_table_grid_min_width(table, 2, 1800)
    total = money(invoice["totalDue"])
    for row in table_rows(table):
        cells = row_cells(row)
        if len(cells) >= 3 and element_text(cells[1]) in {"Subtotal", "TOTAL DUE"}:
            set_cell_width(cells[2], 1800)
            set_cell_no_wrap(cells[2])
            set_element_text(cells[2], total)


def payment_url_text(url):
    return "PAY NOW" if url else "Payment link unavailable"


def add_run_property(parent, tag, attrs=None):
    return ET.SubElement(parent, W + tag, attrs or {})


def run_properties(run):
    props = run.find(W + "rPr")
    if props is None:
        props = ET.Element(W + "rPr")
        run.insert(0, props)
    return props


def force_run_font(run, font_name="Arial"):
    props = run_properties(run)
    fonts = props.find(W + "rFonts")
    if fonts is None:
        fonts = ET.Element(W + "rFonts")
        props.insert(0, fonts)
    for attr in (W + "ascii", W + "hAnsi", W + "eastAsia", W + "cs"):
        fonts.set(attr, font_name)


def force_document_font(root, font_name="Arial"):
    for run in root.iter(W + "r"):
        force_run_font(run, font_name)


def fill_payment_link(root, url):
    for paragraph in root.iter(W + "p"):
        if element_text(paragraph).startswith("Pay Online:"):
            children = list(paragraph)
            for child in children:
                if child.tag != W + "pPr":
                    paragraph.remove(child)

            label_run = ET.SubElement(paragraph, W + "r")
            force_run_font(label_run)
            label_text = ET.SubElement(label_run, W + "t")
            label_text.set(XML + "space", "preserve")
            label_text.text = "Pay Online: "

            if url:
                hyperlink = ET.SubElement(paragraph, W + "hyperlink", {R + "id": "rIdStripePayment"})
                run = ET.SubElement(hyperlink, W + "r")
                run_props = ET.SubElement(run, W + "rPr")
                fonts = ET.SubElement(run_props, W + "rFonts")
                for attr, value in ARIAL_FONT_ATTRS.items():
                    fonts.set(attr, value)
                add_run_property(run_props, "b")
                add_run_property(run_props, "color", {W + "val": "1F4E79"})
                add_run_property(run_props, "u", {W + "val": "single"})
                text = ET.SubElement(run, W + "t")
                text.text = "Pay now"
            else:
                run = ET.SubElement(paragraph, W + "r")
                force_run_font(run)
                text = ET.SubElement(run, W + "t")
                text.text = payment_url_text(url)
            return


def render_attached_template_document(content, invoice):
    root = ET.fromstring(content)
    set_prefixed_paragraph(root, "Invoice #: ", invoice["invoiceNum"])
    set_prefixed_paragraph(root, "Date: ", display_invoice_date(invoice["invoiceDate"]))
    set_exact_paragraph(root, "[Client / Lead Name]", invoice["clientName"])
    set_exact_paragraph(root, "[Email]", invoice["clientEmail"])
    set_prefixed_paragraph(
        root,
        "PO Number: ",
        invoice["weekRange"],
        output_prefix="Billing Period: ",
    )
    fill_line_items(root, invoice)
    fill_totals(root, invoice)
    fill_payment_link(root, invoice["paymentUrl"])
    force_document_font(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render_legacy_template_document(content, invoice):
    replacements = {
        "{{invoiceNum}}": invoice["invoiceNum"],
        "{{invoiceDate}}": invoice["invoiceDate"],
        "{{weekRange}}": invoice["weekRange"],
        "{{clientName}}": invoice["clientName"],
        "{{clientEmail}}": invoice["clientEmail"],
        "{{totalLeads}}": int_text(invoice["totalLeads"]),
        "{{totalDue}}": money(invoice["totalDue"]),
    }
    text = content.decode("utf-8")
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, xml_escape(value))
    text = text.replace("<!-- LINE_ITEMS_ROWS -->", invoice_rows(invoice))
    text = add_payment_link_to_document(text, invoice["paymentUrl"])
    return text.encode("utf-8")


def render_docx(invoice, output_path):
    with zipfile.ZipFile(TEMPLATE_PATH, "r") as src, zipfile.ZipFile(
        output_path, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "word/document.xml":
                if b"{{invoiceNum}}" in content:
                    content = render_legacy_template_document(content, invoice)
                else:
                    content = render_attached_template_document(content, invoice)
            elif item.filename == "word/_rels/document.xml.rels":
                text = content.decode("utf-8")
                content = add_payment_relationship(text, invoice["paymentUrl"]).encode("utf-8")
            dst.writestr(item, content)


def convert_docx_to_pdf(docx_path, output_dir):
    env = os.environ.copy()
    env.setdefault("HOME", str(output_dir))
    result = subprocess.run(
        [
            "libreoffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(docx_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "LibreOffice conversion failed.").strip()
        raise RuntimeError(detail)

    pdf_path = output_dir / (docx_path.stem + ".pdf")
    if not pdf_path.exists():
        raise RuntimeError("LibreOffice did not produce a PDF.")
    return pdf_path


def build_invoice_pdf(payload):
    invoice = normalize_invoice(payload)
    if not invoice["paymentUrl"]:
        invoice["paymentUrl"], invoice["paymentSessionId"] = create_stripe_checkout_session(invoice)
    INVOICE_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    stem = safe_filename(
        f"{invoice['invoiceNum']}_{invoice['clientName']}_{invoice['weekRange']}_{stamp}"
    )
    final_docx = INVOICE_DIR / f"{stem}.docx"
    final_pdf = INVOICE_DIR / f"{stem}.pdf"

    with tempfile.TemporaryDirectory(prefix="jawnix-invoice-") as tmp:
        tmp_dir = Path(tmp)
        tmp_docx = tmp_dir / f"{stem}.docx"
        render_docx(invoice, tmp_docx)
        tmp_pdf = convert_docx_to_pdf(tmp_docx, tmp_dir)
        shutil.copy2(tmp_docx, final_docx)
        shutil.copy2(tmp_pdf, final_pdf)

    return final_pdf, invoice


class Handler(BaseHTTPRequestHandler):
    server_version = "JawnixInvoiceAPI/1.0"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", os.environ.get("JAWNIX_CORS_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Content-Disposition, X-Stripe-Checkout-Url, X-Stripe-Checkout-Session-Id",
        )
        self.send_header("Vary", "Origin")
        super().end_headers()

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, status, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def has_valid_session(self):
        if not auth_is_enabled():
            return True
        cookies = parse_cookie_header(self.headers.get("Cookie", ""))
        return valid_session_token(cookies.get(AUTH_COOKIE_NAME, ""))

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/auth/check":
            if self.has_valid_session():
                self.send_empty(HTTPStatus.NO_CONTENT)
            else:
                redirect_target = self.headers.get("X-Jawnix-Auth-Redirect", "")
                if redirect_target.startswith("/") and not redirect_target.startswith("//"):
                    self.send_empty(
                        HTTPStatus.SEE_OTHER,
                        {
                            "Location": redirect_target,
                            "Set-Cookie": clear_auth_cookie(),
                        },
                    )
                    return
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Login required."})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            self.handle_login()
            return
        if path == "/api/auth/logout":
            self.send_json(
                HTTPStatus.OK,
                {"ok": True},
                {"Set-Cookie": clear_auth_cookie()},
            )
            return
        if path == "/api/generate-invoice":
            self.handle_generate_invoice()
            return
        if path == "/api/void-invoice":
            self.handle_void_invoice()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.")

        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Invalid request size.")

        return json.loads(self.rfile.read(length).decode("utf-8"))

    def handle_login(self):
        if not auth_is_enabled():
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        configured_user = auth_user()
        configured_password = auth_password()
        if not configured_user or not configured_password:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Login is not configured on the server."},
            )
            return

        try:
            payload = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON."})
            return
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request size."})
            return

        submitted_user = str(payload.get("username", "")).strip()
        submitted_password = str(payload.get("password", ""))
        user_ok = hmac.compare_digest(submitted_user, configured_user)
        password_ok = hmac.compare_digest(submitted_password, configured_password)
        if not (user_ok and password_ok):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid username or password."})
            return

        token = make_session_token()
        self.send_json(
            HTTPStatus.OK,
            {"ok": True},
            {"Set-Cookie": make_auth_cookie(token)},
        )

    def handle_void_invoice(self):
        try:
            payload = self.read_json_body()
            session_ids = payload.get("checkoutSessionIds", [])
            if not isinstance(session_ids, list):
                raise ValueError("checkoutSessionIds must be an array.")
            expired = []
            errors = []
            for session_id in session_ids:
                try:
                    expired.append(expire_stripe_checkout_session(session_id))
                except Exception as exc:
                    errors.append({"id": str(session_id), "error": str(exc)})
            status = HTTPStatus.MULTI_STATUS if errors else HTTPStatus.OK
            self.send_json(status, {"expired": expired, "errors": errors})
        except json.JSONDecodeError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON."})
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_generate_invoice(self):
        if urlparse(self.path).path != "/api/generate-invoice":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self.read_json_body()
        except json.JSONDecodeError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON."})
            return
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request size."})
            return

        try:
            pdf_path, invoice = build_invoice_pdf(payload)
            data = pdf_path.read_bytes()
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except subprocess.TimeoutExpired:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "LibreOffice timed out."})
            return
        except Exception as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{pdf_path.name}"')
        if invoice["paymentUrl"]:
            self.send_header("X-Stripe-Checkout-Url", invoice["paymentUrl"])
        if invoice["paymentSessionId"]:
            self.send_header("X-Stripe-Checkout-Session-Id", invoice["paymentSessionId"])
        self.end_headers()
        self.wfile.write(data)


def main():
    if not TEMPLATE_PATH.exists():
        raise SystemExit(f"Missing invoice template: {TEMPLATE_PATH}")
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), Handler)
    print(f"Invoice API listening on 0.0.0.0:{API_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
