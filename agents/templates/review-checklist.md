# Review Checklist

Standard checks for the 8-lens review. QA runs all lenses every cycle.
Add project-specific checks as they are discovered.

## Lens 1 - Apple Sync & Parity

- [ ] Every file in `qa/mac-changes.md` inventory is present in `apple/`
- [ ] No unexpected files in the commit that were not in the stream
- [ ] Git push completed successfully (no partial commits)

## Lens 2 - Security

- [ ] No hardcoded secrets, API keys, or env-specific values
- [ ] Keychain used for sensitive storage (Apple)
- [ ] HTTPS enforced for all network calls
- [ ] Nothing sensitive in logs or source control
- [ ] Auth flows correct

## Lens 3 - Code Quality

- [ ] Readable, consistent naming
- [ ] No duplication
- [ ] Errors handled gracefully
- [ ] Tests cover core behavior
- [ ] No obvious performance issues
- [ ] Conventional commit prefixes used

## Lens 4 - API Contract Compliance

- [ ] Implementations match `shared/api-spec.yaml`
- [ ] Request/response shapes correct
- [ ] Status codes correct
- [ ] Error response format consistent

## Lens 5 - Architecture

- [ ] Appropriately scoped for the task
- [ ] No unnecessary abstractions
- [ ] No missing abstractions
- [ ] No files modified outside the agent's owned directories

## Lens 6 - Cross-Stack Consistency

- [ ] Same concept uses same name everywhere
- [ ] Error patterns consistent across stacks
- [ ] Data models aligned
- [ ] Dates and numbers handled the same way

## Lens 7 - Web-Specific

- [ ] Dev server runs without errors
- [ ] API base URL from environment variable
- [ ] User input sanitized before rendering
- [ ] Loading, error, and empty states handled
- [ ] Semantic HTML and ARIA attributes present
- [ ] Key props on all list renderings

## Lens 8 - Anti-Pattern Check

- [ ] Code does not repeat any entry in `compound/anti-patterns.md`
- [ ] Filtered by stack tags for stacks involved
- [ ] New anti-pattern discovered? Flag for PM

## Project-Specific

<!-- Add checks discovered through reviews below -->
