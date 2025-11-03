import os
import io
import re
import uuid
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont, ImageFilter , ImageOps , ImageEnhance
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler, 
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv 
import pytesseract
import os
import asyncio
import aiohttp
import cv2
import numpy as np
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ------------------ ENV ------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
TELEBIRR_NUMBER = os.getenv("TELEBIRR_NUMBER")
FIREBASE_DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("FIREBASE_DATABASE_URL")
ASK_USER_ID, ASK_MESSAGE = range(2)

# ------------------ Firebase ------------------
import json
cred_data = json.loads(os.getenv("FIREBASE_CRED_JSON"))
cred = credentials.Certificate(cred_data)
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_DATABASE_URL
})


# ------------------ Paths ------------------
os.makedirs("pdfs", exist_ok=True)
os.makedirs("outputs", exist_ok=True)

TEMPLATE_PATH = "LAST.png"
FONT_PATH = "NotoSansEthiopic-ExtraBold.ttf"
GETME_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe"
# ------------------ Helpers ------------------
def is_user_allowed(user_id):
    ref = db.reference(f'users/{user_id}')
    user = ref.get()
    return user and user.get("allow") is True
def is_user_a4(user_id):
    ref = db.reference(f'users/{user_id}')
    user = ref.get()
    return (
        user
        and user.get("a4") is True
        and (user.get("allow") is True or (user.get("package", 0) > 0))
    )

def is_user_black(user_id):
    ref = db.reference(f'users/{user_id}')
    user = ref.get()
    return user and user.get("black") is True
def store_pdf(user_id, file_path, original_name):
    pdf_id = str(uuid.uuid4())
    new_filename = f"{pdf_id}_{original_name}"   # keep original name with ID prefix
    new_path = os.path.join("pdfs", new_filename)

    os.rename(file_path, new_path)  # move/rename the file

    db.reference(f'pdfs/{pdf_id}').set({
        'user_id': user_id,
        'file_path': new_path,
        'status': 'pending',
        'allow': False
    })
    return pdf_id

from datetime import datetime, timedelta
import calendar
def preprocess_and_ocr(barcode_img):
    """
    Takes a PIL image or OpenCV image, converts to black & white, 
    then performs OCR to extract text.
    """
    try:
        # Convert PIL image to OpenCV format (NumPy array)
        if isinstance(barcode_img, Image.Image):
            barcode_img = cv2.cvtColor(np.array(barcode_img), cv2.COLOR_RGB2BGR)

        # Convert to grayscale
        gray = cv2.cvtColor(barcode_img, cv2.COLOR_BGR2GRAY)

        # Apply binary threshold (Otsu’s method for dynamic adjustment)
        _, bw_img = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

        # Invert colors: make text/barcode black and background white
        bw_img = cv2.bitwise_not(bw_img)

        # Run OCR on the processed image
        ocr_text = pytesseract.image_to_string(bw_img, lang="eng")

        return ocr_text.strip()

    except Exception as e:
        print(f"❌ Error during OCR preprocessing: {e}")
        return 
def adjust_expiry(year: int, month: int, day: int) -> (int, int, int):
    """
    Add +8 years, and subtract 2 days safely.
    Returns adjusted (year, month, day).
    """
    # Add 8 years first
    year += 8

    # Handle day-2 safely
    if day > 2:
        day -= 2
    else:
        # Need to go back to previous month
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        day = calendar.monthrange(year, month)[1] - (2 - day)
    print("changed date ")
    return year, month, day
import asyncio

async def delayed_cleanup(paths, delay=10):  # 300 sec = 5 min
    await asyncio.sleep(delay)
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"Deleted {path}")
        except Exception as e:
            print(f"Cleanup error: {e}")
def add_demo_watermark(image_path, output_path):
    """Overlay multiple DEMO watermarks on the given image."""
    img = Image.open(image_path).convert("RGBA")
    watermark = Image.new("RGBA", img.size, (0, 0, 0, 0))

    draw = ImageDraw.Draw(watermark)
    font_size = int(min(img.size) / 5)  # smaller font so 5 fit nicely
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except:
        font = ImageFont.load_default()

    text = "DEMO"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # ✅ Place 5 DEMO texts evenly across the diagonal
    for i in range(5):
        x = int((img.size[0] / 6) * (i + 1) - text_width // 2)
        y = int((img.size[1] / 6) * (i + 1) - text_height // 2)
        draw.text((x, y), text, font=font, fill=(255, 0, 0, 200))

    # ✅ Rotate watermark but keep same canvas size
    watermark = watermark.rotate(30, expand=0)

    # ✅ Ensure same size before compositing
    watermark = watermark.resize(img.size)

    watermarked = Image.alpha_composite(img, watermark)
    watermarked.convert("RGB").save(output_path, "PNG")

def _clean_gc(s):
    # remove amharic letters, tidy slashes and spaces, try to find "YYYY/Mon/DD"
    s = re.sub(r'[\u1200-\u137F]+', '', s)
    s = re.sub(r'\s*/\s*', '/', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'[^0-9A-Za-z/ ]', '', s)
    m = re.search(r'(\d{4}/[A-Za-z]{3}/\d{1,2})', s)
    if m:
        return m.group(1).replace(' ', '')
    # fallback: compact any year/month/day present
    m2 = re.search(r'(\d{4}/\s*[A-Za-z]{3}/\s*\d{1,2})', s)
    if m2:
        return re.sub(r'\s*/\s*', '/', m2.group(1))
    return None

def _build_ec_from_text(left_snip):
    # remove amharic letters only for the date area, keep numeric groups
    s = re.sub(r'[\u1200-\u137F]+', ' ', left_snip)
    nums = re.findall(r'\d+', s)
    if not nums:
        return None

    # heuristics to build a 4-digit year and get month/day
    y = m = d = None
    if len(nums) >= 3:
        if len(nums[0]) == 4:
            y, m, d = nums[0], nums[1], nums[2]
        elif len(nums[0]) == 2 and len(nums[1]) == 2:
            # e.g. "20 17 / 12 / 22" -> year = "20"+"17" -> 2017
            y = nums[0] + nums[1]
            m = nums[2]
            d = nums[3] if len(nums) > 3 else None
        elif len(nums[0]) == 1 and len(nums[1]) == 2:
            # e.g. "2 18 / 1 / 14" -> interpret as 2018
            y = '20' + nums[1]
            m = nums[2] if len(nums) > 2 else None
            d = nums[3] if len(nums) > 3 else None
        else:
            # fallback: take first three numeric tokens
            y, m, d = nums[0], nums[1], nums[2]
    else:
        return None

    try:
        y = str(int(y))
        m = f"{int(m):02d}"
        d = f"{int(d):02d}"
        return f"{y}/{m}/{d}"
    except Exception:
        return None
import pytesseract
import re

def normalize_months(text: str) -> str:
    """
    Cleans OCR text for months (handles repetition, mixed digits/letters, spacing, etc.)
    Returns normalized text with correct month names (Jan, Feb, Mar, ... Dec).
    """

    # 1️⃣ Normalize case and remove extra spaces
    text = text.lower().strip()

    # 2️⃣ Replace common OCR digit-letter confusions
    text = (text
            .replace('0', 'o')
            .replace('1', 'l')
            .replace('5', 's')
            .replace('8', 'b')
            .replace('6', 'g')
            .replace('4', 'a'))

    # 3️⃣ Remove stray punctuation except /
    text = re.sub(r'[^a-z0-9/]', '', text)

    # 4️⃣ Collapse repeated letters (ooct → oct, maaay → may)
    text = re.sub(r'(.)\1{1,}', r'\1', text)

    # 5️⃣ Fix corrupted month names
    month_map = {
        'jan': ['ian', 'jnn', 'jqn'],
        'feb': ['fe6', 'fcb', 'fep', 'fer'],
        'mar': ['mqr', 'ma7', 'marc'],
        'apr': ['aprl', 'apri', 'aprl1', 'appr'],
        'may': ['m4y', 'maay'],
        'jun': ['juin', 'jnn', 'jun3', 'jn'],
        'jul': ['ju1', 'juIy', 'ju7y', 'juiy'],
        'aug': ['au9', 'augus', 'aue', 'au8'],
        'sep': ['sept', '5ep', 'se9'],
        'oct': ['o0t', '0ct', 'oet', 'octt', '0ct0', 'oc', 'oot'],
        'nov': ['n0v', 'noV', 'n0b', 'novv'],
        'dec': ['deC', 'd3c', 'de0', 'decem']
    }

    # Replace each variant
    for clean, variants in month_map.items():
        for v in variants:
            text = re.sub(v, clean, text, flags=re.I)

    # 6️⃣ Final cleanup: ensure consistent month capitalization
    for m in ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]:
        text = re.sub(m.lower(), m, text, flags=re.I)

    # 7️⃣ Remove stray slashes at ends or duplicates
    text = re.sub(r'/+', '/', text)
    text = re.sub(r'^/|/$', '', text)

    return text

def extract_id_data(pdf_path):
    doc = fitz.open(pdf_path)
    page = doc[0]
    text = page.get_text("text")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    data = {}

    # --- DOB (EC + GC) ---
    dob_matches = re.findall(r"\d{2}/\d{2}/\d{4}|\d{4}/\d{2}/\d{2}", text)
    if len(dob_matches) >= 2:
        data["dob_ec"] = dob_matches[0]
        data["dob_gc"] = dob_matches[1]
    else:
        data["dob_ec"] = data["dob_gc"] = ""

    # --- Sex ---
    data["sex"] = "Male" if "Male" in text else ("Female" if "Female" in text else "")
    data["sex_am"] = "ወንድ" if data["sex"] == "Male" else "ሴት"

    # --- Phone ---
    phone_match = re.search(r"09\d{8}", text)
    data["phone"] = phone_match.group(0) if phone_match else ""

    # --- Address (region / subcity / woreda) ---
    try:
        phone_idx = lines.index(data["phone"])
        data["region_am"]   = lines[phone_idx + 1]
        data["region_en"]   = lines[phone_idx + 2]
        data["subcity_am"]  = lines[phone_idx + 3]
        data["subcity_en"]  = lines[phone_idx + 4]
        data["woreda_am"]   = lines[phone_idx + 5]
        data["woreda_en"]   = lines[phone_idx + 6]
    except:
        data.update({
            "region_am": "", "region_en": "",
            "subcity_am": "", "subcity_en": "",
            "woreda_am": "", "woreda_en": ""
        })

    # --- Names (last lines) ---
    data["name_am"] = lines[-2] if len(lines) >= 2 else ""
    data["name_en"] = lines[-1] if len(lines) >= 1 else ""

    # ------------------ Extract images ------------------
    images = {}
    right_images = []
    for img_index, img in enumerate(page.get_images(full=True)):
        xref = img[0]
        bbox = page.get_image_bbox(img)
        base_image = doc.extract_image(xref)
        img_obj = Image.open(io.BytesIO(base_image["image"])).convert("RGB")

        if bbox.x0 > page.rect.width / 2:
            right_images.append((bbox, img_obj))
        else:
            images[f"img_{img_index}"] = img_obj

    right_images.sort(key=lambda tup: tup[0].y0)

    # --- FIN & Barcode ---
    if len(right_images) >= 2:
        fin_img = right_images[1][1]
        barcode_img = right_images[0][1]

        # Gregorian <-> JDN

        def gregorian_to_jdn(year: int, month: int, day: int) -> int:
            a = (14 - month) // 12
            y = year + 4800 - a
            m = month + 12 * a - 3
            jdn = day + ((153 * m + 2) // 5) + 365 * y + y // 4 - y // 100 + y // 400 - 32045
            return jdn

        def jdn_to_gregorian(jdn: int) -> datetime.date:
            j = jdn + 32044
            g = j // 146097
            dg = j % 146097
            c = (dg // 36524 + 1) * 3 // 4
            dc = dg - c * 36524
            b = dc // 1461
            db = dc % 1461
            a = (db // 365 + 1) * 3 // 4
            da = db - a * 365
            y = g * 400 + c * 100 + b * 4 + a
            m = (da * 5 + 308) // 153 - 2
            d = da - (m + 4) * 153 // 5 + 122
            year = y - 4800 + (m + 2) // 12
            month = (m + 2) % 12 + 1
            day = d + 1
            return datetime(year, month, day).date()

        # ===============================
        # Ethiopian <-> JDN
        # ===============================

        ETHIOPIAN_EPOCH = 1724220

        def eth_to_jdn(year: int, month: int, day: int) -> int:
            return day + 30 * (month - 1) + 365 * (year - 1) + ((year - 1) // 4) + ETHIOPIAN_EPOCH

        def jdn_to_eth(jdn: int) -> (int, int, int):
            r = jdn - ETHIOPIAN_EPOCH - 1
            year = (4 * r + 1463) // 1461
            doy = r - (365 * (year - 1) + (year - 1) // 4)
            month = doy // 30 + 1
            day = doy % 30 + 1
            return int(year), int(month), int(day)

        # ===============================
        # Cleaners
        # ===============================

        def normalize_months(text: str) -> str:
            if not text:
                return ""
            s = text.lower().strip()
            s = s.replace("0", "o").replace("1", "l").replace("5", "s").replace("8", "b").replace("4", "a").replace("6", "g")
            s = re.sub(r"[^a-z0-9/]", "", s)
            s = re.sub(r'(.)\1{1,}', r'\1', s)
            month_map = {
                "jan": ["ian", "jrn", "jqn", "jan"],
                "feb": ["fe6", "fcb", "fer", "feb", "febr"],
                "mar": ["mqr", "ma7", "marc", "mar"],
                "apr": ["aprl", "apri", "appr", "apr"],
                "may": ["m4y", "maay", "may"],
                "jun": ["juin", "jnn", "jun3", "jun"],
                "jul": ["ju1", "juiy", "juIy", "jul"],
                "aug": ["au9", "augus", "aue", "aug"],
                "sep": ["sept", "5ep", "se9", "sep"],
                "oct": ["o0t", "0ct", "oet", "octt", "oot", "oc", "oct"],
                "nov": ["n0v", "noV", "novv", "nov"],
                "dec": ["d3c", "de0", "decem", "dec"]
            }
            for clean, variants in month_map.items():
                for v in variants:
                    s = re.sub(v, clean, s, flags=re.I)
            for m in ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]:
                s = re.sub(m.lower(), m, s, flags=re.I)
            s = re.sub(r'/+', '/', s)
            s = re.sub(r'^/|/$', '', s)
            return s

        def letters_to_digits_for_numeric_context(text: str) -> str:
            if not text:
                return ""
            s = text
            s = s.replace('O','0').replace('o','0')
            s = s.replace('I','1').replace('l','1').replace('i','1')
            s = s.replace('S','5').replace('s','5')
            s = s.replace('B','8').replace('b','8')
            s = s.replace('Z','2').replace('z','2')
            return s

        def clean_ec(text: str) -> str:
            if not text:
                return ""
            t = letters_to_digits_for_numeric_context(text)
            t = t.replace("／", "/")
            t = re.sub(r'[^0-9/]', '', t)
            m = re.search(r'(\d{2,4})/(\d{1,2})/(\d{1,2})', t)
            if not m:
                return ""
            y, mm, dd = m.groups()
            if len(y) == 2:
                y = "20" + y
            try:
                y_i = int(y); mm_i = int(mm); dd_i = int(dd)
                if not (1 <= mm_i <= 13 and 1 <= dd_i <= 31):
                    return ""
                return f"{y_i:04d}/{mm_i:02d}/{dd_i:02d}"
            except Exception:
                return ""

        def clean_gc(text: str) -> str:
            if not text:
                return ""
            t = normalize_months(text)
            t = t.replace("／", "/")
            t = re.sub(r'[^A-Za-z0-9/]', '', t)
            t = re.sub(r'/+', '/', t)
            m = re.search(r'(\d{4})/([A-Za-z]{3,})/(\d{1,2})', t)
            if m:
                y, mon, dd = m.groups()
                try:
                    dd_i = int(dd)
                    mon_title = mon.title()[:3]
                    _ = datetime.strptime(mon_title, "%b")
                    return f"{int(y):04d}/{mon_title}/{dd_i:02d}"
                except Exception:
                    return ""
            m2 = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', t)
            if m2:
                y, mm, dd = m2.groups()
                try:
                    mm_i = int(mm); dd_i = int(dd)
                    if not (1 <= mm_i <= 12 and 1 <= dd_i <= 31):
                        return ""
                    mon_str = datetime(2000, mm_i, 1).strftime("%b")
                    return f"{int(y):04d}/{mon_str}/{dd_i:02d}"
                except Exception:
                    return ""
            return ""

        # ===============================
        # EC <-> GC wrappers
        # ===============================

        def ec_str_to_gc_str(ec_str: str) -> str:
            if not ec_str:
                return ""
            try:
                y, m, d = map(int, ec_str.split("/"))
                jdn = eth_to_jdn(y, m, d)
                gdate = jdn_to_gregorian(jdn)
                return f"{gdate.year:04d}/{gdate.strftime('%b')}/{gdate.day:02d}"
            except Exception:
                return ""

        def gc_str_to_ec_str(gc_str: str) -> str:
            if not gc_str:
                return ""
            try:
                parts = gc_str.split("/")
                y = int(re.sub(r'[^0-9]', '', parts[0]))
                mon_part = parts[1]
                if re.search(r'\d', mon_part):
                    m = int(re.sub(r'[^0-9]', '', mon_part))
                else:
                    m = datetime.strptime(mon_part[:3].title(), "%b").month
                d = int(re.sub(r'[^0-9]', '', parts[2]))
                jdn = gregorian_to_jdn(y, m, d)
                ey, em, ed = jdn_to_eth(jdn)
                return f"{int(ey):04d}/{int(em):02d}/{int(ed):02d}"
            except Exception:
                return ""

        # ===============================
        # Expiry adjustment logic
        # ===============================

        def adjust_expiry(year, month, day):
            """expiry = issue + 8 years - 2 days"""
            issue_date = datetime(year, month, day)
            try:
                expiry_date = issue_date.replace(year=issue_date.year + 8) - timedelta(days=2)
            except ValueError:
                # handle Feb 29 → Feb 28 in non-leap expiry years
                expiry_date = issue_date.replace(month=2, day=28, year=issue_date.year + 8) - timedelta(days=2)
            return expiry_date.year, expiry_date.month, expiry_date.day


        def invert_adjust_expiry(year, month, day, years_back=30):
            """issue = expiry - 8 years + 2 days"""
            expiry_date = datetime(year, month, day)
            try:
                issue_date = expiry_date.replace(year=expiry_date.year - 8) + timedelta(days=2)
            except ValueError:
                issue_date = expiry_date.replace(month=2, day=28, year=expiry_date.year - 8) + timedelta(days=2)
            return issue_date.year, issue_date.month, issue_date.day

        # ===============================
        # OCR extraction helpers
        # ===============================

        def extract_after_label(text: str, label: str) -> str:
            m = re.search(rf'{re.escape(label)}(.*)', text, flags=re.I | re.S)
            return m.group(1).strip() if m else ""

        def extract_two_side_dates(after_text: str):
            parts = [p.strip() for p in after_text.split('|') if p.strip()]
            left = parts[0] if len(parts) >= 1 else ""
            right = parts[1] if len(parts) >= 2 else ""
            return left, right

        # ===============================
        # Main extraction routine
        # ===============================

        def extract_issue_dates_and_expiry_from_ocr(ocr_text: str, invert_years_back: int = 30):
            result = {"issue_ec": "", "issue_gc": "", "expiry_ec": "", "expiry_gc": ""}

            after_issue = extract_after_label(ocr_text, "Date of Issue")
            if after_issue:
                left, right = extract_two_side_dates(after_issue)
                result["issue_ec"] = clean_ec(left) or clean_ec(right)
                result["issue_gc"] = clean_gc(right) or clean_gc(left)

            after_expiry = extract_after_label(ocr_text, "Date of Expiry")
            if after_expiry:
                left_e, right_e = extract_two_side_dates(after_expiry)
                result["expiry_ec"] = clean_ec(left_e) or clean_ec(right_e)
                result["expiry_gc"] = clean_gc(right_e) or clean_gc(left_e)

            # conversions
            if result["issue_ec"] and not result["issue_gc"]:
                result["issue_gc"] = ec_str_to_gc_str(result["issue_ec"])
            if result["issue_gc"] and not result["issue_ec"]:
                result["issue_ec"] = gc_str_to_ec_str(result["issue_gc"])
            if result["expiry_ec"] and not result["expiry_gc"]:
                result["expiry_gc"] = ec_str_to_gc_str(result["expiry_ec"])
            if result["expiry_gc"] and not result["expiry_ec"]:
                result["expiry_ec"] = gc_str_to_ec_str(result["expiry_gc"])

            # --- Compute expiry if missing ---
            if result["issue_gc"] and not result["expiry_gc"]:
                gy, gmon, gd = result["issue_gc"].split("/")
                gy = int(re.sub(r'[^0-9]', '', gy))
                gm = datetime.strptime(gmon[:3], "%b").month
                gd = int(re.sub(r'[^0-9]', '', gd))
                exp_y, exp_m, exp_d = adjust_expiry(gy, gm, gd)
                mon_str = datetime(2000, exp_m, 1).strftime("%b")
                result["expiry_gc"] = f"{exp_y:04d}/{mon_str}/{exp_d:02d}"
                result["expiry_ec"] = gc_str_to_ec_str(result["expiry_gc"])

            # --- Compute issue if missing ---
            if result["expiry_gc"] and not result["issue_gc"]:
                gy, gmon, gd = result["expiry_gc"].split("/")
                gy = int(re.sub(r'[^0-9]', '', gy))
                gm = datetime.strptime(gmon[:3], "%b").month
                gd = int(re.sub(r'[^0-9]', '', gd))
                iss_y, iss_m, iss_d = invert_adjust_expiry(gy, gm, gd)
                mon_str = datetime(2000, iss_m, 1).strftime("%b")
                result["issue_gc"] = f"{iss_y:04d}/{mon_str}/{iss_d:02d}"
                result["issue_ec"] = gc_str_to_ec_str(result["issue_gc"])

            # ✅ NEW SAFETY FIX: sanity-check year difference
            try:
                if result["issue_gc"] and result["expiry_gc"]:
                    y1, m1, d1 = result["issue_gc"].split("/")
                    y2, m2, d2 = result["expiry_gc"].split("/")
                    y1, y2 = int(y1), int(y2)
                    year_diff = abs(y2 - y1)
                    if not (7 <= year_diff <= 9):  # invalid gap
                        # Recalculate issue from expiry as a fallback
                        gy, gmon, gd = result["expiry_gc"].split("/")
                        gy = int(re.sub(r'[^0-9]', '', gy))
                        gm = datetime.strptime(gmon[:3], "%b").month
                        gd = int(re.sub(r'[^0-9]', '', gd))
                        iss_y, iss_m, iss_d = invert_adjust_expiry(gy, gm, gd)
                        mon_str = datetime(2000, iss_m, 1).strftime("%b")
                        result["issue_gc"] = f"{iss_y:04d}/{mon_str}/{iss_d:02d}"
                        result["issue_ec"] = gc_str_to_ec_str(result["issue_gc"])
            except Exception:
                pass

            return result



        # ---------- Example integration ----------
        ocr_text = pytesseract.image_to_string(barcode_img, lang="eng")
        fields = extract_issue_dates_and_expiry_from_ocr(ocr_text)

        ocr_text = preprocess_and_ocr(barcode_img)
        fields = extract_issue_dates_and_expiry_from_ocr(ocr_text)


        print(ocr_text)
        data["issue_ec"] = fields.get("issue_ec", "") or ""
        data["issue_gc"] = fields.get("issue_gc", "") or ""
        data["expiry_ec"] = fields.get("expiry_ec", "") or ""
        data["expiry_gc"] = fields.get("expiry_gc", "") or ""

        try:
            if not data["expiry_ec"] and data["issue_ec"]:
                ey, em, ed = adjust_expiry(*map(int, data["issue_ec"].split("/")))
                data["expiry_ec"] = f"{ey:04d}/{em:02d}/{ed:02d}"
        except Exception:
            pass

        try:
            if not data["expiry_gc"] and data["issue_gc"]:
                gy, gmon, gd = data["issue_gc"].split("/")
                gy = int(re.sub(r'[^0-9]', '', gy))
                gm = datetime.strptime(gmon[:3], "%b").month
                gd = int(re.sub(r'[^0-9]', '', gd))
                ey, em, ed = adjust_expiry(gy, gm, gd)
                data["expiry_gc"] = f"{ey:04d}/{datetime(2000,em,1).strftime('%b')}/{ed:02d}"
        except Exception:
            pass

        print("parsed issue_ec, issue_gc:", data.get("issue_ec"), data.get("issue_gc"))
        print("parsed expiry_ec, expiry_gc:", data.get("expiry_ec"), data.get("expiry_gc"))



        # Now crop FIN and barcode for placing on card
        w, h = fin_img.size
        fin_crop = fin_img.crop((int(w * 0.63), int(h * 0.65), int(w * 0.91), int(h * 0.69)))
        data["fin_img"] = fin_crop
        images["fin_img"] = fin_crop

        bw, bh = barcode_img.size
        barcode_crop = barcode_img.crop((int(bw * 0.24), int(bh * 0.835), int(bw * 0.7), int(bh * 0.92)))
        data["barcode_img"] = barcode_crop
        images["barcode_img"] = barcode_crop

    data["images"] = images
    # Ensure expiry fields exist even if OCR failed
    data.setdefault("expiry_ec", "")
    data.setdefault("expiry_gc", "")

    return data
def draw_rotated_text(base_img, text, position, angle, font, fill="black"):
    # 1. Create a temporary image for the text
    temp_img = Image.new("RGBA", (250, 50), (255, 255, 255, 0))  # transparent background
    temp_draw = ImageDraw.Draw(temp_img)
    temp_draw.text((0, 0), text, font=font, fill=fill)

    # 2. Rotate it
    rotated = temp_img.rotate(angle, expand=1)

    # 3. Paste it onto the base image
    base_img.paste(rotated, position, rotated)


def add_rounded_corners(img, radius_ratio=0.1):
    """Apply rounded corners to an image.
       radius_ratio = percentage of smallest dimension to use as radius (0.1 = 10%)"""
    img = img.convert("RGBA")
    w, h = img.size
    radius = int(min(w, h) * radius_ratio)

    # Create rounded mask
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), (w, h)], radius=radius, fill=255)

    # Apply mask
    rounded = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rounded.paste(img, (0, 0), mask=mask)
    return rounded


def remove_white_background(img, threshold=240):
    """Convert white background to transparent"""
    img = img.convert("RGBA")
    datas = img.getdata()
    new_data = []
    for item in datas:
        if item[0] > threshold and item[1] > threshold and item[2] > threshold:
            new_data.append((255, 255, 255, 0))
        else:
            new_data.append(item)
    img.putdata(new_data)
    return img

def add_white_shadow(img, blur_radius=25, expand=25):
    """Add a thick white shadow behind head & shoulders"""
    alpha = img.split()[-1]
    bigger_mask = alpha.filter(ImageFilter.MaxFilter(expand))
    blurred_mask = bigger_mask.filter(ImageFilter.GaussianBlur(blur_radius))
    shadow = Image.new("RGBA", img.size, (255, 255, 255, 0))
    shadow_draw = Image.new("RGBA", img.size, (255, 255, 255, 255))
    shadow = Image.composite(shadow_draw, shadow, blurred_mask)
    combined = Image.alpha_composite(shadow, img)
    return combined
import random
     # Helper to draw text with optional line break if "-" exists

def generate_number_str():
    number = random.randint(8_900_000, 9_500_000)
    return str(number)

def create_id_card(data, template_path, output_path):
    template = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(template)

    try:
        font = ImageFont.truetype(FONT_PATH, 30)
        fonts = ImageFont.truetype(FONT_PATH, 20)
        fontss = ImageFont.truetype(FONT_PATH, 25)
    except:
        font = ImageFont.load_default()
        fonts = ImageFont.load_default()
        fontss = ImageFont.load_default()

    # --- Place photo ---
    if data["images"]:
        for key, img in data["images"].items():
            if key.startswith("img_"):
                first_img = img
                break
        else:
            first_img = None

        if first_img:
            user_id = data.get("id")
            force_black = data.get("force_black", False)

            if force_black or (user_id and is_user_black(user_id)):
                first_img = ImageOps.grayscale(first_img).convert("RGBA")

                
            base_w, base_h = 280, 322
            new_w, new_h = int(base_w *1.2 ), int(base_h *1.4 )
            photo = first_img.resize((new_w, new_h))
            photo = remove_white_background(photo)
            photo = add_white_shadow(photo)
            photo = add_rounded_corners(photo, 0.2) 
            template.paste(photo, (50, 130), mask=photo)
                    # --- Add smaller photo at bottom center (without white background) ---
            small_w, small_h = 110, 140  # adjust size as needed
            photo_small = first_img.resize((small_w, small_h))
            photo_small = remove_white_background(photo_small)

            # Calculate centered X position
            x_center = 820
            y_pos = 470  # 50px margin from bottom
            photo_small = add_rounded_corners(photo_small, 0.1)
            template.paste(photo_small, (x_center, y_pos), mask=photo_small)

    # --- Text fields ---
    draw.text((405, 170), data["name_am"], fill="black", font=font)
    draw.text((405, 210), data["name_en"], fill="black", font=font)
    draw.text((405, 300), f"{data['dob_ec']} | {data['dob_gc']}", fill="black", font=font)
    draw.text((405, 370), f"{data['sex_am']} | {data['sex']}", fill="black", font=font)
    draw.text((405, 440), f"{data["expiry_ec"]} | {data["expiry_gc"]}", fill="black", font=font)
    draw_rotated_text(template, data["issue_ec"], (7,260), 90, fonts, fill="black")
    draw_rotated_text(template, data["issue_gc"], (7, 6), 90, fonts, fill="black")

    draw.text((1085, 65), f"{data['phone']}", fill="black", font=font)


        # --- Draw text helper that returns how many lines it drew ---
    def draw_split_text(draw_obj, xy, text, font, fill, line_spacing=25):
        """Draws text; if '-' exists, draws second part below and returns lines drawn."""
        if not text:
            return 0
        x, y = xy
        parts = [t.strip() for t in text.split("-", 1)]
        draw_obj.text((x, y), parts[0], fill=fill, font=font)
        if len(parts) > 1 and parts[1]:
            draw_obj.text((x, y + line_spacing), parts[1], fill=fill, font=font)
            return 2
        return 1

    # --- Region / Subcity / Woreda (Amharic + English) ---
    x = 1085
    y = 220
    step = 30   # base vertical spacing between groups

    for key in ["region_am", "region_en", "subcity_am", "subcity_en", "woreda_am", "woreda_en"]:
        lines = draw_split_text(draw, (x, y), data.get(key, ""), font, "black")
        # move Y down by base step + any extra line drawn
        y += step * lines


    draw.text((1900, 570), generate_number_str(), fill="black", font=fontss)

    # --- Paste FAN barcode ---
        # --- Paste FAN barcode ---
    if "barcode_img" in data["images"]:
        barcode_img = data["images"]["barcode_img"]

        # Resize barcode to fit into the bottom white rectangle
        target_w, target_h = 310, 100   # adjust for your rectangle space
        barcode_resized = barcode_img.resize((target_w, target_h), Image.LANCZOS)

        # Position: centered in bottom white box
        x_pos = 450
        y_pos = 510  # adjust Y to align with white rectangle

        template.paste(barcode_resized, (x_pos, y_pos))

    # --- Paste FIN ---
    if "fin_img" in data["images"]:
        fin_img = data["images"]["fin_img"]
        tar_w, tar_h = 250, 52
        fin_imgs = fin_img.resize((tar_w, tar_h), Image.LANCZOS)
        template.paste(fin_imgs, (1220, 506))

    # --- Paste QR if available ---
    for key, img in data["images"].items():
        if img.width == img.height:  # square -> QR
            qr_img = img.resize((540, 540))
            template.paste(qr_img, (1510, 20))
            break

    template.save(output_path, "PNG", optimize=True)

def effect_change(img):
    """
    Apply visual enhancement effects to improve brightness,
    contrast, color, and sharpness before mirroring.
    """
    # Convert to RGB (if not)
    img = img.convert("RGB")

    # Slight color correction and brightness adjustment
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(1.25)   # Increase color saturation

    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.3)    # Improve contrast

    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(1.1)    # Slightly brighter

    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(1.4)    # Sharpen details

    # Optional: auto equalize for more balanced lighting
    img = ImageOps.autocontrast(img)

    return img

def make_a4_pdf_with_mirror(png_path, output_pdf_path):
    """
    Create an A4 PDF with the given PNG mirrored horizontally,
    centered horizontally, placed 2% down from the top,
    and rendered at real size: 174.77mm × 55mm.
    """
    # --- Load & mirror the image ---
    imgs = Image.open(png_path) # your enhancement function
    mirrored = ImageOps.mirror(imgs)

    # Save temporary mirrored version
    tmp_path = png_path.replace(".png", "_mirrored.png")
    mirrored.save(tmp_path)

    # --- A4 dimensions in points ---
    a4_width, a4_height = A4  # (595.27, 841.89)

    # --- Target real size in mm ---
    width_mm = 184.77
    height_mm = 55

    # Convert mm → points
    mm_to_pt = 72 / 25.4
    img_w = width_mm * mm_to_pt
    img_h = height_mm * mm_to_pt

    # --- Positioning ---
    top_margin = a4_height * 0.02  # 2% from top
    x = (a4_width - img_w) / 2      # center horizontally
    y = a4_height - img_h - top_margin

    # --- Create PDF ---
    c = canvas.Canvas(output_pdf_path, pagesize=A4)
    c.drawImage(ImageReader(tmp_path), x, y, width=img_w, height=img_h)
    c.showPage()
    c.save()

    os.remove(tmp_path)
    print(f"✅ Created mirrored A4 PDF at real size ({width_mm}×{height_mm} mm): {output_pdf_path}")


# ------------------ Telegram Handlers ------------------
from telegram import ReplyKeyboardMarkup, KeyboardButton

def register_user(user_id, username):
    ref = db.reference(f'users/{user_id}')
    if not ref.get():
        ref.set({
            "username": username,
            "allow": False,
            "package": 1,  # give 1 free
            "a4": False,
            "black": False
        })


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or update.message.from_user.full_name

    # Register user if not exists
    register_user(user_id, username)

    # Keyboard with options
    keyboard = [
        [KeyboardButton("📇 Print ID")],
        [KeyboardButton("💳 Buy Package")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "Welcome! Choose an option below:",
        reply_markup=reply_markup
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.message.from_user.id
    if context.user_data.get("awaiting_one_time_receipt"):
        context.user_data["awaiting_one_time_receipt"] = False
        await handle_one_time_payment(update, context)
        return

    if text == "📇 Print ID":
        await update.message.reply_text("Please send me your PDF file to process.")

    elif text == "💳 Buy Package":
        # Show package options
        keyboard = [
            [KeyboardButton("200 birr = 10 packages")],
            [KeyboardButton("500 birr = 30 packages")],
            [KeyboardButton("1000 birr = 100 packages")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"Select a package below ",
            reply_markup=reply_markup
        )

    elif text in ["200 birr = 10 packages", "500 birr = 30 packages", "1000 birr = 100 packages"]:
        # Save chosen package in context.user_data
        package_map = {
            "200 birr = 10 packages": 10,
            "500 birr = 30 packages": 30,
            "1000 birr = 100 packages": 100,
        }
        context.user_data["requested_package"] = package_map[text]

        await update.message.reply_text(
            f"You selected {text}.\nNow send payment to {TELEBIRR_NUMBER}. Then reply the sms reciept from telebirr to be approved."
        )
    elif text in ["Normal", "A4 Mirror", "Black & White", "A4 Mirror Black & White"]:
        pdf_id = context.user_data.get("pending_pdf_id")

        if not pdf_id:
            await update.message.reply_text("⚠️ No PDF waiting. Send your ID PDF first.")
            return
        
        # One-time print settings for this job
        mode_map = {
            "Normal": {"a4": False, "black": False},
            "A4 Mirror": {"a4": True, "black": False},
            "Black & White": {"a4": False, "black": True},
            "A4 Mirror Black & White": {"a4": True, "black": True},
        }

        settings = mode_map[text]

        # ✅ Save decision for this PDF only
        db.reference(f'pdfs/{pdf_id}').update(settings)
        context.user_data.pop("pending_pdf_id", None)

        await update.message.reply_text("✅ Preference saved! Processing your ID...")

        # Fire printing pipeline
        asyncio.create_task(process_printing(pdf_id, context))
        return
    else:
        await handle_payment_text(update, context)  # fallback for receipt


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    try:
        # ✅ Download file safely
        try:
            file = await update.message.document.get_file()
        except Exception:
            await update.message.reply_text("❌ Failed to retrieve file. Send again.")
            return

        original_name = update.message.document.file_name
        temp_path = f"pdfs/{uuid.uuid4()}.pdf"
        await file.download_to_drive(temp_path)

        # ✅ Save record
        pdf_id = store_pdf(user_id, temp_path, original_name)

        # ✅ Get user info
        user_ref = db.reference(f'users/{user_id}')
        user_data = user_ref.get() or {}

        has_package = user_data.get("package", 0) > 0
        is_allowed = user_data.get("allow", False)
        a4 = user_data.get("a4", False)
        black = user_data.get("black", False)

        # ✅ User has packages but NO special perms → ask output type
        if has_package and not (is_allowed or a4 or black):
            context.user_data["pending_pdf_id"] = pdf_id

            keyboard = [
                [KeyboardButton("Normal")],
                [KeyboardButton("A4 Mirror")],
                [KeyboardButton("Black & White")],
                [KeyboardButton("A4 Mirror Black & White")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

            await update.message.reply_text(
                "📄 You have credit.\nChoose how you want your ID printed:",
                reply_markup=reply_markup
            )
            return

        # ✅ User has package OR is allowed → process instantly
        if has_package or is_allowed:
            await update.message.reply_text(f"✅ Processing your ID...")

            if has_package:
                user_ref.update({"package": max(0, user_data["package"] - 1)})

            asyncio.create_task(process_printing(pdf_id, context))
            return

        # ❌ User has NO package → ask to buy
        keyboard = [
            [KeyboardButton("💳 Buy Package")],
            [KeyboardButton("❓ How to pay")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            "⛔ You don't have printing credits.\nBuy a package to continue.",
            reply_markup=reply_markup
        )
        return

    except Exception as e:
        print("[handle_pdf fatal error]:", e)
        await update.message.reply_text("⚠️ Something went wrong while processing your file.")

async def handle_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    receipt_text = update.message.text
    requested_package = context.user_data.get("requested_package", 0)



    # Store pending package request
    request_id = str(uuid.uuid4())
    db.reference(f'package_requests/{request_id}').set({
        'user_id': user_id,
        'receipt_text': receipt_text,
        'requested_package': requested_package,
        'status': 'pending'
    })

    # Send to admin for approval
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_pkg_{request_id}"),
            InlineKeyboardButton("❌ Disapprove", callback_data=f"disapprove_pkg_{request_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📦 Package Request from User {user_id}\n"
             f"Requested: {requested_package} packages\n"
             f"Receipt:\n{receipt_text}",
        reply_markup=reply_markup
    )

    await update.message.reply_text("Your receipt has been sent. Waiting for admin approval...")
    context.user_data["requested_package"] = 0  # reset

async def handle_one_time_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    receipt_text = update.message.text

    # try to link to the last print request from this user's session
    linked_print_request_id = context.user_data.get("last_print_request")

    request_id = str(uuid.uuid4())
    one_time_ref = db.reference(f'one_time_requests/{request_id}')
    one_time_ref.set({
        'user_id': user_id,
        'receipt_text': receipt_text,
        'status': 'pending',
        'print_request_id': linked_print_request_id,  # may be None if not available
        'created_at': datetime.utcnow().isoformat()
    })

    # Send to admin for approval
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve One-Time", callback_data=f"approve_one_{request_id}"),
            InlineKeyboardButton("❌ Disapprove", callback_data=f"disapprove_one_{request_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🧾 One-Time Payment Request (25 birr)\nFrom User {user_id}\nLinked print_request: {linked_print_request_id}\nReceipt:\n{receipt_text}",
        reply_markup=reply_markup
    )

    await update.message.reply_text("Your receipt was sent. Waiting for admin approval...")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("approve_one_"):
        # Only admin can approve
        if query.from_user.id != ADMIN_ID:
            await query.edit_message_text("You are not allowed to approve.")
            return

        request_id = query.data.split("approve_one_")[1]

        # 🔹 Get request_data from DB
        request_ref = db.reference(f'one_time_requests/{request_id}')
        request_data = request_ref.get()

        if not request_data:
            await query.edit_message_text("⚠️ One-time request not found or already handled.")
            return

        user_id = request_data['user_id']
        print_request_id = request_data.get('print_request_id')

        # Mark approved
        request_ref.update({'status': 'approved', 'approved_by': query.from_user.id, 'approved_at': datetime.utcnow().isoformat()})

        # If we have an explicit linked print request that's best — fetch it
        final_path = None
        if print_request_id:
            pr = db.reference(f'print_requests/{print_request_id}').get()
            if pr:
                final_path = pr.get('final_path')

        # If not linked or missing, try a fallback (the previous fragile scan)
        if not final_path:
            all_requests = db.reference('print_requests').get() or {}
            last_req = None
            for req_id, req in (all_requests.items() if isinstance(all_requests, dict) else []):
                if req.get('user_id') == user_id:
                    last_req = req
            if last_req:
                final_path = last_req.get('final_path')

        if not final_path:
            await query.edit_message_text(f"❗ Could not find the final image for user {user_id}.Resend image or contact support.")
            return

        if final_path and os.path.exists(final_path):
   
            with open(final_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=f,
                        caption="🎉 Your payment was approved! Here is your ID card."
                    )


        else:
            await context.bot.send_message(chat_id=user_id, text="Payment approved but file is missing on server. Contact support.")

        await query.edit_message_text(f"✅ One-time request completed for user {user_id}.")

    if query.data.startswith("approve_pkg_") or query.data.startswith("disapprove_pkg_"):
        request_id = query.data.split("_")[-1]
        request_data = db.reference(f'package_requests/{request_id}').get()
        if not request_data:
            await query.edit_message_text("Request not found.")
            return

        if query.from_user.id != ADMIN_ID:
            await query.edit_message_text("You are not allowed to approve.")
            return

        if query.data.startswith("approve_pkg_"):
            user_id = request_data['user_id']
            requested_package = request_data['requested_package']

            # Update user's package count
            user_ref = db.reference(f'users/{user_id}')
            user_data = user_ref.get() or {}
            current_packages = user_data.get("package", 0)
            user_ref.update({"package": current_packages + requested_package})

            # Mark request approved
            db.reference(f'package_requests/{request_id}').update({'status': 'approved'})

            await query.edit_message_text(f"✅ Approved {requested_package} packages for user {user_id}.")

            # ✅ Send confirmation to user with menu
            keyboard = [
                [KeyboardButton("📇 Print ID")],
                [KeyboardButton("💳 Buy Package")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

            await context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 Your purchase was approved! {requested_package} packages have been added to your account.\n\nChoose an option below 👇",
                reply_markup=reply_markup
            )


        else:  # disapprove
            db.reference(f'package_requests/{request_id}').update({'status': 'disapproved'})
            await query.edit_message_text("❌ Request disapproved.")
            await context.bot.send_message(
                chat_id=request_data['user_id'],
                text="❌ Your package request was disapproved. Please contact support."
            )
        return

import asyncio
semaphore = asyncio.Semaphore(2)  # only 2 PDFs at a time

async def process_printing(pdf_id, context):
    async with semaphore:
        pdf_data = db.reference(f'pdfs/{pdf_id}').get()
        if not pdf_data:
            return

        extracted = await asyncio.to_thread(extract_id_data, pdf_data['file_path'])
        extracted["id"] = pdf_data["user_id"]
        filename = os.path.basename(pdf_data['file_path'])  # e.g. UUID_efayda_something.pdf
        # remove UUID_ prefix
        filename = "_".join(filename.split("_")[1:])  # efayda_something.pdf
        clean_name = filename.replace("efayda_", "")  # something.pdf
        output_path = os.path.join("outputs", clean_name.replace(".pdf", ".png"))

        await asyncio.to_thread(create_id_card, extracted, TEMPLATE_PATH, output_path)

        try:
            cleanup_paths = [pdf_data['file_path'], output_path]  # always clean these
            # ✅ Fetch per-job settings (temporary A4/Black)
            job_a4 = pdf_data.get("a4", False)
            job_black = pdf_data.get("black", False)

            # ✅ Override extracted data BEFORE generating card
            extracted["id"] = pdf_data["user_id"]
            extracted["a4"] = job_a4
            extracted["black"] = job_black

            output_path = os.path.join("outputs", clean_name.replace(".pdf", ".png"))

            # Apply black & white if user selected BW temporarily
            if job_black:
                # Mark for image logic
                extracted["force_black"] = True

            if job_a4 or is_user_a4(pdf_data['user_id']):
                print("changing to a4")
                pdf_output = output_path.replace(".png", "_A4.pdf")
        
                make_a4_pdf_with_mirror(output_path, pdf_output)
        
                with open(pdf_output, "rb") as f:
                    await context.bot.send_document(
                        chat_id=pdf_data['user_id'],
                        document=f,
                        caption="🎉 Here is your A4 mirrored PDF ID card."
                    )
        
                # Add the A4 file for cleanup too
                cleanup_paths.append(pdf_output)
        
            else:
                with open(output_path, "rb") as doc:
                    await context.bot.send_document(
                        chat_id=pdf_data['user_id'],
                        document=doc
                    )
        
        finally:
            # Async cleanup
            asyncio.create_task(delayed_cleanup(cleanup_paths, delay=15))

async def send_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return ConversationHandler.END
    await update.message.reply_text("👤 Please enter the user ID you want to message:")
    return ASK_USER_ID


async def get_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.text.strip()
    user_data = db.reference(f'users/{user_id}').get()
    if not user_data:
        await update.message.reply_text("❌ No user found with that ID. Try again or /cancel.")
        return ConversationHandler.END

    context.user_data["target_user_id"] = int(user_id)
    await update.message.reply_text(
        f"✅ User found: @{user_data.get('username', 'No username')}\nNow enter the message to send:"
    )
    return ASK_MESSAGE


async def send_message_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    target_user_id = context.user_data.get("target_user_id")
    if not target_user_id:
        await update.message.reply_text("❌ Something went wrong. Please start again with /sendmessage.")
        return ConversationHandler.END

    try:
        await context.bot.send_message(chat_id=target_user_id, text=f"📩 Message from Admin:\n\n{message_text}")
        await update.message.reply_text("✅ Message sent successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send message: {e}")

    return ConversationHandler.END


async def cancel_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Message sending cancelled.")
    return ConversationHandler.END
send_message_conv = ConversationHandler(
    entry_points=[CommandHandler("sendmessage", send_message_start)],
    states={
        ASK_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_user_id)],
        ASK_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_message_to_user)],
    },
    fallbacks=[CommandHandler("cancel", cancel_send)],
)

# ------------------ Main ------------------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://your-app.onrender.com/webhook
# ---------------- FastAPI App ----------------
from fastapi import FastAPI, Request

app = FastAPI()

telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Register handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(send_message_conv)
telegram_app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


# Root (for Render health checks)
@app.get("/")
@app.head("/")
async def root():
    return {"status": "ok"}

# Startup hook
@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()   # ✅ required
    await telegram_app.start()        # ✅ required
    await telegram_app.bot.set_webhook(url=os.environ["WEBHOOK_URL"])
    print("Webhook set")

# Shutdown hook
@app.on_event("shutdown")
async def shutdown_event():
    await telegram_app.stop()
    await telegram_app.shutdown()

# Webhook receiver
@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    # process in background so webhook returns fast
    asyncio.create_task(telegram_app.process_update(update))
    return {"ok": True}
