# Mock API reference

Start it with `python mock_api/server.py` (Python 3.9+, no dependencies). It listens on `http://localhost:8080`.

The API is deliberately **unreliable**, like most internal systems: about 15 percent of requests fail with 503, some return 429 with a `Retry-After` header, and latency varies from 50 ms to 3 seconds. This is how the case is evaluated. While developing you can turn it off with `CHAOS=0 python mock_api/server.py`, but your solution must also work with chaos on.

| Method and path | Purpose |
|---|---|
| `GET /health` | Liveness check, never fails |
| `GET /policies/{policy_number}` | Policy record. 404 if the number does not exist |
| `GET /policies?email={address}` | Look up policies by holder email |
| `GET /claims?policy_number={n}` | Claim history of a policy, including claims you created in this run. History entries include `invoice_number` where known |
| `POST /claims` | Register a claim. Required: `policy_number`, `loss_date`, `loss_type`. Other fields (for example `invoice_number`, `amount_claimed`) are stored as given and come back in `GET /claims` |
| `POST /claims/{claim_id}/decision` | Record a decision. `status` must be one of the values in `docs/policy.md`, section 7. Include `payout`, `reason` and any flags |
| `POST /outbox` | Queue an email. Required: `to`, `body`. Optional: `subject`, `in_reply_to`, `kind` (`info_request`, `decision`, `other`). Nothing is really sent |
| `GET /outbox` | See queued emails |

Write endpoints accept an `Idempotency-Key` header. Replaying the same key returns the original result instead of creating a second record.

| `POST /admin/reset` | Clears everything created in this run (claims, decisions, outbox, idempotency keys). Never fails, no chaos |

State lives in memory, so restarting the server also resets it. Use `POST /admin/reset` between runs, so a second run does not see the data of the first.

When you hand in, leave the files in `mock_api/runtime/` untouched, and do not restart or reset the server after your final run (both wipe them). The server writes `outbox.json` there (every queued email) and `write_log.jsonl` (every write). We use them to review what your agent actually did.
