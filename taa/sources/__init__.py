"""Source clients. One module per external data source.

Each module exports an async `fetch(antigen) -> SourceResult` function with
its own per-source asyncio.Semaphore matching that source's published rate limit
(decision 1A from /plan-eng-review). No shared global rate-limiter — explicit
per-source caps.
"""
