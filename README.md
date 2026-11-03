# DocuFlow — Ekstraksi dan Audit Dokumen

DocuFlow adalah aplikasi lokal untuk melihat PDF dan mengekstrak tabel agar hasilnya dapat diperiksa dan disalin ke alur kerja audit.

## Fitur saat ini

- **Single PDF:** unggah satu PDF, lihat preview, navigasi halaman, lihat klasifikasi halaman dan tabel ekstraksi.
- **Multi-document:** unggah beberapa PDF; setiap dokumen memiliki preview, status, progres, dan hasil ekstraksi sendiri.
- **Ekstraksi per halaman:** halaman digital memakai teks dan struktur native PDF. Halaman scan diproses dengan RapidOCR lokal; PDF campuran ditangani per halaman.
- **Pemeriksaan hasil:** hasil tabel mempertahankan sumber dokumen dan nomor halaman jika tersedia. Baris atau sel yang meragukan dapat ditandai untuk ditinjau.
- **Pencarian dan salin:** pencarian tabel, pemilihan baris, salin baris terpilih, dan salin seluruh tabel dalam format TSV.
- **Ekspor:** tabel dapat diunduh sebagai CSV.
- **Preview PDF:** PDF.js menampilkan dokumen di browser, dengan navigasi halaman dan kontrol zoom.

Ekstraksi tidak menjamin semua format PDF terbaca sempurna. Periksa hasil dan penanda tinjauan terhadap sumber dokumen, terutama pada PDF scan.

## Teknologi dan struktur

- `frontend/`: Vite, HTML/CSS/JavaScript, PDF.js.
- `backend/`: FastAPI, PyMuPDF untuk pemrosesan PDF, RapidOCR dan ONNX Runtime untuk halaman scan.
- `tests/`: tes backend dan frontend.
- `tools/`: utilitas eksperimen OCR; bukan bagian dari alur aplikasi.
- `samples/real_documents/`: dataset lokal. PDF di folder ini diabaikan oleh Git; `samples/README.md` tetap dapat dilacak.

Unggahan diproses oleh backend tanpa penyimpanan permanen aplikasi. Gambar halaman sementara untuk OCR dihapus pada jalur proses normal. Progress disimpan di memori proses. Aplikasi belum menyediakan autentikasi pengguna atau penyimpanan progress bersama untuk deployment multi-worker.

## Menjalankan aplikasi lokal

Persyaratan: Node.js yang kompatibel dengan Vite 6 dan Python 3.11 dengan environment backend yang berfungsi.

Pada setup baru, buat dan siapkan environment Python:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
```

Pasang dependency frontend sesuai lockfile, lalu jalankan frontend dan backend dari root proyek:

```powershell
npm ci
npm run dev
```

- Frontend: http://127.0.0.1:5173
- Backend API: http://127.0.0.1:8000
- Dokumentasi API: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/api/health

`npm run dev` menjalankan Vite dan Uvicorn secara bersamaan. Aktifkan `.venv` sebelum menjalankannya agar perintah `python` memakai environment backend.

## Menjalankan pada server internal

Bangun frontend dan jalankan backend tanpa mode reload. Konfigurasikan `.env` pada server dengan alamat frontend yang sebenarnya; jangan gunakan `FRONTEND_ORIGIN` lokal pada deployment.

```env
APP_ENV=production
APP_HOST=0.0.0.0
APP_PORT=8000
FRONTEND_ORIGIN=https://docuflow.internal.example
MAX_UPLOAD_MB=50
INSPECTION_WORKERS=2
INSPECTION_QUEUE_SLOTS=2
OCR_INTRA_OP_THREADS=2
```

```powershell
npm run build
node scripts/start-backend.mjs
```

Frontend hasil build perlu disajikan oleh web server internal atau reverse proxy. Arahkan `/api` ke backend dan sediakan TLS serta autentikasi perusahaan di reverse proxy sebelum dokumen vendor digunakan. Batasi akses jaringan ke server tersebut.

Kapasitas ekstraksi dibatasi per proses: `INSPECTION_WORKERS` adalah jumlah job aktif dan `INSPECTION_QUEUE_SLOTS` adalah jumlah permintaan tambahan yang boleh menunggu. Saat semua slot penuh, API mengembalikan HTTP 503 agar antrean dan pemakaian memori tidak tumbuh tanpa batas; pengguna perlu mencoba kembali. Mulai dari nilai contoh di atas, lalu sesuaikan hanya setelah pengukuran pada server target. Jangan menjalankan beberapa worker Uvicorn: progress dan cache saat ini berada di memori/proses lokal. Untuk multi-worker atau beberapa instance dibutuhkan penyimpanan progress/job bersama.

`OCR_INTRA_OP_THREADS` membatasi jumlah thread CPU ONNX per inferensi (default `2`, dibatasi maksimal ke jumlah CPU logis yang terlihat). Saat startup, backend memuat sesi deteksi, orientasi, dan pengenalan RapidOCR sebelum health check menyatakan OCR `ready`; ini memindahkan inisialisasi model dari upload pertama ke startup server. Pada satu halaman scan nyata di mesin pengembangan, nilai `2` memangkas inferensi dari sekitar 22,0 detik menjadi 16,8 detik dengan teks, confidence, bounding box, dan hasil tabel yang sama pada pengulangan; hasil ini bukan jaminan waktu untuk dokumen atau server lain. Ukur kembali pada mesin deployment sebelum mengubah nilai atau menaikkan concurrency.

Batas ukuran unggahan bukan jaminan waktu selesai. Belum ada batas jumlah halaman atau kompleksitas halaman maupun SLA waktu ekstraksi. Jangan menjanjikan waktu universal; tetapkan SLA hanya setelah mengukur PDF digital, scan, campuran, dan beban bersamaan pada spesifikasi server deployment. Aplikasi belum memiliki autentikasi bawaan dan belum siap dipaparkan langsung ke internet.

## Pemeriksaan perubahan

Jalankan tes Python hanya pada direktori tes aplikasi. Jangan memakai discovery pytest dari root: folder eksperimen OCR berisi skrip bernama `test_*.py` untuk penggunaan manual.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
node --test tests/frontend_table_interactions.test.mjs
npm run build
```

## Dependency utama

- Frontend: `package.json` dan `package-lock.json`.
- Backend: `backend/requirements.txt`.
- `requirements-ocr.txt` menyediakan subset dependency OCR.

Jangan jalankan skrip di `tools/` sebagai bagian dari tes otomatis. Beberapa eksperimen dapat menjalankan inferensi dan menulis artefak ke `ocr-pilot-output/`.
