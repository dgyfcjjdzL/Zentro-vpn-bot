# Zentro VPN Telegram Bot (starter)

This project provides a webhook-based Telegram bot with:
- Mandatory membership check for `@Zentro_VPNx`
- Product list and stock display
- Orders stored in SQLite
- Card-transfer receipt photo submission
- Admin approval/rejection buttons
- Config inventory with one-time assignment
- Customer order history
- Admin commands for products, stock, reports, recent orders, and broadcast
- Webhook secret validation

## Critical security steps first
1. The bot token was shared in a chat. Revoke it using the official `@BotFather` `/revoke` command and use a fresh token.
2. Do not share the new token, `.env`, card details, or database file publicly.
3. Set `ADMIN_ID` to your numeric Telegram user ID, not your username.
4. Add the bot as an administrator in `@Zentro_VPNx` so Telegram can verify membership.
5. Verify each transfer in your bank app before approving a receipt. A screenshot alone is not proof of payment.

## Setup
1. Copy `.env.example` to `.env`.
2. Fill `BOT_TOKEN`, `WEBHOOK_URL`, `WEBHOOK_SECRET`, `ADMIN_ID`, `PAYMENT_CARD`, `PAYMENT_NAME`, and `DB_PATH`.
3. Install dependencies: `pip install -r requirements.txt`
4. This app is intended to run behind a WSGI web server that imports `app` from `app.py`.
5. Test with a small dummy product and a non-sensitive test config before selling anything.

## Admin commands
- `/myid` — show your numeric Telegram ID (works before admin is configured)
- `/admin` — help for admin commands
- `/stats` — summary statistics
- `/products` — product list
- `/addproduct Name|PriceToman|Duration|Volume|Description`
- `/addconfig PRODUCT_ID|CONFIG_TEXT`
- `/offproduct PRODUCT_ID`
- `/orders` — recent orders
- `/broadcast Message`

Example:
`/addproduct Basic 30d|120000|30 روز|50 گیگ|مناسب یک دستگاه`
`/addconfig 1|PASTE_A_TEST_CONFIG_HERE`

Use actual values only after testing. Do not put `|` inside a config if you are using the `/addconfig` command.

## PythonAnywhere deployment notes (free tier)
Free-tier features and outbound network rules can change. This project uses a webhook (rather than a permanent polling process), which is better suited to a web-app host.
1. Create a PythonAnywhere account and a new **Flask** web app.
2. Upload `app.py`, `requirements.txt`, and `.env` into your home project folder. Do not upload `.env` to a public GitHub repository.
3. In a Bash console, install dependencies in the virtual environment configured for the web app:
   `pip install -r /home/YOUR_USERNAME/path/to/requirements.txt`
4. Open the web app's WSGI configuration file and make it import the Flask app. Example (adjust paths):
   `import sys`
   `path = '/home/YOUR_USERNAME/zentro_vpn_bot'`
   `if path not in sys.path: sys.path.insert(0, path)`
   `from app import app as application`
5. Ensure `.env` is in the same folder as `app.py`. Set `DB_PATH` to a persistent path in your home directory, e.g. `/home/YOUR_USERNAME/zentro.db`.
6. Set `WEBHOOK_URL=https://YOUR_USERNAME.pythonanywhere.com` and a long random `WEBHOOK_SECRET` in `.env`.
7. Reload the web app from the PythonAnywhere dashboard. Visit `https://YOUR_USERNAME.pythonanywhere.com/`; it should show `Zentro VPN bot is running.`
8. Send `/myid` to your bot in Telegram. Put the numeric ID into `ADMIN_ID` in `.env`, then reload the web app.
9. In Telegram, make the bot an admin in `@Zentro_VPNx`. Test the membership button with a second account if possible.
10. Test product creation, stock, receipt forwarding, approval/rejection, and config delivery before taking real payments.

If PythonAnywhere blocks Telegram API requests from your account/region or webhook setup fails, check the app error log and platform's current outbound access rules. Never rely on an untested bot for real payments.

## Important limitations
- Receipt images are not verified against bank transactions automatically.
- The owner must verify the actual transfer before pressing approve.
- No payment gateway integration is included.
- This is a starter implementation, not a guarantee of commercial-grade reliability. Back up the database and test thoroughly.
- A refund flow is handled manually by the owner; the bot does not initiate bank refunds.
- The bot only sends configs that you have manually added to the inventory.
