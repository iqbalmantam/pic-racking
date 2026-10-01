#!/usr/bin/env python3
"""Kumpulkan data inspeksi dari 33 Google Sheet PIC menjadi satu file data.json.

Dijalankan oleh GitHub Actions (lihat .github/workflows/update-data.yml).
Hanya memakai pustaka standar Python, tidak perlu pip install.

Format keluaran (dibaca langsung oleh dashboard):
{
  "generated": "2026-09-29T09:30:00+07:00",   # waktu data terakhir berubah
  "raks": ["Rak A01 - ...", ...],               # semua rak, termasuk yang belum ada laporan
  "data": [[header...], [baris...], ...]        # kolom sama seperti sheet MasterData lama
}
"""
import concurrent.futures as cf
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = json.loads((Path(__file__).parent / "sources.json").read_text(encoding="utf-8"))
OUT = ROOT / "data.json"
HEADER = ["Rak_PIC", "Timestamp", "Tanggal", "Shift",
          "Pallet_Rusak", "Segel_Baik", "Produk_Penyok", "Label_Baik"]
WIB = timezone(timedelta(hours=7))
UA = {"User-Agent": "Mozilla/5.0 (pic-racking data builder)"}


def rak_key(name):
    """Kunci rak tanpa nama PIC: 'Rak A01 - Arik' -> 'Rak A01'. PIC boleh berganti, rak tetap."""
    return str(name).split(" - ")[0].strip()


def http_get(url, retries=3, timeout=60):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8-sig")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
    raise RuntimeError(f"gagal unduh: {last}")


# ---------- Sumber utama: gviz JSON (tanggal tidak ambigu) ----------
def cell_to_py(cell, ctype):
    """Ubah sel gviz menjadi nilai Python. Tanggal -> objek datetime."""
    if not cell:
        return ""
    v = cell.get("v")
    if v is None:
        return ""
    if isinstance(v, str) and v.startswith("Date("):
        parts = [int(x) for x in re.findall(r"-?\d+", v)]
        y, mo, d = parts[0], parts[1] + 1, parts[2]   # bulan gviz mulai dari 0
        hh, mi, ss = (parts + [0, 0, 0])[3:6]
        return datetime(y, mo, d, hh, mi, ss)
    return v


def fetch_gviz(sid, gid):
    url = (f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq"
           f"?tqx=out:json&gid={gid}&headers=1")
    text = http_get(url)
    m = re.search(r"setResponse\((.*)\);?\s*$", text, re.S)
    if not m:
        raise RuntimeError("respons gviz tidak dikenali (sheet belum dibagikan publik?)")
    obj = json.loads(m.group(1))
    if obj.get("status") == "error":
        raise RuntimeError("gviz error: " + json.dumps(obj.get("errors", ""))[:200])
    table = obj["table"]
    cols = table["cols"]
    labels = [(c.get("label") or "").strip() for c in cols]
    rows = [[cell_to_py(c, cols[i].get("type")) for i, c in enumerate(r["c"])]
            for r in table["rows"]]
    if not any(labels):                 # header tidak terdeteksi -> baris pertama = header
        if not rows:
            return [], []
        labels = [str(x).strip() for x in rows[0]]
        rows = rows[1:]
    return labels, rows


# ---------- Cadangan: export CSV ----------
def fetch_csv(sid, gid):
    url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    text = http_get(url)
    if text.lstrip().lower().startswith("<!doctype html") or "<html" in text[:200].lower():
        raise RuntimeError("CSV berisi halaman HTML (sheet belum dibagikan publik?)")
    data = list(csv.reader(io.StringIO(text)))
    if not data:
        return [], []
    return [h.strip() for h in data[0]], data[1:]


# ---------- Pemetaan kolom (sama dengan skrip Apps Script lama) ----------
def find_idx(header, keywords):
    for i, h in enumerate(header):
        if any(k in h for k in keywords):
            return i
    return -1


def build_rows(name, labels, rows):
    header = [str(h).lower().strip() for h in labels]
    i_tgl = find_idx(header, ("tanggal", "date", "waktu", "timestamp"))
    i_shift = find_idx(header, ("shift",))
    i_pallet = find_idx(header, ("pallet",))
    i_segel = find_idx(header, ("segel",))
    i_penyok = find_idx(header, ("penyok",))
    i_label = find_idx(header, ("label",))

    def pick(r, i, default="-"):
        if i == -1:
            return default
        return r[i] if i < len(r) else ""

    out = []
    for r in rows:
        if "".join(str(x) for x in r).strip() == "":
            continue
        if i_tgl != -1:
            tgl = pick(r, i_tgl)
        else:
            tgl = (r[0] if r and r[0] != "" else "-")
        if isinstance(tgl, datetime):
            tgl = tgl.strftime("%Y-%m-%d")
        vals = [pick(r, i_shift), pick(r, i_pallet), pick(r, i_segel),
                pick(r, i_penyok), pick(r, i_label)]
        vals = [v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v for v in vals]
        out.append([name, "", tgl] + vals)
    return out


def load_source(src):
    name, sid, gid = src["name"], src["id"], src["gid"]
    try:
        labels, rows = fetch_gviz(sid, gid)
    except Exception as e1:
        try:
            labels, rows = fetch_csv(sid, gid)
        except Exception as e2:
            return name, None, f"gviz: {e1} | csv: {e2}"
    return name, build_rows(name, labels, rows), None


def main():
    previous = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            for row in old.get("data", [])[1:]:
                previous.setdefault(rak_key(row[0]), []).append(row)
        except Exception:
            pass

    results = {}
    failed = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for name, rows, err in ex.map(load_source, SOURCES):
            if rows is None:
                failed.append((name, err))
                # pertahankan data lama untuk rak ini (dicocokkan lewat kode rak, bukan nama PIC)
                rows = [[name] + r[1:] for r in previous.get(rak_key(name), [])]
                print(f"[GAGAL] {name}: {err} (memakai {len(rows)} baris lama)", file=sys.stderr)
            else:
                print(f"[OK]    {name}: {len(rows)} baris")
            results[name] = rows

    if len(failed) == len(SOURCES):
        print("Semua sumber gagal dibaca; data.json tidak diubah.", file=sys.stderr)
        sys.exit(1)

    all_rows = [r for s in SOURCES for r in results[s["name"]]]
    payload = {
        "generated": datetime.now(WIB).isoformat(timespec="seconds"),
        "raks": [s["name"] for s in SOURCES],
        "data": [HEADER] + all_rows,
    }

    # Tulis hanya jika isi berubah, supaya riwayat commit tidak dipenuhi perubahan kosong
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            if old.get("data") == payload["data"] and old.get("raks") == payload["raks"]:
                print("Tidak ada perubahan data.")
                return
        except Exception:
            pass
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"data.json ditulis: {len(all_rows)} baris dari {len(SOURCES) - len(failed)}/{len(SOURCES)} sumber")


if __name__ == "__main__":
    main()
