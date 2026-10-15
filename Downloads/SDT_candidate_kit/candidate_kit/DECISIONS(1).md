# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` to enforce identity, and a unique compound index on `attendance_logs.emp_code` plus `date` to guarantee one attendance record per employee per day even when requests race. Department/join-date and department/employee-code indexes support employee filters and headcount. Attendance indexes cover date-sorted lists, employee/date/status filters, and the latest punch-in lookup. I avoided indexing every derived field because that increases write cost without helping the main contract queries.

2. **Punch-in race.** Both requests may initially find no record, but the unique `(emp_code, date)` index permits only one insert. The winner returns 201; the other receives `DuplicateKeyError`, which the API maps to 409.

3. **Ties.** MongoDB `$rank` gives tied totals the same rank and skips the next rank. Filtering happens after ranking, so all rows with rank at or below `limit` are returned, even if that means more rows than the limit. Tied rows are ordered by employee code.

4. **Headcount.** Department summaries start from employees and look up their logs, preserving employees with no logs while excluding those who join after the month. Trend headcount is looked up for each day using `joined_on <= date`.

5. **100x scale.** I would validate plans and index selectivity on production-like data, consider pre-aggregated daily facts for expensive reports, and introduce bounded caching for repeated analytics while keeping attendance writes authoritative.
