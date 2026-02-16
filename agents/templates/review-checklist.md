# Review Checklist

Standard checks for the 8-lens review. Add project-specific
checks as they're discovered.

## All Stacks

- [ ] No hardcoded secrets, API keys, or env-specific values
- [ ] Error handling covers network failures and edge cases
- [ ] Conventional commit prefixes used
- [ ] No files modified outside the agent's owned directories
- [ ] `shared/decisions.md` updated if any decisions were made

## Apple

- [ ] Builds without warnings in Xcode
- [ ] Tests pass
- [ ] Keychain used for sensitive storage
- [ ] HTTPS enforced for all network calls
- [ ] API URLs from config, not hardcoded

## Back End

- [ ] API responses match `shared/api-spec.yaml`
- [ ] Input validated at boundary
- [ ] Database migrations included for schema changes
- [ ] Error response format consistent
- [ ] Tests cover new endpoints

## Front End

- [ ] Dev server runs without errors
- [ ] API base URL from environment variable
- [ ] User input sanitized before rendering
- [ ] Loading, error, and empty states handled
- [ ] Semantic HTML and ARIA attributes present

## Project-Specific

<!-- Add checks discovered through reviews below -->
