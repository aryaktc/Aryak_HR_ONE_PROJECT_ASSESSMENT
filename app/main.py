"""
Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Any

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("attendance_api")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MAX_EPOCH_MS = 4102444800000
EpochMs = Annotated[StrictInt, Field(ge=10**11, le=MAX_EPOCH_MS)]

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Validation models
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMs | None = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMs | None = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Status | None = None
    punch_in: EpochMs | None = None
    punch_out: EpochMs | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Rule helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Return an aware UTC datetime truncated to whole seconds for storage."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_date(value: str) -> date:
    """Validate and parse an API calendar date."""
    if not DATE_RE.match(value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def validate_date(value: str, name: str = "date") -> None:
    """Raise 422 when a query or path date is not a real YYYY-MM-DD date."""
    try:
        parse_date(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be YYYY-MM-DD") from None


def validate_month(value: str) -> None:
    """Raise 422 when a month does not match YYYY-MM."""
    if not MONTH_RE.match(value):
        raise HTTPException(422, "month must be YYYY-MM")


def parse_shift(value: str) -> time:
    """Parse an HH:MM shift time in IST."""
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def shift_seconds(value: str) -> int:
    """Convert an HH:MM shift time to seconds since midnight."""
    parsed = parse_shift(value)
    return parsed.hour * 3600 + parsed.minute * 60


def ms_to_dt(value: int) -> datetime:
    """Convert epoch milliseconds to aware UTC datetime, truncating to seconds (R1)."""
    return datetime.fromtimestamp(value // 1000, UTC)


def dt_to_ms(value: datetime | None) -> int | None:
    """Convert a BSON datetime to epoch milliseconds for API responses."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def round_half_up(value: float | Decimal, places: int) -> float:
    """Round reported numbers with Decimal half-up semantics (R8)."""
    quant = Decimal("1").scaleb(-places)
    decimal_value = Decimal(str(round(float(value), 9)))
    return float(decimal_value.quantize(quant, rounding=ROUND_HALF_UP))


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Compute rounded work hours from whole seconds (R4)."""
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: float | None) -> bool:
    """A rounded work-hours value below 4.50 is a half day (R5)."""
    return work_hours is not None and Decimal(str(work_hours)) < Decimal("4.50")


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Find the IST attendance date, including overnight shift rollover (R1)."""
    local = punch_in.astimezone(IST)
    result = local.date()
    if shift_seconds(shift_end) <= shift_seconds(shift_start):
        local_seconds = local.hour * 3600 + local.minute * 60 + local.second
        if local_seconds < shift_seconds(shift_end):
            result -= timedelta(days=1)
    return result.isoformat()


def shift_start_dt(att_date: str, shift_start: str) -> datetime:
    """Build the aware UTC shift-start instant for an attendance date (R2)."""
    base = datetime.combine(parse_date(att_date), parse_shift(shift_start), IST)
    return base.astimezone(UTC)


def shift_end_dt(att_date: str, shift_start: str, shift_end: str) -> datetime:
    """Build the aware UTC shift-end instant, rolling overnight shifts forward (R3)."""
    end_date = parse_date(att_date)
    if shift_seconds(shift_end) <= shift_seconds(shift_start):
        end_date += timedelta(days=1)
    base = datetime.combine(end_date, parse_shift(shift_end), IST)
    return base.astimezone(UTC)


def calc_late(punch_in: datetime, att_date: str, shift_start: str) -> int:
    """Late starts only after a strict 10-minute grace period (R2)."""
    diff = int((punch_in - shift_start_dt(att_date, shift_start)).total_seconds())
    return diff // 60 if diff > 600 else 0


def calc_overtime(punch_out: datetime, att_date: str, shift_start: str, shift_end: str) -> int:
    """Count overtime only when at least 30 whole minutes beyond shift end (R3)."""
    minutes = int((punch_out - shift_end_dt(att_date, shift_start, shift_end)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def working_days_in_month(month: str, joined_on: str) -> int:
    """Count Mon-Fri working days from max(month start, joined_on) through month end (R7)."""
    year, month_num = map(int, month.split("-"))
    start = max(date(year, month_num, 1), parse_date(joined_on))
    end = date(year, month_num, calendar.monthrange(year, month_num)[1])
    if start > end:
        return 0
    return sum(1 for offset in range((end - start).days + 1) if (start + timedelta(days=offset)).weekday() < 5)


def month_bounds(month: str) -> tuple[str, str]:
    """Return first and last date strings for a YYYY-MM month."""
    validate_month(month)
    year, month_num = map(int, month.split("-"))
    last = calendar.monthrange(year, month_num)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def presence_weight(doc: dict[str, Any]) -> float:
    """Return present-day weight for analytics (R5-R7)."""
    if doc.get("status") not in PRESENCE_STATUSES:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


# --------------------------------------------------------------------------- #
# Serialization and database setup
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Create all assignment indexes idempotently without blocking startup."""
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    except PyMongoError:
        logger.exception("index creation failed")


@app.on_event("startup")
def on_startup() -> None:
    ensure_indexes()


def employee_out(doc: dict[str, Any]) -> dict[str, Any]:
    """Serialize an employee without MongoDB internals."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def api_value(value: Any) -> Any:
    """Convert nested BSON datetimes to API epoch milliseconds."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value


def record_out(doc: dict[str, Any]) -> dict[str, Any]:
    """Serialize an attendance record with defaults for legacy fields."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": api_value(doc.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# Query builders reused by endpoints and /admin/explain
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Status | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[str, dict[str, Any]]:
    """Build the indexed attendance-list find specification (R10)."""
    if date_from:
        validate_date(date_from, "date_from")
    if date_to:
        validate_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status.value
    return "attendance_logs", {
        "filter": query,
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def build_employee_monthly_pipeline(emp_code: str, month: str) -> tuple[str, list[dict[str, Any]]]:
    """Build the monthly employee aggregation using stored derived values (R6-R8)."""
    start, end = month_bounds(month)
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Build department summary from employees so zero-log staff count (R9)."""
    start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department
    return "employees", [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1900-01-06"]}}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "wh_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
                "wh_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$project": {"department": "$_id", "headcount": {"$size": "$employees"}, "present_days": 1, "wh_sum": 1, "wh_count": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1, "_id": 0}},
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Build leaderboard with standard competition ranking (R8)."""
    start, end = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        ]
    )
    return "attendance_logs", pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> tuple[str, list[dict[str, Any]]]:
    """Build the indexed log aggregation used by department trend."""
    validate_date(from_date, "from")
    validate_date(to_date, "to")
    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    if (parse_date(to_date) - parse_date(from_date)).days > 92:
        raise HTTPException(422, "range must be at most 92 days")
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    if not employees:
        raise HTTPException(404, "department not found")
    codes = [emp["emp_code"] for emp in employees]
    return "attendance_logs", [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$project": {"date_obj": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(parse_date(from_date), time.min, UTC),
                        datetime.combine(parse_date(to_date), time.min, UTC),
                    ],
                },
            }
        },
        {
            "$set": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date_obj": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not ready") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    punch = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(punch, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": calc_late(punch, att_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "punch-in not found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_dt = record["punch_in"]
    if punched_at <= punch_in_dt or punched_at - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = calc_work_hours(punch_in_dt, punched_at)
    update = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": calc_overtime(punched_at, record["date"], emp["shift_start"], emp["shift_end"]),
        "half_day": is_half_day(work_hours),
    }
    result = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    record.update(update)
    return record_out(record)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Status | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    _, spec = build_attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    docs = db.attendance_logs.find(spec["filter"]).sort(spec["sort"]).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [record_out(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    date_str = date
    validate_date(date_str)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status.value if body.status else record["status"]
    punch_in_dt = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
    punch_out_dt = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        punch_in_dt = None
        punch_out_dt = None
    elif punch_in_dt is None:
        raise HTTPException(422, "presence status requires punch_in")

    if punch_in_dt is not None and attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"]) != date_str:
        raise HTTPException(422, "punch_in must stay on the record attendance date")
    if punch_out_dt is not None:
        if punch_in_dt is None or punch_out_dt <= punch_in_dt or punch_out_dt - punch_in_dt > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status in {"ABSENT", "LEAVE"}:
        computed = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = calc_late(punch_in_dt, date_str, emp["shift_start"])
        if punch_out_dt is None:
            computed = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        else:
            work_hours = calc_work_hours(punch_in_dt, punch_out_dt)
            computed = {
                "work_hours": work_hours,
                "late_minutes": late,
                "overtime_minutes": calc_overtime(punch_out_dt, date_str, emp["shift_start"], emp["shift_end"]),
                "half_day": is_half_day(work_hours),
            }

    new_values = {"status": new_status, "punch_in": punch_in_dt, "punch_out": punch_out_dt, **computed}
    defaults = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old = record.get(field, defaults.get(field))
        if old != new_values[field]:
            changes[field] = {"from": old, "to": new_values[field]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history = record.get("history", [])
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history)]}},
        {"$set": new_values, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "attendance record was changed concurrently")
    record.update(new_values)
    record["history"] = history + [entry]
    return record_out(record)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)) -> dict[str, Any]:
    validate_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    working_days = working_days_in_month(month, emp["joined_on"])
    _, pipeline = build_employee_monthly_pipeline(emp_code, month)
    row = next(db.attendance_logs.aggregate(pipeline), None) or {}
    present = float(row.get("present_days", 0))
    pct = None if working_days == 0 else round_half_up(present / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: str | None = None) -> dict[str, Any]:
    _, pipeline = build_department_summary_pipeline(month, department)
    items = []
    for row in db.employees.aggregate(pipeline):
        avg = None if row["wh_count"] == 0 else round_half_up(row["wh_sum"] / row["wh_count"], 2)
        items.append(
            {
                "department": row["department"],
                "headcount": row["headcount"],
                "present_days": round_half_up(row.get("present_days", 0), 2),
                "avg_work_hours": avg,
                "late_count": row.get("late_count", 0),
                "total_late_minutes": row.get("total_late_minutes", 0),
                "leave_count": row.get("leave_count", 0),
                "on_duty_count": row.get("on_duty_count", 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
) -> dict[str, Any]:
    _, pipeline = build_late_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")) -> dict[str, Any]:
    _, pipeline = build_department_trend_pipeline(department, from_date, to_date)
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    by_date: dict[str, dict[str, Any]] = {}
    for doc in db.attendance_logs.aggregate(pipeline):
        by_date[doc["_id"]] = {"present_count": float(doc.get("present_count", 0)), "late_count": doc.get("late_count", 0)}

    start = parse_date(from_date)
    end = parse_date(to_date)
    rows = []
    rates: list[float | None] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        day_str = day.isoformat()
        headcount = sum(1 for emp in employees if emp["joined_on"] <= day_str)
        working = day.weekday() < 5
        bucket = by_date.get(day_str, {"present_count": 0.0, "late_count": 0})
        raw_rate = bucket["present_count"] / headcount if working and headcount > 0 else None
        rates.append(raw_rate)
        window = [rate for rate in rates[-7:] if rate is not None]
        rows.append(
            {
                "date": day_str,
                "is_working_day": working,
                "headcount": headcount,
                "present_count": round_half_up(bucket["present_count"], 2),
                "late_count": bucket["late_count"],
                "attendance_rate": None if raw_rate is None else round_half_up(raw_rate, 4),
                "moving_avg_7d": None if not window else round_half_up(sum(window) / len(window), 4),
            }
        )
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Status | None = None,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        collection, spec = build_attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        command = {"explain": {"find": collection, "filter": spec["filter"], "sort": dict(spec["sort"]), "skip": spec["skip"], "limit": spec["limit"]}, "verbosity": "executionStats"}
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        collection, pipeline = build_employee_monthly_pipeline(emp_code, month)
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        collection, pipeline = build_department_summary_pipeline(month, department)
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        collection, pipeline = build_late_leaderboard_pipeline(month, limit, department)
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        collection, pipeline = build_department_trend_pipeline(department, from_date, to_date)
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}
    else:
        raise HTTPException(422, "unknown endpoint")

    explain = db.command(command)
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explain))}
