## 1. Router Flow

- [x] 1.1 Refactor `RouterService.route()` so access-filtered available Agents become the candidate boundary used by Evidence Provider and LLM.
- [x] 1.2 Move strong Evidence route override handling before any tag/semantic recall effect and ensure a valid strong override short-circuits LLM routing.
- [x] 1.3 Return a no-permission route response when a strong fixed-question match targets an Agent rejected by access filtering, without calling the LLM.
- [x] 1.4 Update tag filtering helpers so they still compute match metadata but do not remove Agents from the effective candidate set in this version.
- [x] 1.5 Preserve route metadata for `available_agent_ids`, effective candidate IDs, tag match status, matched Agent IDs, and tag match details.
- [x] 1.6 Ensure weak Evidence hints and candidate IDs enrich context without shrinking the LLM candidate set.

## 2. Validation And Tests

- [x] 2.1 Update existing Router tests that currently expect tag filtering to reduce Evidence Provider and LLM candidates.
- [x] 2.2 Add a regression test proving strong fixed-question route override does not call the LLM.
- [x] 2.3 Add a regression test proving tag matches do not prevent a strong fixed-question target from being routed when the target is access-allowed.
- [x] 2.4 Add a regression test proving strong fixed-question matches rejected by access filtering return a no-permission message and do not call the LLM.
- [x] 2.5 Add or keep a regression test proving unauthorized Agents never appear in Evidence Provider candidates, LLM candidates, route context candidates, or invocation previews.
- [x] 2.6 Run backend tests with `.venv/bin/python -m pytest`.

## 3. Documentation

- [x] 3.1 Update `docs/App-Desc/contracts/api.md` to describe the new order: access filtering, strong deterministic rules, tag/semantic observe-only signals, LLM routing.
- [x] 3.2 Update Evidence or handoff documentation to state that strong fixed-question matches are strong routes and bypass LLM when access-allowed.
- [x] 3.3 Document that strong fixed-question matches rejected by access filtering return a no-permission prompt.
- [x] 3.4 Update any demo or delivery docs that describe tag filtering as candidate pruning.

## 4. OpenSpec Validation

- [x] 4.1 Run `openspec validate revise-intent-filter-order --strict`.
- [x] 4.2 Fix any OpenSpec formatting or requirement errors before implementation begins.
