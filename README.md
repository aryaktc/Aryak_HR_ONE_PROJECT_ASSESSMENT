# Employee Attendance API

FastAPI service for employee records, attendance punches, regularization, and MongoDB-backed analytics.

## Run locally

Start MongoDB with Docker Compose, then install and run the API:

```bash
docker compose up -d mongodb
python -m venv .venv
# Activate the environment, then:
pip install -r requirements.txt
```

Set `MONGO_URI=mongodb://localhost:27018` and `MONGO_DB=attendance_db` in your shell before starting the app. The Compose file maps MongoDB to port `27018` by default to avoid conflicts with an existing local MongoDB; change `MONGO_PORT` in the environment to use another host port. The API also reads `MONGO_URI` and `MONGO_DB` from a local `.env` file, which is ignored by Git.

```bash
uvicorn app.main:app --reload
```

Check `http://localhost:8000/health`; it returns `{"status":"ok"}` when MongoDB is reachable. Interactive API docs are at `/docs`.

Stop MongoDB with `docker compose down`. The named volume keeps local database data between runs.
