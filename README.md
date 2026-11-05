# PriceLens

Product & price-intelligence platform: crawlers collect products and prices from
e-commerce sites, PostgreSQL stores the catalogue and its price history, and a
Django REST API serves it.

Work in progress.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements/dev.txt
cp .env.example .env        # point DATABASE_URL at a PostgreSQL database
python manage.py migrate
python manage.py runserver
```
