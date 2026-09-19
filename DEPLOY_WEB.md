# Run CBMP as a web application

The dashboard **is** a web app (Streamlit). Users open it in Chrome or Edge.

## Option A — Your Windows PC (fastest pilot)

1. Install **Python 3.10+** from https://www.python.org/downloads/  
   (check “Add Python to PATH”)
2. Unzip `cbmp_dashboard_FINAL.zip`
3. Double-click **`run_web.bat`**
4. Open browser: **http://localhost:8501**
5. Sign in (admin / Users & Access accounts)

Others on the same Wi‑Fi/LAN can use: `http://YOUR-PC-IP:8501`  
(Find IP: `ipconfig` → IPv4 address)

## Option B — Linux server (county / office)

```bash
unzip cbmp_dashboard_FINAL.zip
cd cbmp_dashboard
chmod +x run_web.sh
./run_web.sh
```

Put **nginx** or Apache in front for HTTPS and a proper domain.

## Option C — Docker

```bash
docker build -t cbmp-dashboard .
docker run -p 8501:8501 -e CBMP_BOOTSTRAP_PASSWORD='StrongPass!' cbmp-dashboard
```

Open http://localhost:8501

## First login

- Set env var `CBMP_BOOTSTRAP_PASSWORD` before first seed, **or** use the generated password process from Users & Access.
- Force password change on first login is enabled.
- Create named users under **Users & Access**.

## Production checklist

- [ ] HTTPS (reverse proxy)
- [ ] Backup `cbmp.db` daily
- [ ] Named users only (no shared passwords)
- [ ] Africa’s Talking keys only if using SMS
- [ ] Firewall: only allow needed ports

## “Windows app” without rewriting

Create a desktop shortcut to `http://localhost:8501` or your public URL.  
That is the standard approach for Streamlit systems.
