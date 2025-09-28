import os
import io
import re
import uuid
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv
import pytesseract
import os
import asyncio
import aiohttp
# ------------------ ENV ------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
TELEBIRR_NUMBER = os.getenv("TELEBIRR_NUMBER")
FIREBASE_DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("FIREBASE_DATABASE_URL")

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

def store_pdf(user_id, file_path):
    pdf_id = str(uuid.uuid4())
    db.reference(f'pdfs/{pdf_id}').set({
        'user_id': user_id,
        'file_path': file_path,
        'status': 'pending',
        'allow': False
    })
    return pdf_id
from datetime import datetime, timedelta
import calendar

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
    
    return year, month, day
import asyncio

async def delayed_cleanup(paths, delay=300):  # 300 sec = 5 min
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


import pytesseract

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

        # OCR full barcode image before cropping
        ocr_text = pytesseract.image_to_string(barcode_img, lang="eng+amh")

        # Extract issue/expiry dates
        issue_matches = re.findall(r"\d{4}/[0-9]{2}/[0-9]{2}\s*\|\s*\d{4}/[A-Za-z]{3}/[0-9]{2}", ocr_text)

        if issue_matches:
            ec, gc = [part.strip() for part in issue_matches[0].split("|", 1)]
            data["issue_ec"] = ec
            data["issue_gc"] = gc
            try:
                ec_y, ec_m, ec_d = map(int, ec.split("/"))
                expiry_ec_y, expiry_ec_m, expiry_ec_d = adjust_expiry(ec_y, ec_m, ec_d)
                data["expiry_ec"] = f"{expiry_ec_y:04d}/{expiry_ec_m:02d}/{expiry_ec_d:02d}"
            except Exception as e:
                data["expiry_ec"] = ""

            # ---- Handle GC expiry ----
            try:
                # parse GC date (with month name)
                gc_y, gc_m_str, gc_d = gc.split("/")
                gc_y = int(gc_y)
                gc_d = int(gc_d)
                gc_m = datetime.strptime(gc_m_str, "%b").month  # convert "Aug" → 8

                expiry_gc_y, expiry_gc_m, expiry_gc_d = adjust_expiry(gc_y, gc_m, gc_d)
                expiry_gc_m_str = datetime(2000, expiry_gc_m, 1).strftime("%b")  # back to abbrev
                data["expiry_gc"] = f"{expiry_gc_y:04d}/{expiry_gc_m_str}/{expiry_gc_d:02d}"
            except Exception as e:
                data["expiry_gc"] = ""
        else:
            data["issue_ec"] = data["issue_gc"] = ""
            data["expiry_ec"] = data["expiry_gc"] = ""

        
        




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
    draw.text((1085, 220), data["region_am"], fill="black", font=font)
    draw.text((1085, 250), data["region_en"], fill="black", font=font)
    draw.text((1085, 280), data["subcity_am"], fill="black", font=font)
    draw.text((1085, 310), data["subcity_en"], fill="black", font=font)
    draw.text((1085, 340), data["woreda_am"], fill="black", font=font)
    draw.text((1085, 370), data["woreda_en"], fill="black", font=font)
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

# ------------------ Telegram Handlers ------------------
from telegram import ReplyKeyboardMarkup, KeyboardButton

def register_user(user_id, username):
    ref = db.reference(f'users/{user_id}')
    if not ref.get():
        ref.set({
            "username": username,
            "allow": False,
            "package": 0
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
async def keepalive_loop(interval_seconds: int = 4 * 60):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(GETME_URL, timeout=10) as resp:
                    # optional: check status or json
                    await resp.text()
            except Exception as e:
                # log error but keep going
                print("keepalive error:", e)
            await asyncio.sleep(interval_seconds)

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

    else:
        await handle_payment_text(update, context)  # fallback for receipt


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    file = await update.message.document.get_file()
    file_path = f"pdfs/{uuid.uuid4()}.pdf"
    await file.download_to_drive(file_path)

    pdf_id = store_pdf(user_id, file_path)

    user_ref = db.reference(f'users/{user_id}')
    user_data = user_ref.get() or {}

    has_package = user_data.get("package", 0) > 0
    is_allowed = user_data.get("allow", False)

    if is_allowed or has_package:
        await update.message.reply_text(f"Processing PDF {pdf_id}...")

        # Deduct 1 package if available
        if has_package:
            new_package_count = max(0, user_data["package"] - 1)
            user_ref.update({"package": new_package_count})

        await process_printing(pdf_id, context)
    else:
        # Extract demo card
        extracted = extract_id_data(file_path)
        demo_output = file_path.replace(".pdf", "_demo.png")
        create_id_card(extracted, TEMPLATE_PATH, demo_output)

        demo_watermarked = file_path.replace(".pdf", "_demo_watermarked.png")
        add_demo_watermark(demo_output, demo_watermarked)

        # Save request in DB
                # Save request in DB (store request_id so we can link one-time payment)
        request_id = str(uuid.uuid4())
        db.reference(f'print_requests/{request_id}').set({
            'user_id': user_id,
            'pdf_id': pdf_id,
            'demo_path': demo_watermarked,
            'final_path': demo_output,
            'status': 'pending',
            'created_at': datetime.utcnow().isoformat()
        })

        # keep the print request id in user's session so receipt can be linked
        context.user_data["last_print_request"] = request_id

        try:
            with open(demo_watermarked, "rb") as demo_file:
                await update.message.reply_photo(
                    photo=demo_file,
                    caption=f"You don’t have a package.\nPlease send 25 birr to {TELEBIRR_NUMBER} and the sms receipt message you recieve from telebirr."
                )
        except Exception as e:
            print("Send error:", e)

        # mark that we are awaiting a one-time receipt and which print request to fulfill
        context.user_data["awaiting_one_time_receipt"] = True


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
                # send as document (same as paid flow) to preserve quality
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
   

async def process_printing(pdf_id, context):
    pdf_data = db.reference(f'pdfs/{pdf_id}').get()
    if not pdf_data:
        return

    extracted = extract_id_data(pdf_data['file_path'])
    output_path = pdf_data['file_path'].replace(".pdf", ".png")
    create_id_card(extracted, TEMPLATE_PATH, output_path)

    try:
        with open(output_path, "rb") as doc:
            await context.bot.send_document(
                chat_id=pdf_data['user_id'],
                document=doc,
                write_timeout=120,
                connect_timeout=60,
                read_timeout=60
            )
    finally:
        # ✅ schedule delayed cleanup (5 minutes)
        asyncio.create_task(delayed_cleanup([pdf_data['file_path'], output_path], delay=600))


# ------------------ Main ------------------
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def on_startup(app):
    app.create_task(keepalive_loop())   # python-telegram-bot v20 style
app.post_init = on_startup

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(CallbackQueryHandler(handle_callback))

print("Bot started...")
app.run_polling()
