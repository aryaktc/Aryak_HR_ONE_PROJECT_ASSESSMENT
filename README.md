# Employee Attendance API

FastAPI service for employee records, attendance punches, regularization, and MongoDB-backed attendance analytics.

## Run locally

```bash
python -m venv .venv
# Activate the environment, then:
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Set `MONGO_URI` and `MONGO_DB` in the environment or a local `.env` file. The app defaults to `mongodb://localhost:27017` and `attendance_db`. API docs are available at `/docs`.
