# Recruiting Pipeline

Source for Subhan's recruiting tracker. The live page is a claude.ai artifact:
https://claude.ai/artifact/WDTdfsGrm3vCr7D5JqtmFa

## Files
- `pipeline.html`: the whole app (HTML, CSS and vanilla JS in one file).

## Editing without Claude
1. Edit `pipeline.html` here on GitHub (press `.` for the browser editor) or locally in VS Code.
2. Preview: open the file in a browser. Outside claude.ai it runs in **local preview** with sample data, so you can check layout and logic. Edits in preview are not saved.
3. Commit and push.
4. Go live: in the Out Reach project in Claude, say "publish pipeline.html from the repo". The live URL and all saved data stay the same.

## Where things live in the code
- `:root` block at the top of `<style>`: colours and fonts (light and dark theme).
- `STAT`: status lists for applications and outreach.
- `typeOf()`: how roles are tagged Summer, Off-cycle, Graduate, Part-time, Event.
- `drawFunnel()`, `appFunnel()`, `outFunnel()`: the funnels.
- `renderToday()`: the Today queue on Overview.
- `renderApps()`: the grouped Applications tab.
- `renderTriage()`, `renderProspects()`: LinkedIn and Email tabs.
- `fmtDraft()`: formats drafts with greeting, paragraphs and sign-off.
- `loadSample()`: sample data for local preview.
- Boot block at the bottom: connects to the claude.ai database (`claude.use("db")`).

## Data (stored in the artifact database, not in this repo)
- `applications`: company, role, division, location, status, applied, deadline, rejectedAt, type, source, notes
- `outreach`: name, firm, title, channel (LinkedIn/Email), status, sent, followedUp, lastContact, address, via, notes
- `prospects`: name, firm, title, why, profileUrl, liDraft, emailGuess, emailConfidence, emailSubject, emailBody, status
- `triage/latest`, `emailtriage/latest`: latest inbox check with buckets and drafts
- `triage/done`: drafts marked as sent

Statuses: Preparing, Applied, Online test, HireVue, Interview, Offer, Rejected, Withdrawn (applications); Drafted, Sent, Replied, Call booked, Call done, No reply, Declined (outreach).
