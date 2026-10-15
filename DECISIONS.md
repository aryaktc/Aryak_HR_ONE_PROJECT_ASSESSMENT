# DECISIONS.md

1. **Indexes.** I create a unique `employees.emp_code` index for identity, `(department, joined_on)` and `joined_on` for headcount filters, unique `(emp_code, date)` for one attendance row per day, and `(date desc, emp_code asc)` for list and analytics date ranges. I considered a separate `late_minutes` index, but rejected it because leaderboard is still month-bounded by date and the required indexes stay simpler.

2. **Punch-in race.** Both requests compute the same attendance date and call `insert_one`. MongoDB accepts one document under the unique `(emp_code, date)` index. The loser gets `DuplicateKeyError`, which the API returns as 409.

3. **Ties.** The late leaderboard uses `$rank`, so ties share a rank and the next rank is skipped. The `limit` is applied after ranking, so tied employees at the cutoff all return.

4. **Headcount.** Department summary starts from `employees`, filters by `joined_on <= month_end`, then `$lookup`s logs with `preserveNullAndEmptyArrays`; employees with zero logs still stay in the group.

5. **100x data.** I would add monthly rollup collections for analytics, consider covered indexes for common filters, and bucket or archive old attendance logs.

6. **Department trend.** Generate the requested calendar rows inside MongoDB, look up the employees and attendance for each day, and calculate the seven-row moving average with `$setWindowFields`. The API only applies the contract's half-up output rounding; Python does not fill date gaps or compute headcounts.
