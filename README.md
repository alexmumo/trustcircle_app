# Trust-Circle local app

## Structure
- `app.py` - Flask backend
- `instance/trustcircle.db` - SQLite database
- `schema.sql` - database schema
- `templates/index.html` - web UI
- `static/` - CSS and JavaScript

## Run on Windows
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py app.py
```
Then open `http://127.0.0.1:5000` on the same computer.

The database starts empty and contains users, wallets, loans, contributions, transactions, and repayments.
