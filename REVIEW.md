# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes`, `compute_overtime`, `compute_attendance_date` | The starter used the wrong date interpretation for overnight shifts and mixed local/UTC logic. It also ignored the grace-period rule and the `shift_end <= shift_start` overnight rule from R1-R2. | A punch-in near midnight or an overnight shift produced the wrong `date` and wrong `late_minutes`/`overtime_minutes`. | Rebuilt the logic around IST conversion, overnight detection, and the precise 10-minute grace + floor-minute rules. |
| 2 | `punch_in` | The endpoint did not enforce the unique (`emp_code`, `date`) constraint with the correct MongoDB semantics and used a naive time conversion. It also ignored the contract that `status` is limited to `PRESENT`, `WFH`, `ON_DUTY`. | Duplicate punch-ins or invalid statuses returned incorrect results or accepted bad data. | Enforced the presence-only status set, computed the attendance date from the shift rules, and relied on the unique index to reject duplicates. |
| 3 | `list_employees` and `list_attendance` | Pagination was off by one and filtering did not respect the contract. `total` was not computed from the filtered set, and pagination skipped the wrong rows. | Requesting `page=1&page_size=20` returned the wrong records and totals. | Used `skip((page-1)*page_size)` with `count_documents` on the filtered query and sorted by the required keys. |
| 4 | `punch_out` | The endpoint closed the wrong record and did not validate the `punch_out > punch_in` and `<=24h` rules. It also did not compute derived values according to R3-R5. | A late punch-out or a reversed timestamp led to impossible records. | Found the most recent open punch-in for that employee, validated the interval, then computed `work_hours`, `overtime_minutes`, and `half_day` exactly as specified. |
| 5 | `regularize_attendance` | It did not recompute derived values after corrections, append audit history correctly, or reject a no-op patch or invalid punch-time combinations. | Editing a record with an unchanged payload or invalid punch times silently gave bad data. | Recomputed all derived fields after the final state, appended one history entry for actual changes only, and rejected invalid or no-op updates with 422/409 as needed. |
| 6 | Analytics endpoints | The starter had no analytics implementation, so no monthly summary, department summary, leaderboard, or trend endpoint existed. | Calls to the analytics routes raised 404 or returned placeholder behavior. | Added the monthly, department-level, leaderboard, trend, and explain endpoints with the required fields and date filters. |
| 7 | `health` | The starter always returned 200 even when MongoDB was unavailable. | A failing database connection should yield 503, but the starter did not check it. | Added an actual MongoDB ping check and returned 503 if MongoDB is unavailable. |

Also note anything you looked at and decided was **not** a defect, and why.

- The MongoDB field names themselves were not a defect. The assignment is clear that the database stores ISO date strings and BSON datetimes, and the API must convert them to epoch milliseconds at the API boundary.
- The use of a single file for the app was not a defect. The assignment explicitly says all code lives in `app/main.py`.
- The default `_id` index was not a defect. MongoDB creates it automatically; the assignment only requires the extra application indexes for the expensive query patterns.
