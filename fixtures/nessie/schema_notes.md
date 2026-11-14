# Nessie sandbox schema notes (captured 2026-11-14)

Base URL: https://api.nessieisreal.com (auth = `key` query param).

## Observed (live)
- GET /customers -> 200 `[]` (sandbox had zero customers before this run).
- POST /customers -> 201 `{"code":201,"message":"Customer created","objectCreated":{...,"_id":"<uuid>"}}`. Write envelope = code/message/objectCreated. Ids are UUID strings here.
- Company customer: bd2261d1-601e-41b5-93e8-67f868fe464e (Acme / Co (synthetic)), created by this step.
- GET /customers/{id} -> 200 bare object (first_name, last_name, address{street_number,street_name,city,state,zip}, _id). Reads are bare JSON, no envelope, no pagination seen.
- GET /customers/{id}/accounts -> 200 `[]`.
- GET /accounts -> 200 `[]`; GET /merchants -> 200 `[]`. The sandbox is completely empty, so NO account/bill/deposit/transfer/loan/purchase/merchant schema could be captured. bills/deposits/withdrawals/transfers/loans/purchases.json are `[]` because there is no account to query (not queried); merchants_sample.json is `[]`.
- Errors: unknown customer -> 404 body `"Customer not found"` (JSON string); unknown account -> 404 empty body; missing `key` -> 502 `{"message":"Internal server error"}`.
- SURPRISE: an invalid key (`key=bad`) on GET /customers returned 200 `[]`, so reads do not appear to validate the key. Do not rely on a 401 to detect misconfiguration.
- http://api.nessieisreal.com refused the connection (this is the NESSIE_BASE_URL in .env; change it to https). https://prod.nessieisreal.com returns the docs SPA HTML (not an API).

## Verified (probe 2026-11-14, then deleted)
Date format: `YYYY-MM-DD` strings everywhere (creation_date is server-set today's date). Ids are UUID strings. Writes: 201 `{code,message,objectCreated}`; PUT: 202 `{code,message,objectUpdated}`; reads bare JSON.

BALANCE IS AN INTEGER: `balance` 1234.56 was stored/returned as 1234 (truncated). Deposit/withdrawal `amount` 50.5 returned 50 (truncated to int). Only bill `payment_amount` kept decimals (100.25). Treat Nessie amounts as whole dollars; do not rely on cents.
Balance side effects: NONE. Completed deposit, pending deposit, completed withdrawal, completed purchase and pending transfer all left account balance unchanged. PUT /accounts/{id} {"balance":999.5} accepted (202) but balance unchanged; nickname did change.

- account: _id, type, nickname, rewards, balance(int), account_number (16-digit string, server), customer_id. Create body accepted: type, nickname, rewards, balance. GET /customers/{id}/accounts returns array.
- bill: _id, status ("pending" accepted), payee, nickname, creation_date, payment_date, recurring_date (int), upcoming_payment_date (server-computed, e.g. 2026-12-01 from recurring_date 1), payment_amount (float), account_id. Future payment_date accepted; past (2020-01-01) also accepted. WARNING: a bill created without nickname/recurring_date is stored but then GET /accounts/{id}/bills returns 400 `"3 validation errors for Bill
nickname
  field required...recurring_date...upcoming_payment_date"`. Always send nickname + recurring_date. GET /bills/{id} works; PUT /bills/{id} {"payment_date":"2026-11-26"} -> 202, persisted. DELETE /bills/{id} -> 200 "Bill deleted".
- deposit: _id, medium ("balance"), transaction_date, status ("completed", "pending" accepted; future-dated pending 2026-11-28 accepted), amount (int), description (REQUIRED; omitting -> 400 `1 validation error for DepositCreate
description
  field required (type=value_error.missing)`). No payee_id/creation_date in the GET list item (creation_date appears only in the create envelope). Free-text description round-trips (use it to carry invoice ref). Invalid status value / non-ISO date were not testable (description error fired first). GET /deposits/{id} works; DELETE /deposits/{id} -> 200 "".
- withdrawal: _id, type ("withdrawal"), transaction_date, status, payer_id, medium, amount (int), description. Create also returns creation_date. No GET/DELETE /withdrawals/{id} route (403 Missing Authentication Token).
- transfer: create body is ONLY {transaction_date, status, amount, description}. `medium`, `payee_id`, `payer_id` are REJECTED: 400 `"2 validation errors for TransferCreate
medium
  extra fields not permitted (type=value_error.extra)
payee_id
  extra fields not permitted (type=value_error.extra)"`. Missing fields -> `field required` for transaction_date, status, amount, description. Accepted: status "pending". Created item has no payer/payee link. List item key is `id` (not `_id`): {id, transaction_date, status, amount, description}. Listed only under the source account; the other account's transfers -> 404 `"No transfers found for this account"` (also what an account with no transfers returns). Transfers move no money and cannot reference a destination account, so model internal transfers via description text.
- purchase: _id, type ("merchant"), merchant_id, payer_id, purchase_date, amount, status ("completed"), medium ("balance"), description. No GET/DELETE /purchases/{id} route.
- merchant: _id, name, category, address{street_number,street_name,city,state,zip}, geocode{lat,lng}. GET /merchants lists all (global, not per customer). DELETE /merchants/{id} not supported (403 Missing Authentication Token); the probe merchant c716f198-0537-4d4f-be0b-a7338f2720fa ("zz-probe Merchant") remains in the sandbox.
- loans: GET /accounts/{id}/loans -> 200 `[]`.
- Delete: DELETE /accounts/{id} -> 200 empty body; it does NOT cascade (bills/deposits stay and remain readable by account id, so delete children first: bills and deposits via their own routes; withdrawals/purchases/transfers cannot be deleted). Customer accounts back to `[]` after cleanup. GET /bills (no account) -> 403 route missing.
