# The three agents

All three are roles of one program and share only the database, so each can run as its own process (or Claude session),
stop, and be restarted without the others losing work.

| Agent | Command | Does | Never does |
|---|---|---|---|
| Finder | `python -m jobagent worker --role finder` | Reads the job boards every 6 hours, stores vacancies, scores them against your profile and policy | Touches applications or forms |
| Writer | `python -m jobagent worker --role writer`, plus a Claude session for letters (below) | Builds the tailored CV and cover letter PDFs for the strongest vacancies | Fills forms or submits |
| Applier | `python -m jobagent worker --role applier` | Fills application forms, attaches the writer's PDFs, queues and submits under the policy, reconciles e-mail | Writes letters or changes your profile |

`--role all` (the default) runs all three in one process.

## Writing cover letters with Claude
The worker cannot write prose on its own; without a written letter it falls back to a short template built from your facts.
A Claude session acts as the writer:
1. `python -m jobagent letters-needed --limit 5` gives the vacancies that need a letter, the rules and the facts that may be used.
2. Claude writes one letter per vacancy from those facts only, then stores it:
   `python -m jobagent letter-add <vacancy_id> - <<'TXT' ... TXT`
3. The letter is checked before it can be used: no em or en dashes, 120 to 380 words, names the employer, signed with your name,
   no generic phrases, and every number must appear in your confirmed facts or the vacancy. A rejected letter prints the reasons.
4. The writer worker renders accepted letters to PDF and the applier attaches exactly that PDF. Each application version records it.

Claims must paraphrase confirmed facts, not extend them. No paid model API is used; the writing happens in your Claude session.
