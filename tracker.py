# tracker.py
"""
tracker.py
==========
Script untuk melacak akun game "Arena Breakout" yang TERJUAL di marketplace FunPay.

Logika utama:
1. Scrape daftar iklan aktif hari ini dari FunPay -> df_today
2. Bandingkan dengan data hasil scrape kemarin (active_listings.csv) -> df_yesterday
3. Offer_ID yang ada di df_yesterday tapi TIDAK ADA di df_today dianggap "Terjual"
4. Sebelum mencatat sebagai terjual, lakukan verifikasi ke halaman penawaran (check_if_really_sold)
   untuk memastikan akun benar-benar terjual, bukan hanya turun halaman.
5. Simpan hasil "Terjual" ke sold_accounts_{bulan}.csv (riwayat, mode append)
6. Timpa active_listings.csv dengan df_today untuk komparasi besok

Catatan:
- Jika request biasa (requests + BeautifulSoup) terhalang Cloudflare,
  script otomatis fallback menggunakan Playwright (headless browser).
"""

import re
import sys
import datetime
import pandas as pd
import requests
from bs4 import BeautifulSoup


# =========================================================
# KONFIGURASI
# =========================================================

# TODO: Ganti dengan URL kategori "Arena Breakout" di FunPay
TARGET_URL = "https://funpay.com/en/lots/1650/"  # Ganti XXXX dengan ID kategori Arena Breakout

ACTIVE_LISTINGS_FILE = "active_listings.csv"

# KODE BARU (Otomatis Split per Bulan):
current_month = datetime.datetime.now().strftime("%Y_%m")  # Hasil: "2026_07"
SOLD_ACCOUNTS_FILE = f"sold_accounts_{current_month}.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# =========================================================
# 1. SCRAPING
# =========================================================

def scrape_with_requests(url: str) -> str:
    """
    Coba ambil HTML halaman menggunakan requests + BeautifulSoup.
    Mengembalikan None jika gagal / terindikasi diblokir Cloudflare.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()

        html = resp.text

        # Deteksi sederhana apakah halaman diblokir oleh Cloudflare
        cloudflare_markers = [
            "Just a moment...",
            "Checking your browser",
            "cf-browser-verification",
            "Attention Required! | Cloudflare",
        ]
        if any(marker.lower() in html.lower() for marker in cloudflare_markers):
            print("[INFO] Terindikasi diblokir Cloudflare, akan fallback ke Playwright...")
            return None

        return html

    except requests.RequestException as e:
        print(f"[WARNING] Request biasa gagal: {e}. Akan fallback ke Playwright...")
        return None


def scrape_with_playwright(url: str) -> str:
    """
    Ambil HTML halaman menggunakan Playwright headless browser.
    Digunakan sebagai fallback jika requests biasa terblokir Cloudflare.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()

        page.goto(url, timeout=60000, wait_until="domcontentloaded")

        # Tunggu hingga elemen daftar item muncul (sesuaikan selector jika perlu)
        try:
            page.wait_for_selector("a.tc-item", timeout=45000)
        except Exception:
            print("[WARNING] Selector 'a.tc-item' tidak ditemukan dalam 45s.")
            # Simpan HTML untuk debugging jika gagal
            debug_html = page.content()
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(debug_html)
            page.screenshot(path="debug_screenshot.png", full_page=True)
            page.wait_for_timeout(5000)

        html = page.content()
        browser.close()
        return html


def get_page_html(url: str) -> str:
    """
    Ambil HTML halaman, coba requests dulu, jika gagal/terblokir gunakan Playwright.
    Retry hingga 2x jika Playwright gagal mendapat data.
    """
    html = scrape_with_requests(url)
    if html is not None:
        return html

    for attempt in range(1, 3):
        print(f"[INFO] Mencoba Playwright (percobaan {attempt}/2)...")
        html = scrape_with_playwright(url)
        if html and "tc-item" in html:
            return html
        print("[WARNING] Hasil Playwright kosong/tidak valid, mencoba lagi...")

    return html


def parse_listings(html: str) -> pd.DataFrame:
    """
    Parsing HTML hasil scraping menjadi DataFrame dengan kolom:
    - Offer_ID
    - Deskripsi
    - Harga

    CATATAN PENTING:
    Struktur HTML berikut adalah ASUMSI berdasarkan struktur umum FunPay
    (class "tc-item" untuk setiap baris iklan). Sesuaikan selector CSS
    di bawah ini dengan struktur HTML aktual halaman target jika berbeda.
    """
    soup = BeautifulSoup(html, "lxml")

    rows = []

    # Setiap iklan biasanya berupa tag <a> dengan class "tc-item"
    items = soup.select("a.tc-item")

    for item in items:
        # --- 1. Offer_ID ---
        # Biasanya tersimpan di atribut data-id pada elemen baris
        offer_id = item.get("data-id")

        # Fallback: ambil dari URL href jika atribut data-id tidak ada
        if not offer_id:
            href = item.get("href", "")
            match = re.search(r"id=(\d+)", href)
            if match:
                offer_id = match.group(1)
            else:
                # Fallback terakhir: ambil angka terakhir dari URL
                match = re.search(r"(\d+)(?:/?$)", href)
                offer_id = match.group(1) if match else None

        if not offer_id:
            # Lewati baris yang tidak punya identifier sama sekali
            continue

        # --- 2. Deskripsi ---
        desc_el = item.select_one(".tc-desc-text")
        deskripsi = desc_el.get_text(strip=True) if desc_el else ""

        # --- 3. Harga ---
        price_el = item.select_one(".tc-price")
        harga_text = price_el.get_text(strip=True) if price_el else ""

        # Ambil angka (boleh mengandung titik/koma desimal) dari teks harga
        price_match = re.search(r"[\d.,]+", harga_text)
        if price_match:
            harga_clean = price_match.group(0).replace(",", "")
            try:
                harga = float(harga_clean)
            except ValueError:
                harga = None
        else:
            harga = None

        rows.append({
            "Offer_ID": str(offer_id),
            "Deskripsi": deskripsi,
            "Harga": harga,
        })

    df = pd.DataFrame(rows, columns=["Offer_ID", "Deskripsi", "Harga"])
    return df


# =========================================================
# 2. PEMBERSIHAN DATA (DATA CLEANING)
# =========================================================

def extract_storage_millions(desc: str):
    """
    Ekstrak jumlah penyimpanan/uang dalam satuan "Juta" (Millions) dari teks deskripsi.

    Pola yang didukung:
    - "60M"            -> 60
    - "60kk"           -> 60
    - "60cc"           -> 60
    - "40,000,000"     -> 40
    - "40.000.000"     -> 40 (format dengan titik sebagai pemisah ribuan)

    Mengembalikan float (dalam satuan juta) atau None jika tidak ditemukan.
    """
    if not isinstance(desc, str):
        return None

    text = desc

    # --- Pola 1: angka diikuti M / kk / cc (case-insensitive) ---
    # Contoh: "60M", "60 kk", "60.5M", "60,5kk"
    pattern_suffix = re.compile(
        r"(\d+(?:[.,]\d+)?)\s*(m|kk|cc)\b",
        re.IGNORECASE,
    )
    match = pattern_suffix.search(text)
    if match:
        number_str = match.group(1).replace(",", ".")
        try:
            return float(number_str)
        except ValueError:
            return None

    # --- Pola 2: angka penuh dengan pemisah ribuan, minimal 6 digit ---
    # Contoh: "40,000,000" atau "40.000.000" -> 40 (juta)
    pattern_full = re.compile(r"\b(\d{1,3}(?:[.,]\d{3}){2,})\b")
    match = pattern_full.search(text)
    if match:
        raw_number = match.group(1)
        # Hilangkan semua separator (titik & koma) untuk mendapatkan angka penuh
        digits_only = re.sub(r"[.,]", "", raw_number)
        try:
            value = int(digits_only)
            # Konversi ke satuan juta
            return round(value / 1_000_000, 2)
        except ValueError:
            return None

    return None


def detect_premium(desc: str) -> bool:
    """
    Deteksi apakah deskripsi mengandung item premium:
    "knife", "knives", "glove", atau "gloves" (case-insensitive).
    """
    if not isinstance(desc, str):
        return False

    keywords = ["knife", "knives", "glove", "gloves"]
    desc_lower = desc.lower()
    return any(keyword in desc_lower for keyword in keywords)


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Terapkan semua aturan pembersihan data pada DataFrame hasil scraping:
    1. (OPTIMASI) Filter awal: Hanya ambil akun "Arena Breakout Mobile"
    2. Filter awal: Hapus baris "Rent"/"Rental"
    3. Ekstrak Storage (Juta)
    4. Deteksi item Premium
    """
    if df.empty:
        df["Storage_Millions"] = pd.Series(dtype="float")
        df["Has_Premium"] = pd.Series(dtype="bool")
        return df

    # 1. FILTER AWAL KHUSUS MOBILE (Membuang data PC/Infinite agar proses selanjutnya lebih ringan)
    mask_mobile = df["Deskripsi"].str.contains(r"arena breakout mobile", case=False, na=False)
    df = df[mask_mobile]

    # 2. FILTER AWAL RENTAL (Membuang data sewa)
    mask_rent = df["Deskripsi"].str.contains(r"rent(?:al)?", case=False, na=False)
    df = df[~mask_rent]

    # 3. Komputasi Regex Storage (Hanya berjalan pada data yang sudah disaring)
    df["Storage_Millions"] = df["Deskripsi"].apply(extract_storage_millions)

    # 4. Komputasi Deteksi Premium (Hanya berjalan pada data yang sudah disaring)
    df["Has_Premium"] = df["Deskripsi"].apply(detect_premium)

    # Reset index di akhir setelah semua baris yang tidak perlu dibuang
    df = df.reset_index(drop=True)

    return df


# =========================================================
# 2b. VERIFIKASI STATUS PENAWARAN
# =========================================================

def check_if_really_sold(offer_id):
    """
    Mengecek langsung ke URL penawaran apakah benar-benar terjual
    atau hanya turun halaman (masih aktif di halaman lain).

    Mengembalikan True jika benar-benar terjual/tidak aktif,
    False jika masih aktif (halaman penawaran masih bisa dibeli).
    """
    url = f"https://funpay.com/lots/offer?id={offer_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        # Jika halaman tidak ditemukan (404, 410, dll) -> kemungkinan besar sudah dihapus/terjual
        if resp.status_code != 200:
            return True

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Cari tombol beli (button atau link dengan class 'js-lot-buy')
        buy_btn = soup.find('button', class_='js-lot-buy') or soup.find('a', class_='js-lot-buy')

        # Jika tombol beli tidak ada -> penawaran sudah tidak aktif
        if not buy_btn:
            return True

        # Periksa apakah tombol memiliki class 'disabled'
        classes = buy_btn.get('class', [])
        if isinstance(classes, str):
            classes = classes.split()
        if 'disabled' in classes:
            return True

        # Tombol ada dan tidak disabled -> masih aktif
        return False

    except Exception as e:
        print(f"  ⚠️ Gagal verifikasi ID {offer_id}: {e}")
        # Jika terjadi error jaringan/timeout, asumsikan belum terjual
        # agar tidak salah mencatat penjualan palsu
        return False


# =========================================================
# 3. LOGIKA KOMPARASI "AKUN TERJUAL"
# =========================================================

def run_comparison(df_today: pd.DataFrame):
    """
    Bandingkan df_today dengan data kemarin (active_listings.csv).

    - Jika active_listings.csv belum ada, buat file dari df_today
      dan hentikan script (karena belum ada data pembanding).
    - Jika sudah ada, cari Offer_ID yang hilang (ada kemarin, tidak ada hari ini).
    - Verifikasi setiap Offer_ID yang hilang dengan check_if_really_sold:
        * Jika benar terjual  -> catat ke sold_accounts_{bulan}.csv
        * Jika masih aktif    -> kembalikan ke df_today (agar tetap termonitor)
    - Selalu timpa active_listings.csv dengan df_today yang sudah diperbarui
      di akhir proses.
    """
    import os

    # --- Cek apakah file data kemarin tersedia ---
    if not os.path.exists(ACTIVE_LISTINGS_FILE):
        print(f"[INFO] File '{ACTIVE_LISTINGS_FILE}' belum ditemukan.")
        print("[INFO] Ini kemungkinan run pertama. Menyimpan data hari ini sebagai baseline.")
        df_today.to_csv(ACTIVE_LISTINGS_FILE, index=False)
        print("[INFO] Baseline tersimpan. Script dihentikan (butuh data hari ke-2 untuk komparasi).")
        sys.exit(0)

    # --- Baca data kemarin ---
    df_yesterday = pd.read_csv(ACTIVE_LISTINGS_FILE, dtype={"Offer_ID": str})

    # Pastikan Offer_ID hari ini juga bertipe string agar perbandingan akurat
    df_today["Offer_ID"] = df_today["Offer_ID"].astype(str)

    # --- Cari Offer_ID yang ada kemarin TAPI TIDAK ADA hari ini ---
    ids_yesterday = set(df_yesterday["Offer_ID"])
    ids_today = set(df_today["Offer_ID"])

    missing_ids = ids_yesterday - ids_today

    actually_sold_ids = set()    # Offer_ID yang benar-benar terjual
    still_active_ids = set()     # Offer_ID yang masih aktif (turun halaman)

    if missing_ids:
        print(f"[INFO] Mendeteksi {len(missing_ids)} Offer_ID hilang dari halaman. Memverifikasi status...")
        for offer_id in missing_ids:
            if check_if_really_sold(offer_id):
                actually_sold_ids.add(offer_id)
                print(f"  ✅ Offer {offer_id} TERJUAL (terverifikasi).")
            else:
                still_active_ids.add(offer_id)
                print(f"  ⏸️ Offer {offer_id} masih aktif (turun halaman).")
    else:
        print("[INFO] Tidak ada perubahan Offer_ID.")

    # --- Proses akun yang benar-benar terjual ---
    if actually_sold_ids:
        df_sold = df_yesterday[df_yesterday["Offer_ID"].isin(actually_sold_ids)].copy()
        today_str = datetime.date.today().isoformat()
        df_sold["Date_Sold"] = today_str

        # Append ke sold_accounts_{bulan}.csv
        write_header = not os.path.exists(SOLD_ACCOUNTS_FILE)
        df_sold.to_csv(SOLD_ACCOUNTS_FILE, mode="a", header=write_header, index=False)

        print(f"[INFO] {len(df_sold)} akun terjual disimpan ke '{SOLD_ACCOUNTS_FILE}'.")
    else:
        print("[INFO] Tidak ada akun yang benar-benar terjual.")

    # --- Kembalikan akun yang masih aktif ke daftar hari ini ---
    if still_active_ids:
        df_still_active = df_yesterday[df_yesterday["Offer_ID"].isin(still_active_ids)].copy()
        # Gabungkan dengan df_today (hindari duplikat Offer_ID)
        df_today = pd.concat([df_today, df_still_active], ignore_index=True)
        df_today = df_today.drop_duplicates(subset="Offer_ID", keep="first")
        print(f"[INFO] {len(df_still_active)} akun masih aktif, dikembalikan ke daftar hari ini.")

    # --- Simpan daftar terkini untuk komparasi besok ---
    df_today.to_csv(ACTIVE_LISTINGS_FILE, index=False)
    print(f"[INFO] '{ACTIVE_LISTINGS_FILE}' diperbarui dengan {len(df_today)} iklan aktif.")


# =========================================================
# MAIN
# =========================================================

def main():
    print(f"[INFO] Memulai scraping FunPay - Arena Breakout pada {datetime.datetime.now()}")

    # 1. Ambil HTML halaman target
    html = get_page_html(TARGET_URL)
    if not html:
        print("[ERROR] Gagal mengambil HTML halaman. Script dihentikan.")
        sys.exit(1)

    # 2. Parsing HTML -> DataFrame mentah
    df_raw = parse_listings(html)
    print(f"[INFO] Berhasil scrape {len(df_raw)} iklan dari halaman.")

    if df_raw.empty:
        print("[WARNING] Tidak ada data yang berhasil di-scrape. "
              "Periksa kembali selector HTML pada fungsi parse_listings().")
        sys.exit(1)

    # 3. Bersihkan data (storage, premium, filter rent)
    df_today = clean_dataframe(df_raw)
    print(f"[INFO] Setelah pembersihan data: {len(df_today)} baris valid.")

    # 4. Jalankan logika komparasi & update file CSV
    run_comparison(df_today)

    print("[INFO] Script selesai dijalankan.")


if __name__ == "__main__":
    main()
