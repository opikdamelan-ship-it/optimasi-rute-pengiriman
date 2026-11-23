import io
import os
import math
import time
import shutil
import datetime
import tempfile
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

# ==========================================
# KONFIGURASI HALAMAN WEB
# ==========================================
st.set_page_config(
    page_title="Optimasi Rute Pengiriman Odoo",
    page_icon="🚚",
    layout="wide",
)


# ==========================================
# GERBANG PASSWORD AKSES WEBSITE
# ==========================================
def cek_password():
    if st.session_state.get("akses_valid"):
        return True

    st.title("🔒 Akses Terbatas - Optimasi Rute Pengiriman")
    st.caption("Silakan masukkan password akses yang diberikan admin untuk menggunakan aplikasi ini.")

    pwd_input = st.text_input("Password Akses", type="password", placeholder="Ketik password di sini...")
    if st.button("Masuk", type="primary"):
        pwd_benar = st.secrets.get("APP_PASSWORD", "theacorp")
        if pwd_input == pwd_benar:
            st.session_state["akses_valid"] = True
            st.rerun()
        else:
            st.error("Password salah. Silakan coba lagi.")
    return False

if not cek_password():
    st.stop()



# ==========================================
# KONFIGURASI PROXY (OPSIONAL VIA SECRETS/ENV)
# ==========================================
# Di cloud (Streamlit), proxy tidak diperlukan (otomatis kosong).
# Jika dijalankan di server lokal berkantor proxy, isi lewat Environment Variable.
PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = os.environ.get("PROXY_PORT", "8080")
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

if PROXY_HOST:
    cred = f"{quote(PROXY_USER, safe='')}:{quote(PROXY_PASS, safe='')}@" if PROXY_USER else ""
    http_proxy = f"http://{cred}{PROXY_HOST}:{PROXY_PORT}"
    proxies_config = {"http": http_proxy, "https": http_proxy}
else:
    proxies_config = None

# ==========================================
# KONSTANTA KOLOM, ROUTING & PETA
# ==========================================
COL_LAT = "picking_line/partner_id/lat"
COL_LNG = "picking_line/partner_id/lng"

GROUP_CANDIDATES = [
    "picking_batch_id", "batch_id", "driver_id", "user_id",
    "carrier_id", "name",
]
LABEL_CANDIDATES = [
    "picking_line/name", "picking_line/partner_id/name",
    "picking_line/partner_id", "partner_id",
    "picking_line/partner_id/display_name",
]
DOC_CANDIDATES = ["dokumen"]

MANUAL_KOORDINAT = {}

# Server OSRM. Bisa diisi lebih dari satu (pisah koma) lewat env/secrets OSRM_URL,
# contoh: OSRM_URL="http://IP-VPS-KAMU:5000". Server publik selalu jadi cadangan terakhir.
OSRM_PUBLIK = "https://router.project-osrm.org"


def _ambil_osrm_url():
    nilai = os.environ.get("OSRM_URL", "")
    if not nilai:
        try:
            nilai = st.secrets.get("OSRM_URL", "")
        except Exception:
            nilai = ""
    urls = [u.strip().rstrip("/") for u in str(nilai).split(",") if u.strip()]
    if OSRM_PUBLIK not in urls:
        urls.append(OSRM_PUBLIK)
    return urls


OSRM_BASE_URLS = _ambil_osrm_url()
OSRM_TIMEOUT = 20
OSRM_RETRY = 4            # jumlah putaran percobaan (tiap putaran coba semua server)
OSRM_BACKOFF = 1.5        # detik, dikali 2 tiap putaran: 1.5, 3, 6, ...
OSRM_DELAY = 0.3          # jeda sopan HANYA untuk server publik
OSRM_TABLE_BLOK = 50      # blok sumber x tujuan, total koordinat <= 100
OSRM_CACHE_DB = os.path.join(tempfile.gettempdir(), "osrm_leg_cache.sqlite")
FALLBACK_FACTOR = 1.3

MAP_W_IN, MAP_H_IN, MAP_DPI = 9, 6.5, 100
WARNA_HARI = [
    "#1565c0", "#e65100", "#6a1b9a", "#c62828",
    "#00838f", "#ad1457", "#4e342e", "#455a64",
]

# Aturan jadwal default
JAM_MULAI = 7 * 60               # 07:00
JAM_SELESAI_MAKS = 17 * 60       # 17:00
JAM_PULANG_MAKS = 20 * 60        # 20:00
JAM_ISTIRAHAT_SIANG = 12 * 60 + 30
ISTIRAHAT_SIANG_MENIT = 60
BATAS_NYETIR_JAM = 8
FAKTOR_WAKTU_TRUK = 1.35

session = requests.Session()
if proxies_config:
    session.proxies.update(proxies_config)


# ------------------------------------------------------------------
# Utilitas & Geocoding
# ------------------------------------------------------------------
def fmt_jam(menit):
    m = int(round(menit))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


@st.cache_data(ttl=86400, show_spinner=False)
def cari_koordinat_nominatim(nama_toko, kota_default="Bandung, Indonesia"):
    """Mencari lat/lng otomatis via OpenStreetMap jika koordinat kosong."""
    if not nama_toko or pd.isna(nama_toko):
        return None, None
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": f"{str(nama_toko).strip()}, {kota_default}", "format": "json", "limit": 1}
    headers = {"User-Agent": "RutePengirimanWeb/1.0"}
    try:
        resp = session.get(url, params=params, headers=headers, timeout=8)
        data = resp.json()
        if data:
            time.sleep(0.35)
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None, None


def baca_file_upload(uploaded_file):
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext == ".csv":
        raw = uploaded_file.getvalue()
        try:
            df = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="latin-1")
    else:
        df = pd.read_excel(uploaded_file)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def pilih_kolom(df, kandidat):
    for c in kandidat:
        if c in df.columns:
            return c
    return None


def bersihkan_angka(seri):
    def f(v):
        if pd.isna(v):
            return None
        t = str(v)
        for ch in ("'", '"', "`", "\u2018", "\u2019", "\u00a0"):
            t = t.replace(ch, "")
        return t.strip().replace(" ", "").replace(",", ".")
    return pd.to_numeric(seri.astype(object).map(f), errors="coerce")


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------------------------------------------
# Penggabungan 5 Proses CSV (Picking, Exchange, Return, Others, Forward)
# ------------------------------------------------------------------
def gabungkan_5_proses_csv(df, status_box=None):
    kolom_5_proses = [
        ("picking_line/name", "picking_line/partner_id/lat", "picking_line/partner_id/lng", "Picking"),
        ("exchange_line/name", "exchange_line/partner_id/lat", "exchange_line/partner_id/lng", "Exchange"),
        ("return_line/partner_id/name", "return_line/partner_id/lat", "return_line/partner_id/lng", "Return"),
        ("others_id/reqothers_out_id/display_name", "others_id/reqothers_out_id/lat_delivery", "others_id/reqothers_out_id/lng_delivery", "Others"),
        ("forward_line/display_name", "forward_line/lat_delivery", "forward_line/lng_delivery", "Forward"),
    ]

    ada_kolom_proses = any(c_nm in df.columns for c_nm, _, _, _ in kolom_5_proses)
    if not ada_kolom_proses:
        return df

    if status_box:
        status_box.write("Menggabungkan 5 proses pengiriman (Picking, Exchange, Return, Others, Forward)...")

    records = []
    cur_name = None
    cur_list_rute = None
    cur_total = None
    cur_total_toko = None

    for _, row in df.iterrows():
        if "name" in df.columns and pd.notna(row.get("name")) and str(row.get("name")).strip():
            cur_name = str(row["name"]).strip()
            cur_list_rute = str(row["list_rute"]).strip() if "list_rute" in df.columns and pd.notna(row.get("list_rute")) else cur_name
            cur_total = row.get("total") if "total" in df.columns and pd.notna(row.get("total")) else None
            cur_total_toko = row.get("total_toko") if "total_toko" in df.columns and pd.notna(row.get("total_toko")) else None

        is_first_in_group = pd.notna(row.get("total")) if "total" in df.columns else False

        for c_nm, c_lat, c_lng, jenis in kolom_5_proses:
            if c_nm in df.columns and c_lat in df.columns and c_lng in df.columns:
                nm_val = row.get(c_nm)
                if pd.notna(nm_val) and str(nm_val).strip() and str(nm_val).strip().lower() != "nan":
                    nm_clean = str(nm_val).strip()
                    lat_val = bersihkan_angka(pd.Series([row.get(c_lat)])).iloc[0]
                    lng_val = bersihkan_angka(pd.Series([row.get(c_lng)])).iloc[0]

                    if (pd.isna(lat_val) or pd.isna(lng_val)) and MANUAL_KOORDINAT:
                        for m_nm, (m_la, m_ln) in MANUAL_KOORDINAT.items():
                            if m_nm.strip().upper() == nm_clean.upper():
                                lat_val, lng_val = m_la, m_ln
                                break

                    if pd.isna(lat_val) or pd.isna(lng_val):
                        if status_box:
                            status_box.write(f"🔍 Mencari koordinat otomatis: **{nm_clean}** ({jenis})...")
                        lat_val, lng_val = cari_koordinat_nominatim(nm_clean)

                    records.append({
                        "name": cur_name,
                        "list_rute": cur_list_rute or cur_name or "SEMUA",
                        "total": cur_total if is_first_in_group else None,
                        "total_toko": cur_total_toko if is_first_in_group else None,
                        "picking_line/name": f"[{jenis}] {nm_clean}",
                        COL_LAT: lat_val,
                        COL_LNG: lng_val,
                        "dokumen": jenis,
                    })
                    is_first_in_group = False

    if not records:
        return df

    df_gabung = pd.DataFrame(records)
    if status_box:
        status_box.write(f"✅ Berhasil menggabungkan total **{len(df_gabung)}** item dari 5 proses.")
    return df_gabung


# ------------------------------------------------------------------
# OSRM: rute per leg dan matriks waktu (retry, multi-server, cache aman)
# ------------------------------------------------------------------
_HEADERS = {"User-Agent": "rute-pengiriman-odoo/1.0"}
OSRM_LOG = []  # alasan kegagalan, untuk diagnostik


def _osrm_get(path_query):
    """GET ke OSRM dengan retry + backoff + failover antar server.
    Return data JSON bila code == Ok, selain itu raise RuntimeError."""
    terakhir = "tidak ada respons"
    for percobaan in range(OSRM_RETRY):
        for base in OSRM_BASE_URLS:
            try:
                resp = session.get(base + path_query, timeout=OSRM_TIMEOUT, headers=_HEADERS)
                if resp.status_code in (429, 500, 502, 503, 504):
                    terakhir = f"{base} HTTP {resp.status_code}"
                    OSRM_LOG.append(terakhir)
                    ra = resp.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        time.sleep(min(int(ra), 10))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == "Ok":
                    if base == OSRM_PUBLIK:
                        time.sleep(OSRM_DELAY)
                    return data
                if data.get("code") in ("NoRoute", "NoSegment"):
                    raise RuntimeError(data.get("code"))  # tidak ada gunanya diulang
                terakhir = f"{base} {data.get('code')}"
                OSRM_LOG.append(terakhir)
            except RuntimeError:
                raise
            except Exception as e:
                terakhir = f"{base} {type(e).__name__}: {str(e)[:120]}"
                OSRM_LOG.append(terakhir)
        time.sleep(OSRM_BACKOFF * (2 ** percobaan))
    raise RuntimeError(terakhir)


# --- cache leg permanen (SQLite). Hanya hasil OSRM sukses yang disimpan. ---
def _cache_conn():
    import sqlite3
    con = sqlite3.connect(OSRM_CACHE_DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS leg (k TEXT PRIMARY KEY, v TEXT, t REAL)")
    return con


def _cache_get(k):
    import json
    try:
        con = _cache_conn()
        row = con.execute("SELECT v, t FROM leg WHERE k=?", (k,)).fetchone()
        con.close()
        if row and time.time() - row[1] < 30 * 86400:
            km, mnt, geom = json.loads(row[0])
            return km, mnt, [tuple(p) for p in geom]
    except Exception:
        pass
    return None


def _cache_set(k, km, mnt, geom):
    import json
    try:
        con = _cache_conn()
        con.execute("INSERT OR REPLACE INTO leg VALUES (?,?,?)", (k, json.dumps([km, mnt, geom]), time.time()))
        con.commit()
        con.close()
    except Exception:
        pass


def jarak_osrm(lat1, lng1, lat2, lng2, fallback_speed_kmh):
    # Sengaja TANPA st.cache_data: hasil ESTIMASI tidak boleh ikut ter-cache 24 jam.
    k = f"{lat1:.6f},{lng1:.6f};{lat2:.6f},{lng2:.6f}"
    c = _cache_get(k)
    if c:
        return (c[0], c[1], "OSRM", c[2])
    try:
        data = _osrm_get(
            f"/route/v1/driving/{lng1},{lat1};{lng2},{lat2}?overview=full&geometries=geojson"
        )
        rt = data["routes"][0]
        geom = [tuple(p) for p in rt["geometry"]["coordinates"]]
        km, mnt = rt["distance"] / 1000.0, rt["duration"] / 60.0
        _cache_set(k, km, mnt, geom)
        return (km, mnt, "OSRM", geom)
    except Exception:
        km = haversine_km(lat1, lng1, lat2, lng2) * FALLBACK_FACTOR
        return (km, km / fallback_speed_kmh * 60.0, "ESTIMASI", [(lng1, lat1), (lng2, lat2)])


@st.cache_data(ttl=86400, show_spinner=False)
def _tabel_blok(src_pts, dst_pts):
    """Satu blok matriks durasi (menit). Raise bila gagal -> tidak ikut ter-cache."""
    pts = list(src_pts) + list(dst_pts)
    coords = ";".join(f"{lng},{lat}" for lat, lng in pts)
    s = ";".join(str(i) for i in range(len(src_pts)))
    d = ";".join(str(len(src_pts) + i) for i in range(len(dst_pts)))
    data = _osrm_get(f"/table/v1/driving/{coords}?annotations=duration&sources={s}&destinations={d}")
    return data["durations"]


def matriks_waktu(points, fallback_speed_kmh):
    n = len(points)

    def estimasi(i, j):
        km = haversine_km(points[i][0], points[i][1], points[j][0], points[j][1]) * FALLBACK_FACTOR
        return km / fallback_speed_kmh * 60.0

    m = [[0.0 if i == j else None for j in range(n)] for i in range(n)]
    pts = [tuple(p) for p in points]
    blok = max(1, OSRM_TABLE_BLOK)
    gagal = 0
    for a in range(0, n, blok):
        for b in range(0, n, blok):
            src_idx = range(a, min(a + blok, n))
            dst_idx = range(b, min(b + blok, n))
            try:
                dur = _tabel_blok(tuple(pts[i] for i in src_idx), tuple(pts[j] for j in dst_idx))
                for ii, i in enumerate(src_idx):
                    for jj, j in enumerate(dst_idx):
                        v = dur[ii][jj]
                        if i != j and v is not None:
                            m[i][j] = v / 60.0
            except Exception:
                gagal += 1

    for i in range(n):
        for j in range(n):
            if m[i][j] is None:
                m[i][j] = estimasi(i, j)
    return m, ("OSRM" if gagal == 0 else "ESTIMASI")


# ------------------------------------------------------------------
# Optimasi urutan: Terdekat, Terjauh, Optimal
# ------------------------------------------------------------------
def _panjang(tour, d):
    return sum(d[a][b] for a, b in zip(tour[:-1], tour[1:]))


def _dua_opt(tour, d):
    diperbaiki = False
    n = len(tour)
    for i in range(1, n - 2):
        for j in range(i + 1, n - 1):
            a, b, c, e = tour[i - 1], tour[i], tour[j], tour[j + 1]
            delta = d[a][c] + d[b][e] - d[a][b] - d[c][e]
            if delta < -1e-9:
                tour[i:j + 1] = reversed(tour[i:j + 1])
                diperbaiki = True
    return diperbaiki


def _or_opt(tour, d):
    diperbaiki = False
    for seglen in (1, 2, 3):
        i = 1
        while i + seglen <= len(tour) - 1:
            seg = tour[i:i + seglen]
            prev, nxt = tour[i - 1], tour[i + seglen]
            gain = d[prev][seg[0]] + d[seg[-1]][nxt] - d[prev][nxt]
            rest = tour[:i] + tour[i + seglen:]
            best, pos, rev = 1e-9, None, False
            for k in range(len(rest) - 1):
                p, q = rest[k], rest[k + 1]
                base = d[p][q]
                add = d[p][seg[0]] + d[seg[-1]][q] - base
                add_r = d[p][seg[-1]] + d[seg[0]][q] - base
                if gain - add > best:
                    best, pos, rev = gain - add, k, False
                if gain - add_r > best:
                    best, pos, rev = gain - add_r, k, True
            if pos is not None:
                s = seg[::-1] if rev else seg
                tour[:] = rest[:pos + 1] + s + rest[pos + 1:]
                diperbaiki = True
            i += 1
    return diperbaiki


def optimasi_urutan(dur, strategi="optimal"):
    n = len(dur)
    d = [[(dur[i][j] + dur[j][i]) / 2.0 for j in range(n)] for i in range(n)]

    if strategi == "terjauh":
        tour = [0]
        sisa = set(range(1, n))
        if sisa:
            pertama = max(sisa, key=lambda j: d[0][j])
            tour.append(pertama)
            sisa.remove(pertama)
        tour.append(0)
        while sisa:
            nxt = max(sisa, key=lambda j: min(d[j][t] for t in tour[:-1]))
            best_pos = min(
                range(len(tour) - 1),
                key=lambda k: d[tour[k]][nxt] + d[nxt][tour[k + 1]] - d[tour[k]][tour[k + 1]]
            )
            tour.insert(best_pos + 1, nxt)
            sisa.remove(nxt)
        tot = _panjang(tour, d)
        return tour[1:-1], {"nn": tot, "opt": tot}

    tour, sisa, cur = [0], set(range(1, n)), 0
    while sisa:
        nxt = min(sisa, key=lambda j: d[cur][j])
        tour.append(nxt)
        sisa.remove(nxt)
        cur = nxt
    tour.append(0)
    nn = _panjang(tour, d)

    if strategi == "terdekat":
        return tour[1:-1], {"nn": nn, "opt": nn}

    for _ in range(100):
        a = _dua_opt(tour, d)
        b = _or_opt(tour, d)
        if not (a or b):
            break
    return tour[1:-1], {"nn": nn, "opt": _panjang(tour, d)}


# ------------------------------------------------------------------
# Penjadwalan harian
# ------------------------------------------------------------------
def jadwalkan(legs, n, waktu_bongkar_menit):
    rows = []
    hari, t, nyetir, makan, toko_hari = 1, JAM_MULAI, 0.0, False, 0

    def coba(t0, makan0, m, bongkar):
        tiba = t0 + m
        selesai = tiba + bongkar
        ist = 0
        if (not makan0) and selesai > JAM_ISTIRAHAT_SIANG:
            ist = ISTIRAHAT_SIANG_MENIT
        return tiba, selesai + ist, ist

    for i in range(n + 1):
        km, m_osrm, sumber, _ = legs[i]
        m = m_osrm * FAKTOR_WAKTU_TRUK
        pulang = (i == n)
        bongkar = 0 if pulang else waktu_bongkar_menit
        batas_jam = JAM_PULANG_MAKS if pulang else JAM_SELESAI_MAKS

        tiba, selesai, ist = coba(t, makan, m, bongkar)
        melebihi = selesai > batas_jam or nyetir + m > BATAS_NYETIR_JAM * 60
        flag = False
        if melebihi and toko_hari > 0:
            rows[-1]["inap"] = True
            hari += 1
            t, nyetir, makan, toko_hari = JAM_MULAI, 0.0, False, 0
            tiba, selesai, ist = coba(t, makan, m, bongkar)
        elif melebihi:
            flag = True

        rows.append({
            "hari": hari, "km": km, "menit_jalan": m, "menit_osrm": m_osrm, "tiba": tiba,
            "selesai": selesai, "ist": ist, "inap": False, "melebihi": flag,
            "sumber": sumber, "pabrik": pulang,
        })
        t = selesai
        nyetir += m
        if ist:
            makan = True
        toko_hari += 1
    return rows


# ------------------------------------------------------------------
# Proses satu rute
# ------------------------------------------------------------------
def link_gmaps(titik):
    return "https://www.google.com/maps/dir/" + "/".join(f"{a:.6f},{b:.6f}" for a, b in titik)


def proses_rute(nama, gdata, cfg):
    pabrik_lat = cfg["pabrik_lat"]
    pabrik_lng = cfg["pabrik_lng"]
    pabrik_name = cfg["pabrik_name"]
    speed_kmh = cfg["speed_kmh"]
    bongkar_mnt = cfg["bongkar_mnt"]
    strategi = cfg["strategi"]

    df_valid = gdata.dropna(subset=[COL_LAT, COL_LNG]).copy()
    df_kosong = gdata[gdata[COL_LAT].isna() | gdata[COL_LNG].isna()].copy()
    df_unik = df_valid.drop_duplicates(subset=[COL_LAT, COL_LNG]).copy()

    total_src = gdata["total"].dropna().iloc[0] if "total" in gdata and gdata["total"].notna().any() else None
    toko_src = gdata["total_toko"].dropna().iloc[0] if "total_toko" in gdata and gdata["total_toko"].notna().any() else None
    jadwal_no = gdata["name"].dropna().iloc[0] if "name" in gdata and gdata["name"].notna().any() else None

    hasil = {
        "nama": nama, "kosong": df_kosong, "total_src": total_src, "toko_src": toko_src,
        "jadwal_no": jadwal_no, "rows": [], "hari_ringkas": [], "km": 0.0, "menit": 0.0,
        "hari": 0, "opt": None, "geoms": [], "stops": [], "sumber_matriks": "",
    }
    if df_unik.empty:
        return hasil

    label_col = pilih_kolom(gdata, LABEL_CANDIDATES)
    doc_col = pilih_kolom(gdata, DOC_CANDIDATES)
    grp = df_valid.groupby([COL_LAT, COL_LNG])
    jml_baris = grp.size().to_dict()
    docs = {}
    if doc_col:
        docs = grp[doc_col].apply(lambda s: ", ".join(sorted({str(x) for x in s.dropna()}))).to_dict()
    nama_toko = {}
    if label_col:
        nama_toko = grp[label_col].apply(
            lambda s: " / ".join(list(dict.fromkeys(str(x) for x in s.dropna()))[:3])
        ).to_dict()

    toko = df_unik.to_dict("records")
    points = [(pabrik_lat, pabrik_lng)] + [(t[COL_LAT], t[COL_LNG]) for t in toko]

    dur, sumber_m = matriks_waktu(points, speed_kmh)
    urutan, info = optimasi_urutan(dur, strategi)
    hemat = info["nn"] - info["opt"]

    seq = [0] + urutan + [0]
    legs = [
        jarak_osrm(points[a][0], points[a][1], points[b][0], points[b][1], speed_kmh)
        for a, b in zip(seq[:-1], seq[1:])
    ]
    jad = jadwalkan(legs, len(urutan), bongkar_mnt)

    rows, stops = [], []
    prev_nama = pabrik_name
    for i, idx in enumerate(urutan, start=1):
        t = toko[idx - 1]
        lat, lng = t[COL_LAT], t[COL_LNG]
        nm = nama_toko.get((lat, lng)) or f"Toko {i}"
        j = jad[i - 1]
        rows.append({
            **j, "urut": i, "dari": prev_nama, "nama": nm, "lat": lat, "lng": lng,
            "jml_baris": jml_baris.get((lat, lng), 1), "dokumen": docs.get((lat, lng), "")
        })
        stops.append({"no": i, "nama": nm, "lat": lat, "lng": lng, "hari": j["hari"]})
        prev_nama = nm

    j = jad[-1]
    rows.append({
        **j, "urut": len(urutan) + 1, "dari": prev_nama, "nama": pabrik_name,
        "lat": pabrik_lat, "lng": pabrik_lng, "jml_baris": 0, "dokumen": ""
    })

    max_hari = rows[-1]["hari"]
    ringkas, prev_end = [], (pabrik_lat, pabrik_lng)
    for h in range(1, max_hari + 1):
        rh = [r for r in rows if r["hari"] == h]
        tr = [r for r in rh if not r["pabrik"]]
        pts = [prev_end] + [(r["lat"], r["lng"]) for r in tr]
        if h == max_hari:
            pts.append((pabrik_lat, pabrik_lng))
        elif tr:
            prev_end = (tr[-1]["lat"], tr[-1]["lng"])
        ringkas.append({
            "hari": h, "toko": len(tr), "km": sum(r["km"] for r in rh),
            "menit": sum(r["menit_jalan"] for r in rh),
            "mulai": JAM_MULAI, "selesai": rh[-1]["selesai"],
            "inap": (tr[-1]["nama"] if (h < max_hari and tr) else ""),
            "link": link_gmaps(pts) if len(pts) >= 2 else "",
        })

    hasil.update({
        "rows": rows, "hari_ringkas": ringkas, "stops": stops,
        "km": sum(r["km"] for r in rows), "menit": sum(r["menit_jalan"] for r in rows),
        "hari": max_hari, "sumber_matriks": sumber_m,
        "opt": {
            "nn": info["nn"], "opt": info["opt"], "hemat": hemat,
            "pct": (hemat / info["nn"] * 100.0) if info["nn"] else 0.0,
        },
        "geoms": [(legs[i][3], rows[i]["hari"]) for i in range(len(legs))],
    })
    return hasil


# ------------------------------------------------------------------
# Peta OpenStreetMap
# ------------------------------------------------------------------
def _merc(lng, lat):
    x = lng * 20037508.34 / 180.0
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return x, y * 20037508.34 / 180.0


def ambil_tile_latar(xmin, ymin, xmax, ymax):
    from io import BytesIO
    from PIL import Image

    R = 20037508.342789244
    for z in range(17, 4, -1):
        n = 2 ** z
        x0, x1 = int((xmin + R) / (2 * R) * n), int((xmax + R) / (2 * R) * n)
        y0, y1 = int((R - ymax) / (2 * R) * n), int((R - ymin) / (2 * R) * n)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= 30:
            break

    canvas = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256), "white")
    headers = {"User-Agent": "RutePengirimanWeb/1.0"}
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            resp = session.get(
                f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png",
                headers=headers, timeout=15
            )
            resp.raise_for_status()
            tile = Image.open(BytesIO(resp.content)).convert("RGB")
            canvas.paste(tile, ((tx - x0) * 256, (ty - y0) * 256))
            time.sleep(0.05)

    left = x0 / n * 2 * R - R
    right = (x1 + 1) / n * 2 * R - R
    top = R - y0 / n * 2 * R
    bottom = R - (y1 + 1) / n * 2 * R
    return canvas, (left, right, bottom, top)


def gambar_peta(res, out_png, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    pabrik_lat = cfg["pabrik_lat"]
    pabrik_lng = cfg["pabrik_lng"]
    geoms, stops = res["geoms"], res["stops"]
    allx, ally = [], []
    for geom, _ in geoms:
        for lng, lat in geom:
            x, y = _merc(lng, lat)
            allx.append(x)
            ally.append(y)
    fx, fy = _merc(pabrik_lng, pabrik_lat)
    allx.append(fx)
    ally.append(fy)
    padx = (max(allx) - min(allx)) * 0.06 or 1000
    pady = (max(ally) - min(ally)) * 0.06 or 1000

    fig, ax = plt.subplots(figsize=(MAP_W_IN, MAP_H_IN), dpi=MAP_DPI)
    ax.set_xlim(min(allx) - padx, max(allx) + padx)
    ax.set_ylim(min(ally) - pady, max(ally) + pady)
    ax.set_aspect("equal")
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    ax.set_facecolor("#f4f4f4")

    if cfg["map_basemap"]:
        try:
            img, ext = ambil_tile_latar(xlim[0], ylim[0], xlim[1], ylim[1])
            ax.imshow(img, extent=ext, zorder=0, interpolation="bilinear")
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
            ax.set_aspect("equal")
            ax.text(
                0.995, 0.005, "\u00a9 OpenStreetMap contributors",
                transform=ax.transAxes, fontsize=6, ha="right", va="bottom",
                bbox=dict(facecolor="white", alpha=0.7, pad=1, edgecolor="none"), zorder=7
            )
        except Exception:
            pass

    for geom, hari in geoms:
        warna = WARNA_HARI[(hari - 1) % len(WARNA_HARI)]
        xs, ys = zip(*[_merc(lng, lat) for lng, lat in geom])
        ax.plot(xs, ys, color=warna, linewidth=2.2, alpha=0.85, zorder=3)
    padat = len(stops) > 20
    ukuran, fsz = (110, 5.5) if padat else (210, 7)
    for s in stops:
        x, y = _merc(s["lng"], s["lat"])
        warna = WARNA_HARI[(s["hari"] - 1) % len(WARNA_HARI)]
        ax.scatter([x], [y], s=ukuran, c=warna, edgecolors="white", linewidths=1.0, zorder=4)
        ax.text(
            x, y, str(s["no"]), color="white", fontsize=fsz, fontweight="bold",
            ha="center", va="center", zorder=5
        )
    ax.scatter(
        [fx], [fy], s=320, marker="*", c="#2e7d32", edgecolors="white",
        linewidths=1.2, zorder=6
    )

    handles = [
        Line2D([0], [0], marker="*", color="w", markerfacecolor="#2e7d32",
               markersize=14, label="Pabrik (start/finish)")
    ]
    if res.get("tanpa_hari"):
        handles.append(Line2D([0], [0], color=WARNA_HARI[0], linewidth=3, label="Jalur rute"))
    else:
        for h in range(1, res["hari"] + 1):
            handles.append(Line2D([0], [0], color=WARNA_HARI[(h - 1) % len(WARNA_HARI)],
                                  linewidth=3, label=f"Hari {h}"))
    ax.legend(handles=handles, loc="lower left", fontsize=8)
    ax.set_xticks([])
    ax.set_yticks([])
    info = res.get("info") or f"{len(stops)} toko | {res['km']:.1f} km | {res['hari']} hari"
    ax.set_title(f"{res['nama'][:70]}\n{info}", fontsize=10)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


# ------------------------------------------------------------------
# Tulis Excel ke Memory (BytesIO)
# ------------------------------------------------------------------
def nama_sheet_aman(nama, dipakai):
    bersih = "".join("_" if ch in "[]:*?/\\'" else ch for ch in str(nama)).strip()
    bersih = bersih[:31] or "Rute"
    dasar, k = bersih, 2
    while bersih.lower() in dipakai:
        suf = f"-{k}"
        bersih = dasar[:31 - len(suf)] + suf
        k += 1
    dipakai.add(bersih.lower())
    return bersih


def buat_excel_bytes(hasil_list, label_kosong, cfg):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.drawing.image import Image as XLImage

    tampilkan_harian = cfg["tampilkan_harian"]
    make_map = cfg["make_map"]
    pabrik_lat = cfg["pabrik_lat"]
    pabrik_lng = cfg["pabrik_lng"]
    pabrik_name = cfg["pabrik_name"]
    speed_kmh = cfg["speed_kmh"]
    bongkar_mnt = cfg["bongkar_mnt"]
    strategi = cfg["strategi"]

    wb = Workbook()
    ws_ring = wb.active
    ws_ring.title = "Ringkasan"
    dipakai = {"ringkasan", "tanpa koordinat"}

    if tampilkan_harian:
        lebar = {
            "A": 8, "B": 8, "C": 36, "D": 36, "E": 13, "F": 15, "G": 9, "H": 9,
            "I": 52, "J": 11, "K": 11, "L": 10, "M": 40, "N": 20,
        }
    else:
        lebar = {
            "A": 8, "B": 36, "C": 36, "D": 14, "E": 16, "F": 14, "G": 18,
            "H": 11, "I": 11, "J": 10, "K": 40, "L": 20,
        }

    f_judul = Font(bold=True, size=14)
    f_bold = Font(bold=True)
    f_putih = Font(bold=True, color="FFFFFF")
    fill_rute = PatternFill("solid", fgColor="1F4E78")
    fill_head = PatternFill("solid", fgColor="D9E1F2")
    fill_inap = PatternFill("solid", fgColor="FFF2CC")
    f_link = Font(color="0563C1", underline="single")

    def atur_lebar(sh):
        for k, v in lebar.items():
            sh.column_dimensions[k].width = v

    def tulis_baris(sh, r, nilai, font=None, fill=None):
        for k, v in enumerate(nilai):
            c = sh.cell(row=r, column=1 + k, value=v)
            if font:
                c.font = font
            if fill:
                c.fill = fill
        return r + 1

    tmp_dir = tempfile.mkdtemp(prefix="peta_rute_")
    peta_preview = {}
    try:
        def tambah_peta(sh, r, n, res, info_peta):
            if not make_map:
                return r
            try:
                png = os.path.join(tmp_dir, f"peta_{n}.png")
                gambar_peta({**res, **info_peta}, png, cfg)
                sh.add_image(XLImage(png), f"A{r}")
                with open(png, "rb") as f_img:
                    peta_preview[res["nama"]] = f_img.read()
                return r + 35
            except Exception:
                return r

        def tulis_link_bagian(sh, r, res):
            pts = (
                [(pabrik_lat, pabrik_lng)] + [(s["lat"], s["lng"]) for s in res["stops"]]
                + [(pabrik_lat, pabrik_lng)]
            )
            names = [pabrik_name] + [s["nama"] for s in res["stops"]] + [pabrik_name]
            sh.cell(row=r, column=1, value=(
                "LINK GOOGLE MAPS (klik untuk membuka; rute dibagi per maks 10 titik agar tidak terpotong)"
            )).font = f_bold
            r += 1
            s, k = 0, 1
            while s < len(pts) - 1:
                e = min(s + 9, len(pts) - 1)
                c = sh.cell(row=r, column=1, value=f"Bagian {k}: {names[s]} -> {names[e]}")
                c.hyperlink = link_gmaps(pts[s:e + 1])
                c.font = f_link
                r += 1
                s, k = e, k + 1
            return r + 1

        def blok_sederhana(sh, r, n, res):
            n_toko = len(res["stops"])
            truk_menit = res["menit"]
            bongkar = n_toko * bongkar_mnt
            sh.cell(row=r, column=1, value=(
                f"No. Jadwal: {res['jadwal_no']} | Total (Odoo): {res['total_src']} | "
                f"Toko (Odoo): {res['toko_src']} | Toko unik (koordinat): {n_toko} | "
                f"Tanpa koordinat: {len(res['kosong'])} | Strategi: {strategi.upper()}"
            )).font = f_bold
            r += 1
            sh.cell(row=r, column=1, value=(
                f"TOTAL JARAK: {res['km']:.1f} km | WAKTU JALAN: {truk_menit / 60:.1f} jam (kec. {speed_kmh} km/j) | "
                f"+ bongkar {bongkar / 60:.1f} jam ({bongkar_mnt} mnt/toko) = {(truk_menit + bongkar) / 60:.1f} jam total"
            )).font = f_bold
            r += 2

            r = tulis_link_bagian(sh, r, res)

            info_peta = {
                "geoms": [(g, 1) for g, _ in res["geoms"]],
                "stops": [{**s, "hari": 1} for s in res["stops"]],
                "hari": 1, "tanpa_hari": True,
                "info": f"{n_toko} toko | {res['km']:.1f} km | {truk_menit / 60:.1f} jam jalan ({strategi.upper()})",
            }
            r = tambah_peta(sh, r, n, res, info_peta)

            tulis_baris(sh, r, [
                "Urutan", "Dari", "Ke", "Jarak Leg (km)", "Jarak Kumulatif (km)",
                "Waktu Truk (mnt)", "Waktu Kumulatif Truk (mnt)",
                "Lat", "Lng", "Jml Baris", "Dokumen", "Link Google Maps"
            ], f_bold, fill_head)
            r += 1
            cum_km = cum_t = 0.0
            prev = (pabrik_lat, pabrik_lng)
            for x in res["rows"]:
                cum_km += x["km"]
                cum_t += x["menit_jalan"]
                tulis_baris(sh, r, [
                    x["urut"], x["dari"], x["nama"], round(x["km"], 2), round(cum_km, 2),
                    round(x["menit_jalan"], 1), round(cum_t, 1),
                    x["lat"], x["lng"], x["jml_baris"], x["dokumen"], None
                ])
                c = sh.cell(row=r, column=12, value="Buka rute leg ini")
                c.hyperlink = link_gmaps([prev, (x["lat"], x["lng"])])
                c.font = f_link
                prev = (x["lat"], x["lng"])
                r += 1
            tulis_baris(
                sh, r, ["", "", "TOTAL", round(res["km"], 2), "", round(truk_menit, 1)],
                f_bold, fill_head
            )
            return r + 3

        def blok_harian(sh, r, n, res):
            sh.cell(row=r, column=1, value=(
                f"Total (Odoo): {res['total_src']} | Toko (Odoo): {res['toko_src']} | "
                f"Toko unik (koordinat): {len(res['stops'])} | Jarak: {res['km']:.1f} km | "
                f"Waktu jalan: {res['menit'] / 60:.1f} jam | ESTIMASI: {res['hari']} HARI"
            )).font = f_bold
            r += 2

            tulis_baris(sh, r, [
                "Hari", "Toko", "Menginap di", "Link Google Maps", "Jarak (km)",
                "Waktu Jalan (mnt)", "Mulai", "Selesai"
            ], f_bold, fill_head)
            r += 1
            for h in res["hari_ringkas"]:
                tulis_baris(sh, r, [
                    h["hari"], h["toko"], h["inap"] or "(kembali ke pabrik)", None,
                    round(h["km"], 1), round(h["menit"]), fmt_jam(h["mulai"]),
                    fmt_jam(h["selesai"])
                ])
                if h["link"]:
                    c = sh.cell(row=r, column=4, value="Buka di Google Maps")
                    c.hyperlink = h["link"]
                    c.font = f_link
                r += 1
            r += 1

            r = tambah_peta(sh, r, n, res, {})

            tulis_baris(sh, r, [
                "Hari", "Urutan", "Dari", "Ke", "Jarak Leg (km)", "Waktu Jalan (mnt)",
                "Tiba", "Selesai", "Catatan", "Lat", "Lng", "Jml Baris", "Dokumen",
                "Link Google Maps"
            ], f_bold, fill_head)
            r += 1
            hari_sebelumnya = 0
            prev = (pabrik_lat, pabrik_lng)
            for x in res["rows"]:
                cat = []
                if x["hari"] != hari_sebelumnya and x["hari"] > 1:
                    cat.append(f"Mulai Hari {x['hari']} {fmt_jam(JAM_MULAI)} dari lokasi menginap")
                if x["ist"]:
                    cat.append(f"Istirahat siang {x['ist']} mnt")
                if x["inap"]:
                    cat.append("MENGINAP di sekitar sini")
                if x["melebihi"]:
                    cat.append("Leg ini melebihi batas jam kerja/nyetir")
                if x["pabrik"]:
                    cat.append("Tiba kembali di pabrik")
                hari_sebelumnya = x["hari"]
                tulis_baris(
                    sh, r, [
                        x["hari"], x["urut"], x["dari"], x["nama"], round(x["km"], 2),
                        round(x["menit_jalan"], 1), fmt_jam(x["tiba"]), fmt_jam(x["selesai"]),
                        "; ".join(cat), x["lat"], x["lng"], x["jml_baris"], x["dokumen"], None
                    ],
                    fill=fill_inap if x["inap"] else None
                )
                c = sh.cell(row=r, column=14, value="Buka rute leg ini")
                c.hyperlink = link_gmaps([prev, (x["lat"], x["lng"])])
                c.font = f_link
                prev = (x["lat"], x["lng"])
                r += 1
            return r + 2

        daftar = []
        for n, res in enumerate(hasil_list, start=1):
            sch = res.get("jadwal_no")
            sch_txt = str(sch).strip() if sch is not None and str(sch).strip().lower() not in ("", "nan") else ""
            nama_dasar = sch_txt or str(res["nama"])
            nama_sh = nama_sheet_aman(nama_dasar, dipakai)
            sh = wb.create_sheet(nama_sh)
            atur_lebar(sh)

            sh.cell(row=1, column=1, value=f"RENCANA PENGIRIMAN & RUTE - {nama_dasar}").font = f_judul
            if tampilkan_harian:
                sh.cell(row=2, column=1, value=(
                    f"Berangkat {fmt_jam(JAM_MULAI)} dari pabrik | bongkar selesai maks {fmt_jam(JAM_SELESAI_MAKS)} | "
                    f"bongkar {bongkar_mnt} mnt/toko | istirahat siang {ISTIRAHAT_SIANG_MENIT} mnt | "
                    f"maks nyetir {BATAS_NYETIR_JAM} jam/hari | "
                    f"menginap di lokasi bila batas harian tercapai"
                ))

            r = 4
            r = tulis_baris(
                sh, r, [f"RUTE: {res['nama']}"] + [""] * (13 if tampilkan_harian else 11),
                f_putih, fill_rute
            )
            if not res["rows"]:
                sh.cell(row=r, column=1, value="Tidak ada baris dengan koordinat valid.")
                r += 2
            elif tampilkan_harian:
                r = blok_harian(sh, r, n, res)
            else:
                r = blok_sederhana(sh, r, n, res)

            n_toko = len(res["stops"])
            daftar.append({
                "sheet": nama_sh, "rute": res["nama"], "toko": n_toko, "km": res["km"],
                "jam_jalan": res["menit"] / 60.0,
                "jam_total": (res["menit"] + n_toko * bongkar_mnt) / 60.0,
                "hari": res["hari"], "kosong": len(res["kosong"]),
            })

        # Sheet Ringkasan
        sh = ws_ring
        sh.sheet_properties.tabColor = "1F4E78"
        for k, v in {"A": 6, "B": 24, "C": 36, "D": 12, "E": 14, "F": 16, "G": 20, "H": 10, "I": 16}.items():
            sh.column_dimensions[k].width = v
        sh.cell(row=1, column=1, value="RINGKASAN RENCANA PENGIRIMAN").font = f_judul
        sh.cell(row=2, column=1, value=(
            f"Strategi: {strategi.upper()} | Kecepatan: {speed_kmh} km/jam | "
            f"Bongkar: {bongkar_mnt} mnt/toko. Klik nomor SCH untuk membuka sheet-nya."
        ))
        r = tulis_baris(sh, 4, [
            "No", "SCH (Sheet)", "Rute", "Toko", "Jarak (km)", "Waktu Jalan (jam)",
            "Jalan + Bongkar (jam)", "Hari", "Tanpa Koordinat"
        ], f_bold, fill_head)
        for k, d in enumerate(daftar, start=1):
            tulis_baris(sh, r, [
                k, d["sheet"], d["rute"], d["toko"], round(d["km"], 1),
                round(d["jam_jalan"], 1), round(d["jam_total"], 1),
                d["hari"] if tampilkan_harian else "", d["kosong"]
            ])
            c = sh.cell(row=r, column=2)
            c.hyperlink = f"#'{d['sheet']}'!A1"
            c.font = f_link
            r += 1
        tulis_baris(sh, r, [
            "", "TOTAL", "", sum(d["toko"] for d in daftar),
            round(sum(d["km"] for d in daftar), 1),
            round(sum(d["jam_jalan"] for d in daftar), 1),
            round(sum(d["jam_total"] for d in daftar), 1), "",
            sum(d["kosong"] for d in daftar)
        ], f_bold, fill_head)

        # Sheet Tanpa Koordinat
        kosong_semua = [(res, row) for res in hasil_list for _, row in res["kosong"].iterrows()]
        if kosong_semua:
            sh = wb.create_sheet("Tanpa Koordinat")
            for k, v in {"A": 6, "B": 24, "C": 36, "D": 44, "E": 30}.items():
                sh.column_dimensions[k].width = v
            sh.cell(row=1, column=1, value="BARIS TANPA KOORDINAT (perlu dilengkapi lat/lng di Odoo)").font = f_judul
            r = tulis_baris(sh, 3, ["No", "SCH", "Rute", "Toko / Baris", "Dokumen"], f_bold, fill_head)
            for k, (res, row) in enumerate(kosong_semua, start=1):
                lab = str(row.get(label_kosong, "")) if label_kosong else ""
                dok = ""
                for dc in DOC_CANDIDATES:
                    if dc in row.index and pd.notna(row[dc]):
                        dok = str(row[dc])
                        break
                tulis_baris(sh, r, [k, res.get("jadwal_no") or "", res["nama"], lab, dok])
                r += 1

        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return bio.getvalue(), daftar, peta_preview
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ==================================================================
# ANTARMUKA PENGGUNA (STREAMLIT UI)
# ==================================================================
st.title("🚚 Optimasi & Perencanaan Rute Pengiriman")
st.caption("Upload file ekspor Odoo (.csv / .xlsx) untuk menghitung urutan rute optimal, estimasi waktu tempuh, peta jalan, dan laporan Excel multi-sheet.")

with st.sidebar:
    st.header("⚙️ Parameter Pengiriman")

    speed_kmh = st.number_input("Kecepatan rata-rata truk (km/jam)", min_value=10.0, max_value=120.0, value=40.0, step=5.0)
    bongkar_mnt = st.number_input("Waktu bongkar per toko (menit)", min_value=0, max_value=240, value=25, step=5)

    pilihan_strategi = st.radio(
        "Strategi Pengurutan Rute",
        options=["optimal", "terdekat", "terjauh"],
        format_func=lambda x: {
            "optimal": "Optimal (Nearest Neighbor + 2-Opt + Or-Opt)",
            "terdekat": "Terdekat (Nearest Neighbor)",
            "terjauh": "Terjauh (Farthest Insertion - Area Luar Dulu)",
        }[x],
        index=0,
    )

    st.divider()
    st.subheader("📍 Titik Berangkat (Pabrik / Gudang)")
    pabrik_name = st.text_input("Nama Titik Awal", value="PABRIK (START/FINISH)")
    col_lat, col_lng = st.columns(2)
    with col_lat:
        pabrik_lat = st.number_input("Latitude", value=-6.908125, format="%.6f")
    with col_lng:
        pabrik_lng = st.number_input("Longitude", value=107.546261, format="%.6f")

    st.divider()
    st.subheader("🗺️ Opsi Tambahan")
    tampilkan_harian = st.checkbox("Hitung pembagian hari & jadwal menginap", value=False)
    make_map = st.checkbox("Sertakan gambar peta di Excel", value=True)
    map_basemap = st.checkbox("Gunakan latar jalan OpenStreetMap", value=True, disabled=not make_map)

uploaded_file = st.file_uploader(
    "Pilih atau tarik file jadwal Odoo ke sini (.xlsx, .xls, atau .csv)",
    type=["xlsx", "xls", "csv"],
)

if uploaded_file is not None:
    if st.button("🚀 Hitung & Optimasi Rute", type="primary", use_container_width=True):
        cfg = {
            "pabrik_lat": pabrik_lat,
            "pabrik_lng": pabrik_lng,
            "pabrik_name": pabrik_name,
            "speed_kmh": speed_kmh,
            "bongkar_mnt": bongkar_mnt,
            "strategi": pilihan_strategi,
            "tampilkan_harian": tampilkan_harian,
            "make_map": make_map,
            "map_basemap": map_basemap,
        }

        with st.status("Memproses data pengiriman...", expanded=True) as status:
            df = baca_file_upload(uploaded_file)
            df = gabungkan_5_proses_csv(df, status_box=status)

            missing_cols = [c for c in (COL_LAT, COL_LNG) if c not in df.columns]
            if missing_cols:
                status.update(label="Kolom koordinat tidak ditemukan!", state="error")
                st.error(f"Kolom wajib tidak ada di file: {', '.join(missing_cols)}")
                st.write("Kolom yang tersedia:", list(df.columns))
                st.stop()

            for col in (COL_LAT, COL_LNG):
                df[col] = bersihkan_angka(df[col])

            if "total" in df.columns and df["total"].notna().any():
                pos = list(df.columns).index("total")
                label_col = df.columns[pos - 1] if pos > 0 else None
                df["_grp"] = df["total"].notna().cumsum()
                df["_label"] = df[label_col].ffill() if label_col else df["_grp"]
                df = df[df["_grp"] > 0]
                groups = [(str(g["_label"].iloc[0]).strip(), g) for _, g in df.groupby("_grp", sort=False)]
            elif pilih_kolom(df, GROUP_CANDIDATES):
                group_col = pilih_kolom(df, GROUP_CANDIDATES)
                df[group_col] = df[group_col].ffill()
                groups = [(str(k), g) for k, g in df.groupby(group_col, sort=False)]
            else:
                groups = [("SEMUA", df)]

            hasil_list = []
            prog_bar = st.progress(0, text="Menghitung rute...")
            total_g = len(groups)
            for idx_g, (nama, gdata) in enumerate(groups, start=1):
                status.write(f"Menghitung rute ({idx_g}/{total_g}): **{nama}** ({len(gdata)} baris)...")
                res = proses_rute(nama, gdata, cfg)
                hasil_list.append(res)
                prog_bar.progress(idx_g / total_g, text=f"Selesai {idx_g} dari {total_g} rute")

            status.write("📊 Menyusun file Excel & merender peta...")
            excel_bytes, daftar_ringkas, peta_preview = buat_excel_bytes(
                hasil_list, pilih_kolom(df, LABEL_CANDIDATES), cfg
            )
            status.update(label="Optimasi rute selesai!", state="complete", expanded=False)

        # Simpan ke session_state agar hasil tidak hilang saat tombol download diklik
        base_name = os.path.splitext(uploaded_file.name)[0]
        st.session_state["hasil_excel"] = excel_bytes
        st.session_state["nama_excel"] = f"{base_name}_HASIL_RUTE.xlsx"
        st.session_state["daftar_ringkas"] = daftar_ringkas
        st.session_state["peta_preview"] = peta_preview
        from collections import Counter
        st.session_state["osrm_diag"] = Counter(OSRM_LOG).most_common(5)
        st.session_state["osrm_servers"] = list(OSRM_BASE_URLS)
        st.session_state["ada_estimasi"] = any(
            x["sumber"] == "ESTIMASI" for res in hasil_list for x in res["rows"]
        )

if "hasil_excel" in st.session_state:
    st.success("Perhitungan selesai! Silakan unduh file Excel atau lihat ringkasannya di bawah.")

    st.download_button(
        label=f"📥 Download {st.session_state['nama_excel']}",
        data=st.session_state["hasil_excel"],
        file_name=st.session_state["nama_excel"],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )

    if st.session_state.get("ada_estimasi"):
        st.warning("Catatan: Sebagian leg memakai jarak ESTIMASI garis lurus karena server OSRM tidak terjangkau setelah beberapa kali percobaan. Klik proses ulang: leg yang sudah berhasil tersimpan di cache, jadi hanya yang gagal yang dicoba lagi.")
        with st.expander("Detail teknis OSRM (untuk diagnosa)"):
            st.write("Server dicoba:", st.session_state.get("osrm_servers"))
            diag = st.session_state.get("osrm_diag") or []
            if diag:
                for alasan, jml in diag:
                    st.code(f"{jml}x  {alasan}")
            else:
                st.write("Tidak ada error tercatat (kemungkinan hasil dari proses sebelumnya).")

    st.subheader("📋 Ringkasan Rute")
    df_ringkas = pd.DataFrame(st.session_state["daftar_ringkas"])
    st.dataframe(df_ringkas, use_container_width=True, hide_index=True)

    if st.session_state.get("peta_preview"):
        st.subheader("🗺️ Preview Peta Rute")
        pilih_peta = st.selectbox("Pilih rute untuk melihat peta:", list(st.session_state["peta_preview"].keys()))
        if pilih_peta:
            st.image(st.session_state["peta_preview"][pilih_peta], use_container_width=True)
