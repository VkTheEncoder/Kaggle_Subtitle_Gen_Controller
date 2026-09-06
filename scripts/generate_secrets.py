import secrets
print("FLASK_SECRET_KEY=" + secrets.token_urlsafe(48))
print("CONTROL_SECRET=" + secrets.token_urlsafe(48))
print("DASHBOARD_PASSWORD=" + secrets.token_urlsafe(18))
