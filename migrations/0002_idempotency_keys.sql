-- 0002_idempotency_keys: remember the order created for each Idempotency-Key,
-- so a client retry returns the original order instead of a duplicate.
CREATE TABLE idempotency_keys (
    key          TEXT    PRIMARY KEY,
    request_hash TEXT    NOT NULL,
    order_id     INTEGER NOT NULL REFERENCES orders (id),
    created_at   TEXT    NOT NULL
);
