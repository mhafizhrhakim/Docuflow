# DocuFlow Deployment Guide

This guide describes two supported operating scenarios: a temporary developer-laptop demo through Ngrok, and the official internal deployment using Docker Compose. DocuFlow has no application login, user database, or account/session management. The company IT team owns production network access, TLS, reverse-proxy policy, and any company identity integration.

## Temporary demo through Ngrok

Use this only while the company server is unavailable. Start Docker Compose on the developer laptop, then expose its local Nginx port through Ngrok:

```powershell
docker compose up --build -d
ngrok http 8080
```

Open the HTTPS URL printed by Ngrok and share it only with the intended, authorized tester. The developer laptop, Docker containers, internet connection, and Ngrok process must remain active while the tester uses DocuFlow. Stop Ngrok with `Ctrl+C`; stop the Docker stack with `docker compose down` when the demo is over.

Ngrok is a temporary access path, not a production deployment. The app itself has no login, so anyone who can reach the tunnel may be able to use the upload and extraction endpoints. Do not post the URL publicly; apply Ngrok access restrictions available to your account before sharing, and use only documents approved for this demo. The Docker Compose port binds to `127.0.0.1:8080` by default, which is reachable by the local Ngrok agent without a permanent hostname in Vite configuration.

## Official internal production

Run Docker Compose on the company-managed server. Nginx serves the production frontend and proxies `/api` to FastAPI. The company IT team manages the fronting reverse proxy, HTTPS/TLS, DNS, firewall, and access restrictions through company networks or gateway. Do not use Ngrok as the production ingress.

## Architecture

- Nginx serves the built Vite frontend on port `8080` and forwards `/api/*` to FastAPI.
- FastAPI runs one Uvicorn worker on the private Compose network. Keep one instance because job progress and OCR coordination are process-local.
- RapidOCR uses the existing local ONNX Runtime pipeline. Its PP-OCR models live in the `rapidocr-models` Docker volume. The image installs the already-used optional RapidTable structure fallback at the same pinned version found in the current project environment, using `--no-deps` to retain headless OpenCV and avoid the conflicting `opencv-python` wheel. RapidTable's model files live in the separate `rapidtable-models` volume.
- OCR result cache lives in the `docuflow-ocr-cache` Docker volume.
- Temporary upload and raster files use container temporary storage. OCR raster files are deleted by the processing code; the backend container's `/tmp` is not a persistent volume.
- The Compose stack requires no external database.

The frontend and API share one origin. Browser calls use `/api`; Nginx preserves that path when proxying to FastAPI. TLS and any company access control should be applied by the internal reverse proxy or gateway in front of this stack.

## Requirements

- Docker Engine with the Docker Compose plugin.
- Outbound HTTPS access during image builds to install npm/Python packages.
- Outbound access to RapidOCR's model host on first startup, unless IT pre-populates the model volume from an approved model bundle.
- The RapidTable fallback model is also initialized on first use when required; allow its configured model host or provide the approved model files in the persistent model volume.
- Sufficient CPU, RAM, and disk for the intended number and size of scanned PDFs. No universal processing-time guarantee is implied.

## Configure

Copy `.env.example` to `.env`, then set the deployment-specific values:

```dotenv
DOCUFLOW_FRONTEND_ORIGIN=https://docuflow.internal.example
DOCUFLOW_HTTP_PORT=8080
DOCUFLOW_BIND_ADDRESS=127.0.0.1
MAX_UPLOAD_MB=50
INSPECTION_WORKERS=2
INSPECTION_QUEUE_SLOTS=2
OCR_INTRA_OP_THREADS=2
```

`DOCUFLOW_FRONTEND_ORIGIN` must be the exact HTTPS origin users will open, without a path. HTTP is accepted only for localhost loopback Compose testing. `DOCUFLOW_BIND_ADDRESS=127.0.0.1` keeps the web port local to the server so a reverse proxy on that host can reach it. If the IT-managed ingress runs on another host/container, IT must attach it to the Compose network or deliberately bind the port to an approved interface and firewall it.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DOCUFLOW_FRONTEND_ORIGIN` | Yes for Compose | Origin allowlisted by FastAPI CORS. Use the exact production HTTPS origin. |
| `DOCUFLOW_HTTP_PORT` | No | Host port for the Nginx frontend; default `8080`. |
| `DOCUFLOW_BIND_ADDRESS` | No | Host interface for the published port; defaults to loopback. |
| `MAX_UPLOAD_MB` | No | Backend upload limit; default `50`. Nginx allows 55 MB to leave room for multipart overhead. Keep the Nginx limit above the backend limit if this value is increased. |
| `UPLOAD_TTL_MINUTES` | No | In-memory progress retention; default `30`. |
| `INSPECTION_WORKERS` | No | Concurrent extraction jobs in one backend process; default `2`. |
| `INSPECTION_QUEUE_SLOTS` | No | Additional queued jobs; default `2`. Excess requests receive HTTP 503. |
| `OCR_INTRA_OP_THREADS` | No | ONNX CPU threads per inference; default `2`. |

There are no `DOCUFLOW_ACCESS_PASSWORD`, `DOCUFLOW_AUTH_SECRET`, or application-session variables. Do not add application login secrets. If company policy requires authentication, configure it in the infrastructure layer owned by IT.

## Start and operate

From the repository root:

```sh
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs -f backend frontend
```

Open `http://localhost:8080` when running locally. In the internal environment, open the company URL provided by IT. Verify the backend health response:

```sh
curl -fsS http://localhost:8080/api/health
```

The HTTP health check indicates that FastAPI responds. For scan testing, also confirm the response's `ocr.engine_state` becomes `ready`; the first startup may download and initialize model files. Keep one backend worker and one replica. Multiple backend processes would not share progress IDs or in-process coordination.

To stop the services without deleting persistent data:

```sh
docker compose down
```

Do not use `docker compose down -v` as routine cleanup: it deletes both the OCR result cache and model volumes. Store and back up the volumes only under company data-retention policy.

## Storage, privacy, and retention

- `rapidocr-models` and `rapidtable-models` contain OCR/table model weights and avoid downloading them after container recreation.
- `docuflow-ocr-cache` contains recognized text, confidence, and word geometry. Application cleanup bounds it to 256 MB and seven days, but the Docker volume itself persists until its owner removes it. Treat it as sensitive document-derived data and restrict volume/host access.
- Uploaded PDFs are read by the API and are not stored as permanent application documents. The endpoint enforces `MAX_UPLOAD_MB` (default 50 MB), closes the upload, and runs within a bounded queue.
- Rendered OCR PNGs and optional table-fallback PNGs use `/tmp` and are removed in normal and error paths. Container temporary storage is ephemeral across container replacement.
- Progress is kept in backend memory and contains progress counters, not document tables. A restart loses active progress.
- No PDF samples, local OCR cache, virtual environments, credentials, or benchmark output are included in the Docker build context; `.dockerignore` excludes them.

## Network and production security

- The backend port is exposed only inside Compose; the host publishes the Nginx port, default-bound to `127.0.0.1`.
- Terminate TLS at the company reverse proxy/gateway. Restrict ingress to approved company networks and configure the company's access-control policy there.
- FastAPI keeps explicit CORS origins and rejects wildcard or non-HTTPS production origins. Same-origin Nginx proxying does not need browser CORS, but the explicit origin remains required as a production configuration check and supports approved split-origin setups.
- Production mode disables FastAPI's interactive API documentation. For local development, `APP_ENV=development` keeps `/docs` available.
- Nginx allows a 55 MB request body, and the API defaults to a 50 MB PDF limit. Nginx's upload processing timeout is one hour to accommodate long scan jobs; this is a maximum proxy wait, not a promised extraction time.
- The Compose configuration does not add an application login or protect a publicly reachable server. Do not bind the service to a public interface unless the company gateway/firewall provides the required access control.

## Verification and smoke tests

Run automated checks from the repository root:

```sh
python -m pytest tests -q
node --test tests/frontend_table_interactions.test.mjs
npm run build
docker compose config
docker compose build
```

After startup, use approved non-sensitive PDFs for runtime smoke checks. The endpoint response should include page classification and extraction results:

```sh
curl -f -F "file=@/path/to/digital.pdf" "http://localhost:8080/api/documents/inspect?ocr=true"
curl -f -F "file=@/path/to/scanned.pdf" "http://localhost:8080/api/documents/inspect?ocr=true"
```

In the UI, verify a digital PDF, a scanned PDF, progress polling, page/table synchronization, review indicators, copy, and CSV export against the source document. A successful HTTP response or non-empty table is not proof of extraction accuracy. Record source-level checks on representative company documents before inviting auditors.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Backend exits during startup | Check `FRONTEND_ORIGIN`/`DOCUFLOW_FRONTEND_ORIGIN` spelling and scheme. Production requires an explicit origin; use HTTPS except for localhost loopback Compose testing. |
| `ocr.engine_state` is not `ready` | Review `docker compose logs backend`, outbound model-host access, available disk space, and model-volume write permissions. |
| HTTP 413 on upload | Check PDF size against `MAX_UPLOAD_MB`; if raising it, also raise `client_max_body_size` in `nginx.conf`. |
| HTTP 503 during concurrent work | The configured active and queued extraction slots are full. Wait for a job to finish or tune concurrency after measuring server CPU/RAM. |
| Frontend loads but API is unavailable | Check `docker compose ps`, backend health, and that the request reaches `/api/` through Nginx. |
| Container restarts or disk grows | Check host disk/memory, persistent cache/model volumes, and container logs. Do not remove volumes until retention and recovery requirements are approved by IT. |

## Handover checklist for IT

- Choose the server, DNS name, TLS termination, firewall scope, and company access-control mechanism.
- Review CPU/RAM/disk capacity using expected concurrent scanned PDFs.
- Provide outbound model download during first start or an approved offline model bundle.
- Decide retention, backup, access, and deletion policy for OCR cache and model volumes.
- Set the exact production frontend origin and approved host binding.
- Validate health, digital/scan uploads, progress, review flags, and CSV export through the actual company ingress.
