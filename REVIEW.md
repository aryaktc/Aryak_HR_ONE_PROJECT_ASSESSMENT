# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | MongoClient setup | Client was not `tz_aware` and had no short server selection timeout. | BSON dates came back naive and `/health` could hang. | Use `MongoClient(..., tz_aware=True, serverSelectionTimeoutMS=3000)`. |
| 2 | `/health` | It returned ok without pinging MongoDB. | Stop MongoDB and `/health` still returned 200. | Run `db.command("ping")`; return 503 on failure. |
| 3 | `compute_late_minutes` | It used naive/local datetimes instead of IST attendance dates. | UTC punch times were compared against the wrong local clock. | Replaced with aware UTC/IST helpers. |
| 4 | `compute_late_minutes` | The strict 10-minute boundary was wrong: 09:40:01 returned 0. | Unit tests for 09:40:00, 09:40:01, 10:15:59. | Implemented R2 with seconds diff > 600 and floor minutes from shift start. |
| 5 | Attendance date | Overnight shifts were not handled. | A 05:59:59 IST punch-in for 22:00-06:00 got the wrong date. | Added `attendance_date()` with overnight rollover. |
| 6 | `compute_work_hours` | Python `round()` uses banker's/float rounding. | Half-up cases such as 2.675 round incorrectly. | Use `Decimal` and `ROUND_HALF_UP`. |
| 7 | `compute_overtime` | No 30-minute minimum and no overnight shift-end handling. | 18:59 returned overtime and overnight shifts used the wrong end date. | Added R3 helper with overnight end and minimum threshold. |
| 8 | `/employees` create | It checked then inserted, so duplicate creates could race. | Parallel duplicate creates can produce two successful inserts without a unique index. | Add unique index and catch `DuplicateKeyError` as 409. |
| 9 | `/employees` create | Validation was missing or loose. | Bad emp codes, emails, equal shifts, and impossible dates were accepted. | Added Pydantic constraints and date/shift validators. |
| 10 | `/employees` create | `created_at` used naive local time and was returned as a datetime. | Response did not match epoch-ms contract. | Store aware UTC and serialize to epoch ms. |
| 11 | `/employees` list | `skip = page * page_size`, so page 1 skipped the first page. | `GET /employees?page=1` returned the second page. | Use `(page - 1) * page_size`. |
| 12 | `/employees` list | `total` ignored the department filter, and results were not sorted. | Filtered request returned total for all employees. | Count filtered query and sort by `emp_code`. |
| 13 | `/attendance/punch-in` | Unknown employee could cause a 500. | Punch in for a nonexistent `emp_code`. | Return 404 before using shift fields. |
| 14 | `/attendance/punch-in` | Status was free form. | `{"status":"HELLO"}` was accepted. | Use a presence-status enum. |
| 15 | `/attendance/punch-in` | Timestamp parsing accepted seconds-looking, strings, floats, and booleans. | Bad JSON values did not return 422. | Use `Annotated[StrictInt, Field(...)]` for every instant. |
| 16 | `/attendance/punch-in` | It checked then inserted and had no unique date index. | Parallel punch-ins could both pass the pre-check. | Insert directly under `(emp_code, date)` unique index and map duplicate to 409. |
| 17 | Serializers | `_id` leaked as `id`; datetimes returned as ISO/Python datetimes. | Responses contained internal IDs and non-contract timestamp shapes. | Centralized serializers drop `_id` and convert BSON dates to epoch ms, including history. |
| 18 | `/attendance` list | It loaded all matching docs into memory and sorted in Python. | Large collections were slow and explain could not show the intended index plan. | Use MongoDB find/sort/skip/limit and filtered count. |
| 19 | `/attendance` list | It did not validate page bounds or `date_from > date_to`. | Bad dates or reversed ranges did not return 422. | Add query validation and shared builder. |
| 20 | Indexes | No assignment indexes were created. | Explain plans could use collection scans and races were not guarded. | Create the required indexes at startup, idempotently. |

Checked and found fine: `load_dotenv()` did not override real environment variables, which matches the assignment. ISO `YYYY-MM-DD` string comparison is also correct for date ranges because the format sorts lexicographically in calendar order.
