# CapitalPay TZS Dash Checkout

Dash/Flask checkout app for CapitalPay, configured for account ID `46` and TZS.
It includes:

- Checkout iframe at `/dash/`
- CapitalPay notification listener at `/notify` and `/payment/notify`
- Live notification/IP monitor in the Dash app

## Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
gunicorn testTZS:server
```

Environment variables:

```bash
CAPITALPAY_API_KEY=your_api_key
CAPITALPAY_API_SECRET=your_api_secret
CAPITALPAY_NOTIFICATION_URL=https://your-app.onrender.com/notify
CAPITALPAY_NOTIFICATION_DB=capitalpay_notifications.sqlite3
```

If `CAPITALPAY_NOTIFICATION_URL` is not set, the checkout form fills it from the current request host plus `/notify`.
