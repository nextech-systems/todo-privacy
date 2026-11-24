"""Full-row snapshots and exact restoration for everything this app changes.

What can change, and the snapshot that undoes it:

| Change | Snapshot kind | Restore |
| --- | --- | --- |
| ToDo Custom DocPerm rows, DocShares on ToDos and on their linked records (linked.py), the ToDo Auditor role | permissions | restore_permissions |
| tabToDo.assigned_by filled where empty | backfill | restore_backfill |
| "Assigned" comment text with the description removed | comments | restore_comments |

Snapshots are JSON files in ``<site>/private/todo_privacy/`` (mode 600). That
folder is not under ``private/files`` and is never served over HTTP. A
permissions snapshot holds every column of every Custom DocPerm, DocPerm and
DocShare row for ToDo, in table order, so restoration is checked for exact
equality.

Restores change a row only if it still holds the value this app wrote, in a
single conditional UPDATE, so later edits by people are never overwritten.
"""

import hashlib
import json
import os

import frappe
from frappe.utils import now_datetime

from todo_privacy import linked

TODO = "ToDo"
# The share scope a snapshot covers is recorded in it (see linked.py):
# none (before 1.2.2) = direct ToDo shares; "todo-and-linked" (1.2.2);
# "todo-and-linked-v2" (1.2.3+). Each is compared and restored in its own scope.
SHARE_SCOPE = linked.CURRENT_SCOPE


def _snapshot_scope(snap):
	scope = snap.get("share_scope") or linked.SCOPE_DIRECT
	if scope not in linked.KNOWN_SCOPES:
		frappe.throw(f"Snapshot has an unknown share scope {scope!r}; nothing was changed")
	return scope


def _require_admin():
	if frappe.session.user != "Administrator":
		frappe.only_for("System Manager")


def _dir():
	path = frappe.get_site_path("private", "todo_privacy")
	os.makedirs(path, exist_ok=True)
	return path


def _versions():
	from todo_privacy import __version__

	return {"app_version": __version__, "frappe_version": frappe.__version__}


def _digest(payload):
	return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()


def write(kind, data):
	"""Write a snapshot with provenance: site, time, versions and a checksum of its rows."""
	stamp = now_datetime().strftime("%Y%m%dT%H%M%S%f")
	path = os.path.join(_dir(), f"{kind}-{stamp}.json")
	body = json.loads(json.dumps(data, default=str))
	snap = {
		"kind": kind,
		"site": frappe.local.site,
		"taken": stamp,
		"server_time": str(now_datetime()),
		**_versions(),
		"sha256": _digest(body),
		**body,
	}
	with open(path, "w") as f:
		json.dump(snap, f, indent=1, default=str)
	os.chmod(path, 0o600)
	return path


def _read(path, kind, force=False):
	"""Load a snapshot, refusing one that is damaged, from another site, or (unless force) another version."""
	base = os.path.realpath(_dir())
	real = os.path.realpath(path)
	if os.path.dirname(real) != base:
		frappe.throw(f"Snapshot must be inside {base}")
	with open(real) as f:
		snap = json.load(f)
	if snap.get("kind") != kind:
		frappe.throw(f"Not a {kind} snapshot")
	if snap.get("site") != frappe.local.site:
		frappe.throw(f"Snapshot was taken on site {snap.get('site')}, not {frappe.local.site}")
	meta = {"kind", "site", "taken", "server_time", "app_version", "frappe_version", "sha256"}
	if "sha256" not in snap:
		# Only snapshots written before 1.2.0 lack a checksum; anything else has been altered.
		if snap.get("app_version") or not force:
			frappe.throw("Snapshot has no checksum. Pre-1.2.0 snapshots need force=1 after manual review.")
	elif _digest({k: v for k, v in snap.items() if k not in meta}) != snap["sha256"]:
		frappe.throw("Snapshot checksum does not match its contents; it has been altered or damaged")
	live = _versions()
	mismatch = [k for k in live if snap.get(k) and snap[k] != live[k]]
	if mismatch and not force:
		frappe.throw(
			"Snapshot was taken on a different version ("
			+ ", ".join(f"{k} {snap[k]} vs {live[k]}" for k in mismatch)
			+ "). Re-run with force=1 if you have checked this is safe."
		)
	return snap


def list_snapshots():
	return sorted(os.listdir(_dir()))


def _rows(table, where_field):
	return frappe.db.sql(
		f"select * from `tab{table}` where `{where_field}`=%s order by idx asc, creation asc, name asc",
		(TODO,),
		as_dict=True,
	)


def _state():
	from todo_privacy.policy import AUDITOR_ROLE

	return {
		"custom_docperm": _rows("Custom DocPerm", "parent"),
		"docperm": _rows("DocPerm", "parent"),
		"docshare": linked.scoped_shares(),
		"share_scope": SHARE_SCOPE,
		"auditor_role_exists": bool(frappe.db.exists("Role", AUDITOR_ROLE)),
	}


def _normalise(value):
	return json.loads(json.dumps(value, default=str, sort_keys=True))


def take_permissions(reason="manual"):
	"""Snapshot every ToDo DocPerm and Custom DocPerm row, and every in-scope DocShare row. Returns the file path."""
	_require_admin()
	return write("permissions", {"reason": reason, **_state()})


def compare_permissions(path, force=False):
	"""Differences between the snapshot and the live rows (empty list = identical)."""
	snap = _read(path, "permissions", force=force)
	live = _normalise(_state())
	live["docshare"] = _normalise(linked.scoped_shares(_snapshot_scope(snap)))
	diffs = []
	for key, label in (
		("custom_docperm", "Custom DocPerm rows"),
		("docperm", "standard DocPerm rows (this app never writes them)"),
		("docshare", "ToDo DocShare rows"),
		("auditor_role_exists", "ToDo Auditor role presence"),
	):
		default = False if key == "auditor_role_exists" else []
		if live[key] != snap.get(key, default):
			diffs.append(f"{label} differ")
	return diffs


def restore_permissions(path, force=False):
	"""Put ToDo permissions, in-scope shares and the Auditor role back exactly as snapshotted.

	This re-opens ToDos to the roles in the snapshot. The app re-applies its
	baseline on migrate and daily, so uninstall it in the same sitting if the
	rollback is meant to stick (see README, Rollback).
	"""
	from todo_privacy.policy import AUDITOR_ROLE, _clear_caches

	_require_admin()
	snap = _read(path, "permissions", force=force)
	scope = _snapshot_scope(snap)  # refuse unknown scopes before any write

	frappe.db.delete("Custom DocPerm", {"parent": TODO})
	for row in snap["custom_docperm"]:
		frappe.get_doc({"doctype": "Custom DocPerm", **row}).db_insert()

	# only the scope this snapshot covers: shares it never recorded are left alone
	linked.delete_scoped_shares(scope)
	for row in snap.get("docshare", []):
		frappe.get_doc({"doctype": "DocShare", **row}).db_insert()

	if not snap.get("auditor_role_exists") and frappe.db.exists("Role", AUDITOR_ROLE):
		if frappe.db.count("Has Role", {"role": AUDITOR_ROLE}):
			frappe.db.rollback()
			frappe.throw(f"{AUDITOR_ROLE} has been granted to users; remove those grants first")
		frappe.db.delete("Role", {"name": AUDITOR_ROLE})

	_clear_caches()
	diffs = compare_permissions(path, force=force)
	if diffs:
		frappe.db.rollback()
		frappe.throw("Restore did not match the snapshot; rolled back: " + "; ".join(diffs))
	frappe.db.commit()
	return {"restored_custom_rows": len(snap["custom_docperm"]), "verified_identical": True}


def restore_backfill(path, force=False):
	"""Set ``assigned_by`` back to its previous value where it still holds the backfilled value.

	The backfill wrote ``assigned_by = owner``. A row is restored only if its
	``assigned_by`` and ``owner`` still equal the owner recorded at backfill
	time; anything changed since is reported and left alone.
	"""
	_require_admin()
	snap = _read(path, "backfill", force=force)
	restored, skipped = 0, []
	for row in snap["rows"]:
		frappe.db.sql(
			"""update `tabToDo` set assigned_by=%(previous)s
			where name=%(name)s and assigned_by=%(owner)s and owner=%(owner)s""",
			{"previous": row["assigned_by"], "name": row["name"], "owner": row["owner"]},
		)
		if frappe.db._cursor.rowcount:
			restored += 1
		else:
			skipped.append(row["name"])
	frappe.db.commit()
	return {"restored": restored, "skipped": len(skipped), "skipped_names": skipped[:50]}


def restore_comments(path, force=False):
	"""Put the original "Assigned" comment text back where it is still the sanitised text."""
	from todo_privacy.policy import sanitised_assignment_text

	_require_admin()
	snap = _read(path, "comments", force=force)
	restored, skipped = 0, 0
	for row in snap["rows"]:
		frappe.db.sql(
			"update `tabComment` set content=%s where name=%s and content=%s",
			(row["content"], row["name"], sanitised_assignment_text(row["content"])),
		)
		if frappe.db._cursor.rowcount:
			restored += 1
		else:
			skipped += 1
	frappe.db.commit()
	return {"restored": restored, "skipped": skipped}
