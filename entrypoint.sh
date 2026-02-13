#!/bin/bash
set -e

echo "Running database migrations..."
flask db upgrade

echo "Seeding account types..."
flask seed

echo "Starting Flask..."
exec flask run --host=0.0.0.0
