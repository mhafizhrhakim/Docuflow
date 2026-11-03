# Arsitektur DocuFlow

## Alur aplikasi

```text
Browser (Vite + PDF.js)
        │ /api/*
        ▼
FastAPI (backend.main:app)
        │
        ├── PyMuPDF: buka PDF, baca teks/struktur native, klasifikasikan halaman
        ├── RapidOCR + ONNX Runtime: baca halaman scan secara lokal
        └── table_extractor: deteksi tabel, rekonstruksi baris/kolom, tandai hasil yang perlu ditinjau
```

`npm run dev` menjalankan Vite di `127.0.0.1:5173` dan Uvicorn di `127.0.0.1:8000`. Vite meneruskan route `/api` ke backend. Entry point backend adalah `backend.main:app`.

## Alur pemrosesan dokumen

1. Frontend memuat preview menggunakan PDF.js.
2. Frontend mengirim PDF ke `POST /api/documents/inspect`.
3. `document_parser.inspect_pdf()` memeriksa setiap halaman dan memakai klasifikasi per halaman:
   - Halaman dengan teks/struktur native diproses melalui jalur PyMuPDF.
   - Halaman scan atau halaman dengan gambar halaman penuh diarahkan ke OCR lokal RapidOCR.
   - Dokumen campuran dapat menggunakan jalur berbeda pada halaman berbeda.
4. `table_extractor.extract_tables_from_page()` mencari kandidat tabel native dan membentuk baris/kolom dari struktur atau posisi teks. Untuk scan, hasil kata OCR dan koordinatnya dapat dipakai dalam rekonstruksi geometri.
5. Hasil tabel, metadata halaman, koordinat sumber yang tersedia, status OCR, dan indikator yang perlu ditinjau dikirim ke frontend.
6. Frontend menampilkan hasil pada halaman/dokumen yang sesuai. Copy memakai TSV; ekspor CSV dibuat dari tabel.

Header yang tidak ditemukan tidak selalu menghentikan ekstraksi. Header continuation dapat diwarisi dalam kondisi yang memenuhi aturan parser. Nilai yang meragukan tidak seharusnya dianggap benar tanpa pemeriksaan sumber.

## Workflow frontend

### Single PDF

Workflow Single menerima satu PDF, menampilkan preview dan navigasi halaman, klasifikasi halaman, serta tabel hasil. Progress memakai endpoint progress yang sama dengan workflow Multi-document.

### Multi-document

Workflow Multi membuat kartu dan viewer terpisah untuk setiap dokumen. Setiap dokumen dikirim dan diproses secara independen melalui endpoint inspeksi yang sama; progress kartu dan progres keseluruhan didasarkan pada halaman yang dilaporkan selesai.

## Progress API dan penyimpanan

Progress opsional dilaporkan melalui `GET /api/documents/inspect/progress/{progress_id}`. Data progress berisi status/angka proses, bukan teks dokumen atau hasil tabel. Penyimpanan saat ini berupa dictionary di memori satu proses dengan TTL 30 menit; ini bukan penyimpanan bersama lintas worker.

PDF dibaca selama request dan tidak disimpan sebagai dokumen permanen oleh endpoint inspeksi. Gambar raster sementara untuk OCR dibuat di temporary directory dan dihapus melalui `finally` pada jalur normal/exception Python.

## Dependency dan eksperimen

- `frontend/vite.config.js`: root frontend, dev server, dan proxy API.
- `package.json` / `package-lock.json`: Vite, `concurrently`, PDF.js.
- `backend/requirements.txt`: FastAPI, Uvicorn, PyMuPDF, RapidOCR, ONNX Runtime, dan dependency backend lainnya.
- `tools/rapidocr_pilot/` serta `tools/paddleocr_vl_pilot/` adalah eksperimen pendukung; keduanya bukan bagian dari request aplikasi.

## Tes

Tes backend berada di `tests/`. Jalankan pytest dengan direktori tersebut secara eksplisit agar discovery tidak mengimpor skrip eksperimen OCR di `tools/` atau salinan di `scripts/tools/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
node --test tests/frontend_table_interactions.test.mjs
npm run build
```
