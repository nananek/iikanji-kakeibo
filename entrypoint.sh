#!/bin/sh
set -e

echo "Running database migrations..."
flask db upgrade

echo "Seeding account types..."
flask seed

if [ "${FLASK_DEBUG}" = "1" ]; then
  echo "Starting Flask development server..."
  exec flask run --host=0.0.0.0
else
  echo "Starting Gunicorn..."
  exec gunicorn "app:create_app()" -c gunicorn.conf.py
fi
