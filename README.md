# DocuFlow — Ekstraksi dan Audit Dokumen

DocuFlow adalah aplikasi lokal untuk melihat PDF dan mengekstrak tabel agar hasilnya dapat diperiksa dan disalin ke alur kerja audit.

## Fitur saat ini

- **Single PDF:** unggah satu PDF, lihat preview, navigasi halaman, lihat klasifikasi halaman dan tabel ekstraksi.
- **Multi-document:** unggah beberapa PDF; setiap dokumen memiliki preview, status, progres, dan hasil ekstraksi sendiri.
- **Ekstraksi per halaman:** halaman digital memakai teks dan struktur native PDF. Halaman scan diproses dengan RapidOCR lokal; PDF campuran ditangani per halaman.
- **Pemeriksaan hasil:** hasil tabel mempertahankan sumber dokumen dan nomor halaman jika tersedia. Baris atau sel yang meragukan dapat ditandai untuk ditinjau.
- **Pencarian dan salin:** pencarian tabel, pemilihan baris, salin baris terpilih, dan salin seluruh tabel dalam format TSV.
- **Ekspor:** tabel dapat diunduh sebagai CSV atau Excel (.xlsx).
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

## Demo sementara melalui Ngrok

Selama server perusahaan belum tersedia, DocuFlow dapat dibuka sementara dari laptop developer:

```powershell
docker compose up --build -d
ngrok http 8080
```

Bagikan URL HTTPS yang diberikan Ngrok hanya kepada pengguna yang diizinkan. Laptop developer, Docker, dan tunnel Ngrok harus tetap aktif selama aplikasi digunakan. Hentikan tunnel dengan `Ctrl+C`, lalu jalankan `docker compose down` bila ingin menghentikan DocuFlow. Ngrok hanya untuk demo sementara, bukan production. DocuFlow tidak memiliki login aplikasi; URL tunnel dapat memberi akses ke dokumen yang diunggah, jadi batasi akses melalui kontrol Ngrok yang tersedia dan jangan membagikan URL secara publik.

## Deployment internal production

DocuFlow tidak memiliki login aplikasi atau database pengguna. Jalur deployment utama adalah server internal perusahaan; pembatasan akses jaringan, TLS, reverse proxy, dan autentikasi tingkat perusahaan ditetapkan oleh tim IT.

Project menyediakan Docker Compose untuk frontend Nginx dan backend FastAPI. Panduan kedua skenario, environment, storage, model OCR, jaringan, operasi, serta troubleshooting ada di [DEPLOYMENT.md](DEPLOYMENT.md). Production menggunakan Docker Compose pada server perusahaan; Nginx/reverse proxy dan HTTPS dikelola bersama tim IT, yang juga membatasi akses lewat jaringan atau gateway perusahaan.

OCR lokal menggunakan RapidOCR dan ONNX Runtime; fallback struktur tabel RapidTable yang sudah digunakan di environment proyek juga disertakan dalam image. Model diunduh ketika pertama kali dimuat jika belum tersedia. Docker Compose menaruh model OCR, model RapidTable, dan cache OCR pada named volume; cache mengandung teks hasil OCR dan mengikuti retensi aplikasi (maksimum 256 MB dan 7 hari). Temporary raster diproses di filesystem sementara container dan dibersihkan oleh pipeline.

Extraction queue, progress, dan cache runtime masih dikelola dalam satu proses backend. Jalankan satu worker dan satu instance sampai penyimpanan progress bersama dirancang serta diuji. Kebutuhan CPU/RAM dan waktu scan bergantung pada server dan dokumen; ukur pada infrastruktur perusahaan sebelum menetapkan kapasitas/SLA. Jangan expose aplikasi langsung ke internet tanpa kontrol akses dari infrastruktur perusahaan.

## Pemeriksaan perubahan

Jalankan tes Python hanya pada direktori tes aplikasi. Jangan memakai discovery pytest dari root: folder eksperimen OCR berisi skrip bernama `test_*.py` untuk penggunaan manual.

```powershell
python -m pytest tests -q
node --test tests/frontend_table_interactions.test.mjs
npm run build
```

## Dependency utama

- Frontend: `package.json` dan `package-lock.json`.
- Backend: `backend/requirements.txt`.
- `requirements-ocr.txt` menyediakan subset dependency OCR.

Jangan jalankan skrip di `tools/` sebagai bagian dari tes otomatis. Beberapa eksperimen dapat menjalankan inferensi dan menulis artefak ke `ocr-pilot-output/`.
