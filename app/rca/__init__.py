"""AI Root Cause Analysis (RCA) — DIAGNOSIS ONLY.

Given a Jira defect, this package investigates the codebase and related
artifacts and produces a *diagnosis*: the most likely root cause pinpointed to a
file/symbol/lines, why it occurs, and the supporting evidence trail.

Hard scope boundary: every module here is read-only with respect to the
codebase. Nothing in this package generates fixes, patches or diffs, edits
files, runs or modifies tests, or touches branches. The deliverable is an
explanation and a pointer to the cause — nothing more.
"""
