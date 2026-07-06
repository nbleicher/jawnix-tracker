#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from html import escape as xml_escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
STRIPE_API_URL = "https://api.stripe.com/v1/checkout/sessions"


def money(value):
    return f"${float(value or 0):,.2f}"


def int_text(value):
    return f"{int(value or 0):,}"


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
    }


def public_base_url():
    configured = os.environ.get("JAWNIX_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return configured or "https://example.com"


def create_stripe_checkout_session(invoice):
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not secret_key:
        return ""

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
    if not checkout_url:
        raise RuntimeError("Stripe did not return a Checkout URL.")
    return checkout_url


def table_cell(text, align="left", bold=False):
    justification = "" if align == "left" else f'<w:jc w:val="{align}"/>'
    bold_tag = "<w:b/>" if bold else ""
    return (
        "<w:tc><w:tcPr><w:tcW w:w=\"0\" w:type=\"auto\"/></w:tcPr>"
        f"<w:p><w:pPr>{justification}</w:pPr><w:r><w:rPr>{bold_tag}</w:rPr>"
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
        '<w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
        "<w:t>Click here to pay securely with Stripe</w:t>"
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


def render_docx(invoice, output_path):
    replacements = {
        "{{invoiceNum}}": invoice["invoiceNum"],
        "{{invoiceDate}}": invoice["invoiceDate"],
        "{{weekRange}}": invoice["weekRange"],
        "{{clientName}}": invoice["clientName"],
        "{{clientEmail}}": invoice["clientEmail"],
        "{{totalLeads}}": int_text(invoice["totalLeads"]),
        "{{totalDue}}": money(invoice["totalDue"]),
    }

    with zipfile.ZipFile(TEMPLATE_PATH, "r") as src, zipfile.ZipFile(
        output_path, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "word/document.xml":
                text = content.decode("utf-8")
                for placeholder, value in replacements.items():
                    text = text.replace(placeholder, xml_escape(value))
                text = text.replace("<!-- LINE_ITEMS_ROWS -->", invoice_rows(invoice))
                text = add_payment_link_to_document(text, invoice["paymentUrl"])
                content = text.encode("utf-8")
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
    invoice["paymentUrl"] = invoice["paymentUrl"] or create_stripe_checkout_session(invoice)
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
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition, X-Stripe-Checkout-Url")
        self.send_header("Vary", "Origin")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):
        if urlparse(self.path).path == "/api/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if urlparse(self.path).path != "/api/generate-invoice":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length."})
            return

        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Invalid request size."})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
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
