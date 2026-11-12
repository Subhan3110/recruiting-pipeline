# Sources

Discovery and submission are separate capabilities. A public listing API says nothing about whether applications can be submitted.

| Source | Discover | Describe | Detect closed | Locate form | Account | Prepare | Submit | Verify outcome | Status |
|---|---|---|---|---|---|---|---|---|---|
| Greenhouse Job Board API | yes | yes | yes | yes | none needed | yes | browser | page + e-mail | implemented |
| NAV Arbeidsplassen feed | planned | planned | planned | links out | n/a | no | no | no | not implemented |
| Lever postings API | planned | planned | planned | hosted form | none | planned | browser | page + e-mail | not implemented |
| Ashby posting API | planned | planned | planned | hosted form | none | planned | browser | page + e-mail | not implemented |
| Workday | no public API | - | - | tenant sites | per-tenant account | - | browser | portal status | not implemented |

## Greenhouse (verified live 2026-11-12)
- `GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true`: full listing, no authentication, no pagination.
- `GET .../jobs/{id}?questions=true`: the application form schema (`questions`, `location_questions`, `demographic_questions`).
  A 404 means the job is closed.
- The API's application POST requires the employer's API key, so candidates cannot use it. Submission uses the hosted form at
  `https://job-boards.greenhouse.io/{board}/jobs/{id}`.
- Hosted form facts observed in a real browser session: element ids equal API field names (`#first_name`, `#resume`,
  `#question_<id>`); selects are react-select comboboxes whose options render in `.select__menu`; multi-selects are checkboxes with id
  `<name>_<value>`; the phone field has a country combobox `#country`; the location field is an async autocomplete `#candidate-location`
  backed by `api-geocode-earth-proxy.greenhouse.io`; scripts load from `job-boards.cdn.greenhouse.io`; reCAPTCHA loads from `www.recaptcha.net`.
- No candidate account exists. Some employers ask for an e-mailed security code after submit.
- Rate limits: none published for the board API; the adapter keeps one request per second and backs off on 429/5xx.

### What has and has not been verified
| Step | Evidence |
|---|---|
| Listing, descriptions, form schema | Live calls for 34 boards |
| Filling a real hosted form (text, dropdowns, checkboxes, CV and letter upload, location) | Live dry run, synthetic profile, submit not clicked |
| Submit click, confirmation page, security-code prompt, reCAPTCHA behaviour in headless Chromium | **Local fixture only.** Not yet exercised on a real employer form |
| Acknowledgement e-mail matching | Fixture mailbox only |

The first real submission should be watched: if reCAPTCHA rejects headless Chromium, the attempt ends as a user action
(`blocked: captcha`) or as `uncertain`, never as a silent retry.

## Adding an adapter
Subclass `SourceAdapter`, set `capabilities` honestly, yield dicts for `vacancies.upsert`, key employers by tenant
(`<source>:<tenant>`), and add a row above with the date you verified the endpoints.
