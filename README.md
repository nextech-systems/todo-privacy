# ToDo Privacy

Keeps ERPNext ToDos visible only to the people involved: the person it is
assigned to (`allocated_to`) and the person who created or assigned it
(`assigned_by`). Administrator, and anyone deliberately given the ToDo Auditor
role, can read all of them.

Version 1.2.4, for Frappe v14.101.1 / ERPNext v14.92.14. Not verified on v15.

## Why this app exists

Broad ToDo permissions can let users see assignments outside their own work. This app applies a consistent participant and auditor policy to ToDos and to the places where ToDo text can appear.

## Install on a disposable site first

```bash
# Run from the bench directory on a staging site first.
bench get-app https://github.com/nextech-systems/todo-privacy.git
bench --site staging.example.com install-app todo_privacy
```

Installation, migration, and the daily job can reapply the permission baseline. The install path can update assignment metadata, remove in-scope shares, and sanitize assignment comments. Review those effects with site administrators, back up the database, and use the status and rollback procedures below when evaluating the app.

## What this app does

| Part | File | Effect |
| --- | --- | --- |
| Exact permission baseline | `policy.py` | ToDo Custom DocPerm is exactly two rows: `All` (read, write, create, delete, report, print, email; no share, no export) and `ToDo Auditor` (read, report, print). Any other row, flag, duplicate, order or ToDo DocShare is drift. Re-applied on install, after every `bench migrate` and daily. Role Permission Manager refuses ToDo changes outright. |
| Administrator sees all | `policy.py` | Frappe gives Administrator every role, so the Auditor row restores Administrator's full ToDo list. Nobody else holds the role unless granted; `status()` lists every holder. |
| Decisions from the stored record | `permissions.py` | Who may read, change or delete an existing ToDo is decided from the row in the database, never from the submitted document. Forged `assigned_by`/`allocated_to` values are ignored. |
| Creator recorded | `permissions.py`, `guards.py` | New ToDos always record their creator as `assigned_by`. It cannot be changed afterwards except by Administrator or trusted server code. |
| Creator backfill | `policy.py` | `assigned_by = owner` where empty, in one conditional UPDATE per batch. No `modified` change, no events. Snapshotted. |
| Share block | `guards.py`, `linked.py` | No share may expose ToDo content: shares on a ToDo, or on a Comment, Version, Communication or File linked to one (for Communication, by primary reference or timeline link), are refused on insert and every save, for named users and "everyone", for everyone including Administrator. A shared record cannot be moved onto a ToDo, and a shared Communication cannot gain a ToDo timeline link. Existing in-scope shares are removed (snapshotted). Sharing anything else works as before. |
| CRM re-link | `permissions.py` | A non-participant may move a ToDo to a new reference (ERPNext Lead to Opportunity) only when every other field, including Custom Fields, is unchanged, and they can read the old record and write the new one. |
| Timeline and comments | `guards.py`, `policy.py`, `redaction.py` | Every "Assigned" timeline comment is stored without the ToDo description, whatever code writes it; existing ones are cleaned (snapshotted); the form sidebar hides other people's ToDo descriptions. |
| One redaction rule | `redaction.py` | Every wrapper uses the same functions to decide what ToDo text a user may see. Non-participants keep the structure (who, status, date, reference), never the text. |
| Run lock | `install.py` | A per-site database lock stops install, migrate and the daily job from rewriting at the same time; a second run skips and logs it. |
| Comment and Version records | `permissions.py` | System Manager can normally list every Comment and Version. Rows about a ToDo now follow the participant rule, decided from the stored row, so a comment cannot be re-pointed away from (or onto) someone else's ToDo. |
| Communication records | `permissions.py`, `redaction.py` | A Communication filed on a ToDo, or linked to one by a secondary timeline link, follows the participant rule for every link, in lists, counts, search and direct reads. Frappe's own check only looks at the primary reference. Form timelines of other records omit ToDo-linked Communications the reader is not part of, and page by visible emails so older ordinary emails stay reachable through Load More. Communications with no ToDo link are unaffected. |
| Reader wrappers | `overrides.py` | "Assigned To" sidebar counts include only readable records; ERPNext CRM activity panel checks record access and redacts descriptions. |
| Report and digest | `todo_privacy/report/my_open_todos`, `email_digest.py` | New "My Open ToDos" report lists ToDos assigned to or by the user. ERPNext Email Digest now includes ToDos assigned to the recipient. Frappe's standard "ToDo" report is unchanged and still omits those (upstream behaviour). |
| Snapshots | `snapshot.py` | Full rows in `<site>/private/todo_privacy/` (mode 600) with site, time, app and Frappe versions and a SHA-256 of the contents. Restores refuse an altered file or another site, refuse another version unless `force=1`, and only touch rows still holding the value this app wrote. |

Fixtures are deliberately not used: fixture sync inserts and updates rows but
never deletes them, so it can neither remove the broad rows nor stop them
coming back.

## Who notices a change

Users with previously broad ToDo access stop
seeing other people's ToDos, and System Managers stop seeing ToDo comments and
change history in the Comment and Version lists. Tell them before go-live.

## Check status

```bash
bench --site S execute todo_privacy.policy.status
```

Required: `baseline_problems` empty; `auditor_holders` as intended (normally
empty); every `hooks_active` entry `evaluated_first: true`; every
`overrides_active` entry `ours: true`. In v14 the FIRST app's whitelisted
override and the LAST app's class override win, so another app can silently
take over one of these.

## Staging tests

```bash
bench --site staging.example.com run-tests --app todo_privacy
```

Use a disposable Frappe site with test execution enabled and outbound email disabled. The suite creates synthetic records and exercises record permissions, redaction, and custom-field behavior. The renamed release passed syntax checks; its Frappe-dependent tests still need to run on a site.

## Rollback

Take a database backup first. Do all steps in one sitting, so no migrate or
daily job re-applies the baseline in between.

1. `bench --site S execute todo_privacy.snapshot.list_snapshots`
2. Permissions, ToDo shares and the Auditor role, from the EARLIEST
   `permissions-*.json` (taken before install):
   `bench --site S execute todo_privacy.snapshot.restore_permissions --kwargs "{'path': '<site>/private/todo_privacy/permissions-....json'}"`
   Rolls back and reports if the result is not identical to the snapshot.
   Refuses if someone has been granted ToDo Auditor since. Snapshots taken
   by another app version need `'force': 1` after checking; a snapshot with
   its checksum removed is always refused. Each snapshot restores only the
   share scope it recorded (no marker = direct ToDo shares; 1.2.2 scope;
   1.2.3+ scope); an unknown scope is refused before anything is written.
   In production, restore with snapshots written by the installed version.
3. Each `backfill-*.json`: `... snapshot.restore_backfill --kwargs "{'path': '...'}"`
4. Each `comments-*.json`: `... snapshot.restore_comments --kwargs "{'path': '...'}"`
5. `bench --site S uninstall-app todo_privacy`. The app creates no
   tables; uninstalling removes its "My Open ToDos" report record and Module Def.

After step 5 ToDos are as open as before the install, so plan the follow-up
fix before running it.

## License

[MIT](LICENSE).
