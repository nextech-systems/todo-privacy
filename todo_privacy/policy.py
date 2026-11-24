"""ToDo privacy policy: one exact, enforced permission baseline for the ToDo DocType.

Why this works
--------------
Frappe's own ToDo rules (frappe/desk/doctype/todo/todo.py) limit a user to
ToDos where they are ``allocated_to`` or ``assigned_by``, but only when the
user holds no non-automatic role that the ToDo permission table grants. Broad custom permission grants can bypass that filter.

The baseline is exactly two Custom DocPerm rows:

1. ``All``: the everyday rights every user needs on their own ToDos, with
   sharing off.
2. ``ToDo Auditor``: read-only. Frappe gives Administrator every role, so this
   row is what lets Administrator list all ToDos. No user holds the role unless
   someone grants it on purpose, and ``status()`` reports every holder.

Custom DocPerm rows fully replace the standard ToDo permissions, which include
System Manager, so the table is never left empty.

Fixtures are not used: fixture sync inserts and updates rows but never deletes
them, so it can neither remove the broad rows nor stop them coming back.
"""

import json

import frappe

from todo_privacy.redaction import ASSIGNED_SPLIT

TODO = "ToDo"
AUDITOR_ROLE = "ToDo Auditor"
PERM_FIELDS = (
	"select",
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"set_user_permissions",
	"share",
	"print",
	"email",
)
_ALL = {"read", "write", "create", "delete", "report", "print", "email"}
_AUDITOR = {"read", "report", "print"}
BASELINE = (
	{"role": "All", "permlevel": 0, "if_owner": 0, **{f: int(f in _ALL) for f in PERM_FIELDS}},
	{"role": AUDITOR_ROLE, "permlevel": 0, "if_owner": 0, **{f: int(f in _AUDITOR) for f in PERM_FIELDS}},
)
SKIP_BACKFILL_OWNERS = ("Guest", "")


# ------------------------------------------------------------------ baseline


def _custom_rows():
	return frappe.db.sql(
		"select * from `tabCustom DocPerm` where parent=%s order by idx asc, creation asc, name asc",
		(TODO,),
		as_dict=True,
	)


def _shape(row):
	return {
		"role": row.get("role"),
		"permlevel": int(row.get("permlevel") or 0),
		"if_owner": int(row.get("if_owner") or 0),
		**{f: int(row.get(f) or 0) for f in PERM_FIELDS},
	}


def check():
	"""List every way the live ToDo permissions differ from the exact baseline.

	An empty list means the baseline holds. Read-only.
	"""
	problems = []
	if not frappe.db.exists("Role", AUDITOR_ROLE):
		problems.append(f"role {AUDITOR_ROLE} is missing")

	live = [_shape(r) for r in _custom_rows()]
	expected = [dict(r) for r in BASELINE]
	if live != expected:
		live_roles = [(r["role"], r["permlevel"], r["if_owner"]) for r in live]
		for want in expected:
			key = (want["role"], want["permlevel"], want["if_owner"])
			matches = [r for r in live if (r["role"], r["permlevel"], r["if_owner"]) == key]
			if not matches:
				problems.append(f"missing baseline row {key[0]} (permlevel 0)")
			elif len(matches) > 1:
				problems.append(f"{len(matches)} rows for {key[0]} (permlevel 0); expected one")
			elif matches[0] != want:
				diff = sorted(f for f in PERM_FIELDS if matches[0][f] != want[f])
				problems.append(f"{key[0]} row has wrong rights: {', '.join(diff)}")
		allowed = {(r["role"], r["permlevel"], r["if_owner"]) for r in expected}
		for key in live_roles:
			if key not in allowed:
				problems.append(f"extra row: role {key[0]}, permlevel {key[1]}, if_owner {key[2]}")
		if not problems:
			problems.append("rows are in the wrong order")

	from todo_privacy.linked import scoped_share_count

	shares = scoped_share_count()
	if shares:
		problems.append(f"{shares} DocShare rows give access to ToDos or their comments, history, emails or attachments")
	return problems


def enforce_permissions(reason="manual"):
	"""Make the ToDo permission table exactly the baseline. Idempotent.

	The caller takes a snapshot first (see install.py). Rows are rewritten
	directly, as Frappe's own Role Permission Manager does, then caches are
	cleared. Returns the problems that were corrected (empty if nothing changed).
	"""
	problems = check()
	if not problems:
		return []

	if not frappe.db.exists("Role", AUDITOR_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": AUDITOR_ROLE,
				"desk_access": 1,
				"description": "Read-only access to every ToDo. Grant only to named auditors.",
			}
		).insert(ignore_permissions=True)

	frappe.db.delete("Custom DocPerm", {"parent": TODO})
	for idx, row in enumerate(BASELINE, start=1):
		frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": TODO,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"idx": idx,
				**row,
			}
		).db_insert()

	from todo_privacy.linked import delete_scoped_shares

	delete_scoped_shares()
	_clear_caches()
	_log("ToDo privacy baseline enforced", {"reason": reason, "corrected": problems})
	return problems


def _clear_caches():
	from frappe.core.doctype.doctype.doctype import clear_permissions_cache

	frappe.clear_cache(doctype=TODO)
	clear_permissions_cache(TODO)
	if getattr(frappe.local, "role_permissions", None):
		frappe.local.role_permissions = {}


def auditor_holders():
	return frappe.get_all(
		"Has Role",
		filters={"role": AUDITOR_ROLE, "parenttype": "User"},
		pluck="parent",
	)


# ------------------------------------------------------------------ backfill


def backfill_candidates_count():
	return frappe.db.sql(
		"""select count(*) from `tabToDo`
		where ifnull(assigned_by, '') = '' and ifnull(owner, '') not in %(skip)s""",
		{"skip": SKIP_BACKFILL_OWNERS},
	)[0][0]


def backfill_assigned_by(reason="manual", dry_run=False, batch_size=1000):
	"""Record each ToDo's creator as ``assigned_by`` where it is empty.

	Frappe's participant filter covers ``allocated_to`` and ``assigned_by`` but
	not ``owner``. Only empty values change, in a single conditional UPDATE per
	batch, so a row someone assigns in the meantime is never overwritten.
	``modified`` is untouched and no document events or notifications fire.
	The snapshot records each row's owner so the restore can tell whether the
	value is still the one this backfill wrote.
	"""
	from todo_privacy import snapshot

	rows = frappe.db.sql(
		"""select name, assigned_by, owner from `tabToDo`
		where ifnull(assigned_by, '') = '' and ifnull(owner, '') not in %(skip)s
		order by name""",
		{"skip": SKIP_BACKFILL_OWNERS},
		as_dict=True,
	)
	if dry_run or not rows:
		return {"candidates": len(rows), "changed": 0, "snapshot": None}

	path = snapshot.write("backfill", {"reason": reason, "rows": rows})
	changed = 0
	names = [r.name for r in rows]
	for start in range(0, len(names), batch_size):
		chunk = names[start : start + batch_size]
		frappe.db.sql(
			"""update `tabToDo` set assigned_by = owner
			where name in %(names)s and ifnull(assigned_by, '') = ''""",
			{"names": chunk},
		)
		changed += frappe.db._cursor.rowcount
	_log("ToDo assigned_by backfilled", {"reason": reason, "changed": changed, "snapshot": path})
	return {"candidates": len(rows), "changed": changed, "snapshot": path}


# --------------------------------------------------- assignment comment text


def sanitised_assignment_text(content):
	from todo_privacy.redaction import strip_assignment_text

	return strip_assignment_text(content)


def sanitise_assignment_comments(reason="manual", dry_run=False):
	"""Remove ToDo descriptions from existing "Assigned" timeline comments.

	Frappe writes "<assigner> assigned <assignee>: <description>" on the
	referenced document. Anyone who can open that document, and every System
	Manager through the Comment list, could read the description. Snapshot
	first; ``snapshot.restore_comments`` puts the original text back.
	"""
	from todo_privacy import snapshot

	rows = frappe.db.sql(
		"""select name, content from `tabComment`
		where comment_type = 'Assigned' and content like %(pattern)s""",
		{"pattern": f"%{ASSIGNED_SPLIT}%"},
		as_dict=True,
	)
	if dry_run or not rows:
		return {"candidates": len(rows), "changed": 0, "snapshot": None}

	path = snapshot.write("comments", {"reason": reason, "rows": rows})
	changed = 0
	for row in rows:
		frappe.db.sql(
			"update `tabComment` set content=%s where name=%s and content=%s",
			(sanitised_assignment_text(row.content), row.name, row.content),
		)
		changed += frappe.db._cursor.rowcount
	_log("Assignment comments sanitised", {"reason": reason, "changed": changed, "snapshot": path})
	return {"candidates": len(rows), "changed": changed, "snapshot": path}


def assignment_comment_candidates_count():
	return frappe.db.sql(
		"select count(*) from `tabComment` where comment_type='Assigned' and content like %s",
		(f"%{ASSIGNED_SPLIT}%",),
	)[0][0]


# ------------------------------------------------------------------ status

OUR_HOOKS = {
	"ToDo": "todo_privacy.permissions.todo_has_permission",
	"Comment": "todo_privacy.permissions.comment_has_permission",
	"Version": "todo_privacy.permissions.version_has_permission",
	"Communication": "todo_privacy.permissions.communication_has_permission",
}


def hooks_active():
	"""For each guarded DocType: is our permission hook loaded and evaluated first?

	Frappe evaluates DocType hooks plus any "*" hooks in reverse order and
	takes the first non-None answer.
	"""
	hooks = frappe.get_hooks("has_permission", {}) or {}
	report = {}
	for doctype, ours in OUR_HOOKS.items():
		chain = list(hooks.get(doctype, []) or []) + list(hooks.get("*", []) or [])
		order = list(reversed(chain))
		report[doctype] = {
			"loaded": ours in chain,
			"evaluated_first": bool(order) and order[0] == ours,
			"evaluation_order": order,
		}
	return report


def overrides_active():
	"""Map each wrapped method and class to what Frappe will actually use.

	v14 picks the FIRST app's whitelisted-method override and the LAST app's
	DocType class override.
	"""
	from todo_privacy import hooks as app_hooks

	resolved = {}
	for original, ours in app_hooks.override_whitelisted_methods.items():
		actual = frappe.override_whitelisted_method(original)
		resolved[original] = {"resolves_to": actual, "ours": actual == ours}
	class_hooks = frappe.get_hooks("override_doctype_class", {}) or {}
	for doctype, ours in app_hooks.override_doctype_class.items():
		actual = (class_hooks.get(doctype) or [None])[-1]
		resolved[f"class:{doctype}"] = {"resolves_to": actual, "ours": actual == ours}
	return resolved


def status():
	"""Read-only summary: ``bench --site X execute todo_privacy.policy.status``."""
	return {
		"baseline_problems": check(),
		"auditor_holders": auditor_holders(),
		"backfill_candidates": backfill_candidates_count(),
		"assignment_comment_candidates": assignment_comment_candidates_count(),
		"hooks_active": hooks_active(),
		"overrides_active": overrides_active(),
		"custom_rows": [_shape(r) for r in _custom_rows()],
	}


def _log(title, data):
	try:
		frappe.log_error(title=title, message=json.dumps(data, indent=1, default=str))
	except Exception:
		pass

