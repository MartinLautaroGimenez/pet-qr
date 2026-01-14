# Pet QR (Dog Tag QR + Alerts + Admin Dashboard)

## Endpoints
- Public profile: `/p/rocky`
- Admin: `/admin` (login en `/admin/login`)

## Run local
```bash
cp .env.example .env
docker build -t pet-qr .
docker run --rm -p 8000:8000 --env-file .env -v petqr_data:/data pet-qr
