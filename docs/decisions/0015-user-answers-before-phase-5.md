# 0015 — The user's answers to plan §16, and the working-hours template

- Status: accepted · Date: 2026-11-26

## Context
Plan §16 lists questions to answer before Phase 5. The user answered them on 2026-11-26.

## Decision
1. **Model for automatic runs:** each project's own `model` setting (default Sonnet). There is no automatic
   upgrade to Opus for large tasks. The Surplus project itself is set to Opus with high effort (ADR 0008).
2. **Projects root:** unchanged. New projects go to `~/surplus-projects` (`SURPLUS_PROJECTS_ROOT`), and any
   existing folder can be registered.
3. **Working hours:** weekdays 09:00-18:00, local time (the system zone unless `SURPLUS_TZ` is set). They are
   stored as a preference (`setting` table) and can be set from the dashboard, `PUT /api/preferences` or
   `surplus hours "mon-fri 09:00-18:00"`.
4. **Phone channel (Phase 6):** Telegram.
5. **System packages in agent containers:** no. The runner image stays fixed; agents install project
   dependencies with npm/uv as before.

### The template (`predictor/template.py`)
- The template only applies during cold start (ADR 0010), and P90 is never lowered below observed data.
- **Inside working hours** it is as conservative as having no data. For the 5-hour bucket, the user may use
  the whole window (5 pp per slot). For the weekly bucket, the whole weekly limit is spread over the working
  slots of a week.
- **Outside working hours** it is 0, replacing the no-data prior there. This is where it helps: slots never
  observed, such as nights when the PC was off, no longer block agent work.
- Simulator check: cold start with no history and only the template gives 0 user-blocked incidents, both for
  a user matching the template and for an evening user it describes wrongly. The kill switch and observed
  data cover the mismatch. It is a regression test.
- A changed template refits the forecast at once. A stored value that no longer parses counts as no template.
