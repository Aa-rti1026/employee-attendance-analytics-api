# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** I created a unique index on `employees.emp_code`, a unique composite index on `attendance_logs.emp_code + date`, plus a date/emp-code sort index and a few supporting indexes for status and late-minute filtering. These are the queries that dominate the assignment: employee lookup by code, duplicate punch-in prevention, list-by-date sorting, and monthly summary filtering. I did not keep a broad compound index on every field because it would be too expensive for writes and would not help the main filters enough.

2. **Punch-in race.** If two identical punch-in requests land at the same instant, MongoDB sees the same unique compound key (`emp_code`, `date`). The first insert succeeds; the second one raises a duplicate-key error and the API converts that to a 409 response. That gives exactly one 201 and the rest 409 without relying on a slow check-then-insert pattern.

3. **Ties.** The leaderboard uses standard competition ranking. If two employees are tied at the cutoff, both keep the same rank, and the next rank is skipped. The API returns every row whose rank is less than or equal to `limit`, so a tie can produce more rows than `limit`.

4. **Headcount.** For department summaries, I build the employee list for the month first and include everyone whose `joined_on` is on or before the end of the month, even if they have zero logs. That ensures zero-log employees still count toward headcount.

5. **One thing I would change** for 100x the data is to push the heavy aggregation work into MongoDB pipelines and maintain precomputed monthly summary collections, instead of scanning large arrays in Python. That would keep latency stable as the dataset grows.
