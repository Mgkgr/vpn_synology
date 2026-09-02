# HY2-USA Rename Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Replace the reserve profile with the supplied US Hysteria2 endpoint and rename every active gateway/dashboard reference from HY2-NL to HY2-USA.

**Architecture:** Keep a single canonical reserve ID, HY2-USA, in Mihomo and the dashboard. An idempotent SQLite data migration translates stored active references before API reads, while the privileged NAS applier changes only the HY2 config block, fallback link and its managed provider/file under a rollback transaction.

**Tech Stack:** Mihomo YAML, POSIX shell/Python 3 on DSM, FastAPI/Pydantic, SQLAlchemy/SQLite, React/TypeScript, PowerShell.

**Spec:** docs/superpowers/specs/2026-08-31-hy2-usa-rename.md

## Global Constraints

- Do not store the supplied Hysteria2 credentials in Git, output or command-line arguments.
- Preserve WireGuard profiles/keys, WG-IMP, GeoData, DNS and every unrelated managed rule.
- Preserve a closed backup and restore files after any failed post-change validation.
- Use HY2-USA for new data; legacy HY2-NL is permitted only in one-time migration/rollback detection.

---

### Task 1: Make HY2-USA the dashboard contract

**Files:**
- Modify: backend/app/schemas.py, backend/app/policy_rules.py, backend/app/collectors.py, backend/app/routes.py
- Modify: frontend/src/api/types.ts, frontend/src/api/client.ts, frontend/src/pages/OverviewPage.tsx, frontend/src/pages/RoutesPage.tsx, frontend/src/pages/RulesPage.tsx
- Modify tests: backend/tests/test_collectors.py, backend/tests/test_routes.py, frontend/src pages tests

**Interfaces:**
- Produces the canonical reserve value HY2-USA in controller probes, rule actions and all API responses.
- Consumes only live Mihomo groups containing WG-IMP and HY2-USA.

- [ ] Step 1: Write failing backend/frontend tests that require HY2-USA.

~~~
assert overview.json()["fallback"]["reserve"] == "HY2-USA"
expect(screen.getByText("Резервный: HY2-USA")).toBeVisible()
~~~

- [ ] Step 2: Run focused tests and confirm they fail because the old reserve ID is expected.
- [ ] Step 3: Replace active literals and managed provider mapping with HY2-USA and managed-hy2-usa.txt; leave only migration constants at the old name.
- [ ] Step 4: Run backend and frontend focused suites and verify the API contract and labels.
- [ ] Step 5: Commit the contract change.

### Task 2: Migrate persisted dashboard references

**Files:**
- Modify: backend/app/db.py
- Test: backend/tests/test_db.py

**Interfaces:**
- Consumes SQLite tables managed_rule_policies, probe_events and route_events.
- Produces idempotent data changes from HY2-NL to HY2-USA before API/collector use.

- [ ] Step 1: Write a failing test which seeds all three record types with HY2-NL, runs create_all, then asserts each active column is HY2-USA.
- [ ] Step 2: Run the test and verify the old values remain before implementation.
- [ ] Step 3: Add one transactional SQLite migration that updates policy action, probe target/outbound and route previous/new outbound.
- [ ] Step 4: Run the test twice to prove migration idempotence.
- [ ] Step 5: Commit the DB migration.

### Task 3: Apply the rename safely on DSM

**Files:**
- Modify: deploy/replace-hy2-profile.ps1, deploy/scripts/replace-hy2-profile.sh
- Modify: deploy/scripts/configure-managed-rules.sh, deploy/scripts/apply-fallback-policy.sh
- Test: deploy/test-replace-hy2-profile.ps1

**Interfaces:**
- Consumes a temporary mode-0600 profile created from masked PowerShell input.
- Produces HY2-USA Mihomo YAML, managed-hy2-usa.txt, a validated hot-reload and a closed rollback bundle.

- [ ] Step 1: Write a failing fixture test with managed providers/rules and assert the output changes only HY2 references while retaining unrelated rules and the fallback interval.
- [ ] Step 2: Run it and verify HY2-NL remains in the generated fixture before implementation.
- [ ] Step 3: Extend the privileged applier to atomically rename the active HY2 block, fallback member, provider name/path, managed rule file and rule line; preserve metadata and restore all files on failure.
- [ ] Step 4: Make the delay probe use HY2-USA, then run syntax, fixture and NAS shell syntax tests.
- [ ] Step 5: Commit the DSM applier.

### Task 4: Deploy and verify

**Files:**
- Modify: README.md, docs/operations.md

- [ ] Step 1: Document one command, masked secret prompts and safe rollback output.
- [ ] Step 2: Run backend pytest, frontend tests/build and deployment-script tests.
- [ ] Step 3: Deploy the dashboard, run the privileged rename and record only redacted status lines.
- [ ] Step 4: Verify controller version, fallback group, policy provider, database migration and separate HY2-USA delay.
- [ ] Step 5: Commit documentation and final verification.
