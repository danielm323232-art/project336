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

# ------------------ ENV ------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
FIREBASE_CRED = os.getenv("FIREBASE_CRED")
TELEBIRR_NUMBER = os.getenv("TELEBIRR_NUMBER")
FIREBASE_DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("FIREBASE_DATABASE_URL")

# ------------------ Firebase ------------------
cred = credentials.Certificate(FIREBASE_CRED)
firebase_admin.initialize_app(cred, {
    'databaseURL': FIREBASE_DATABASE_URL
})

# ------------------ Paths ------------------
os.makedirs("pdfs", exist_ok=True)
os.makedirs("outputs", exist_ok=True)

TEMPLATE_PATH = "LAST.png"
FONT_PATH = "NotoSansEthiopic-SemiBold.ttf"

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
        fin_crop = fin_img.crop((int(w * 0.5), int(h * 0.65), int(w * 0.91), int(h * 0.69)))
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

def create_id_card(data, template_path, output_path):
    template = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(template)

    try:
        font = ImageFont.truetype(FONT_PATH, 30)
        fonts = ImageFont.truetype(FONT_PATH, 20)
    except:
        font = ImageFont.load_default()
        fonts = ImageFont.load_default()

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
        tar_w, tar_h = 398, 58
        fin_imgs = fin_img.resize((tar_w, tar_h), Image.LANCZOS)
        template.paste(fin_imgs, (1080, 506))

    # --- Paste QR if available ---
    for key, img in data["images"].items():
        if img.width == img.height:  # square -> QR
            qr_img = img.resize((540, 540))
            template.paste(qr_img, (1510, 20))
            break

    template.save(output_path, "PNG", optimize=True)

# ------------------ Telegram Handlers ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF file to process.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    file = await update.message.document.get_file()
    file_path = f"pdfs/{uuid.uuid4()}.pdf"
    await file.download_to_drive(file_path)

    pdf_id = store_pdf(user_id, file_path)

    if is_user_allowed(user_id):
        await update.message.reply_text(f"User allowed. Processing PDF {pdf_id}...")
        await process_printing(pdf_id, context)
    else:
        # Extract demo card
        extracted = extract_id_data(file_path)
        demo_output = file_path.replace(".pdf", "_demo.png")
        create_id_card(extracted, TEMPLATE_PATH, demo_output)

        # Apply DEMO watermark
        demo_watermarked = file_path.replace(".pdf", "_demo_watermarked.png")
        add_demo_watermark(demo_output, demo_watermarked)

        # Send demo to user
        with open(demo_watermarked, "rb") as demo_file:
            await update.message.reply_photo(
                photo=demo_file,
                caption=f"You are not allowed yet.\nPlease send payment to {TELEBIRR_NUMBER} and reply with the receipt text."
            )


async def handle_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    receipt_text = update.message.text

    pdfs_ref = db.reference('pdfs').order_by_child('user_id').equal_to(user_id).get()
    pending_pdf_id = None
    for key, val in pdfs_ref.items():
        if val['status'] == 'pending' and not val['allow']:
            pending_pdf_id = key
            break

    if pending_pdf_id:
        db.reference(f'pdfs/{pending_pdf_id}').update({'receipt_text': receipt_text})
        keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{pending_pdf_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"User {user_id} submitted PDF {pending_pdf_id} with receipt:\n{receipt_text}",
            reply_markup=reply_markup
        )
        await update.message.reply_text("Receipt received. Waiting for admin approval.")
    else:
        await update.message.reply_text("No pending PDF found.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not query.data.startswith("approve_"):
        return

    pdf_id = query.data.split("_")[1]
    pdf_data = db.reference(f'pdfs/{pdf_id}').get()
    if not pdf_data:
        await query.edit_message_text("PDF not found.")
        return

    if user_id != ADMIN_ID:
        await query.edit_message_text("You are not allowed to approve.")
        return

    db.reference(f'pdfs/{pdf_id}').update({'allow': True, 'status': 'approved'})
    await query.edit_message_text(f"PDF {pdf_id} approved. Processing...")

    await context.bot.send_message(
        chat_id=pdf_data['user_id'],
        text="Your PDF has been approved. Processing now..."
    )

    await process_printing(pdf_id, context)

async def process_printing(pdf_id, context):
    pdf_data = db.reference(f'pdfs/{pdf_id}').get()
    if not pdf_data:
        return

    extracted = extract_id_data(pdf_data['file_path'])
    output_path = pdf_data['file_path'].replace(".pdf", ".png")
    create_id_card(extracted, TEMPLATE_PATH, output_path)

    with open(output_path, "rb") as doc:
        await context.bot.send_document(
            chat_id=pdf_data['user_id'],
            document=doc,
            write_timeout=120,
            connect_timeout=60,
            read_timeout=60
        )

# ------------------ Main ------------------
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_text))
app.add_handler(CallbackQueryHandler(handle_callback))

print("Bot started...")
app.run_polling()
