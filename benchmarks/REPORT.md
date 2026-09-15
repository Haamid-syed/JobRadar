# JobRadar Focused Test and Benchmark Report

Date: 2026-09-15  
Baseline commit: `810ecee`  
Environment: Apple arm64, macOS 15.6.1, Python 3.12.13

## Scope

This pass covers cross-source deduplication, mocked scoring fallback/recovery, and
scraper concurrency. It makes no claims about live scraper speed, relevance
accuracy, API load, frontend performance, real provider availability, or LLM cost.

## Regression results

Command:

```bash
backend/venv/bin/python -m unittest discover -s tests -v
```

Result: **13/13 tests passed**.

- Within-batch duplicates from different sources collapse to one record and keep
  the richer description.
- The same opening rediscovered across sources on a later run is deduplicated when matching identity evidence exists.
- Similar but distinct titles and companies remain separate.
- Same-title openings with different explicit requisition IDs or specific
  locations remain separate within a batch and across database-backed runs.
- Matching requisition IDs, matching specific locations, and shared normalized
  application URLs retain cross-source matching.
- Ambiguous records with generic locations and different URLs remain separate.
- After simulated Gemini exhaustion, both mocked OpenRouter and mocked MLX remain
  available for two consecutive jobs.
- After an injected scoring interruption, a reopened temporary SQLite database
  retained all 3 committed pending jobs, resumed and scored 3/3, and contained 3
  unique rows after a repeated insert (0 new duplicates).

The regression-first run failed 3/6 tests: later-run cross-source deduplication,
subsequent-job OpenRouter fallback, and subsequent-job MLX fallback. Those three
tests passed after the fixes.

The later identity-policy regression run initially failed 4/8 deduplication tests:
different requisitions, different locations, later-run requisitions, and ambiguous
remote records. All identity tests pass after the evidence-aware fix.

## Deduplication identity policy

Jobs are first grouped by normalized company and title. A pair is kept separate
when both records provide conflicting requisition IDs or conflicting specific
locations. Within a group, matching requisition IDs, matching specific locations,
or the same normalized application URL provide enough evidence to merge records.
The richer description is retained when a merge occurs.

Generic values such as `Remote`, `Worldwide`, `Anywhere`, or a missing location do
not count as specific location evidence. When records have no comparable
requisition ID, no specific matching location, and different application URLs,
the case is ambiguous and both records are preserved. If only one source exposes
a requisition ID, matching specific location or URL can still support a merge;
otherwise the records remain separate. This conservative choice may retain some
duplicates, but avoids silently discarding potentially distinct openings.

Structured requisition IDs are currently captured from the Lever, Greenhouse, and
Ashby adapters. Aggregator-specific listing IDs are not treated as employer
requisition IDs.

## Controlled concurrency replay

Command:

```bash
backend/venv/bin/python benchmarks/replay_concurrency.py
```

Method:

- Uses the production `scheduler.run_scrapers` execution path.
- Replays 12 deterministic fixture sources with controlled 60–75 ms I/O delays.
- Each source returns 40 deterministic jobs: 480 listings per repetition.
- Runs one unmeasured warm-up and five measured repetitions per worker count.
- Compares a SHA-256 digest of every output field across all measured runs.

| Workers | Median duration | Listings/second | Speedup | Five measured durations (seconds) |
| ---: | ---: | ---: | ---: | --- |
| 1 | 0.8581 s | 559.4 | 1.00x | 0.8581, 0.8653, 0.8679, 0.8525, 0.8457 |
| 2 | 0.4476 s | 1,072.3 | 1.92x | 0.4693, 0.4478, 0.4424, 0.4382, 0.4476 |
| 4 | 0.2339 s | 2,051.9 | 3.67x | 0.2330, 0.2339, 0.2426, 0.2405, 0.2339 |

Equivalent outputs: **yes**  
Output digest: `58cc8ad89cf79957c86c5e9cda482140d6b35345b65b47634cf4187f1a079e29`

## Limitations

This is a controlled replay benchmark, not live scraping performance. The fixture
models I/O waiting with deterministic sleeps and generated records. It excludes
internet variability, remote rate limits, HTML/API parsing, filtering, enrichment,
database persistence, and scoring. Listings/second is therefore useful only for
comparing scheduler worker counts under this fixture; it is not an end-to-end
production throughput claim.
