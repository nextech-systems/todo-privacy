"""One place that decides what ToDo text a user may see in responses about other records.

Every wrapper in overrides.py, the report and the Email Digest go through
these functions; nothing redacts on its own.

Rule: ToDo free text (the description, and the copy of it Frappe writes into
"Assigned" timeline comments) is shown only to that ToDo's assignee and
assigner, plus Administrator and ToDo Auditors. Everyone else still sees the
structure: who it is assigned to, by whom, status, date, priority and
reference. That keeps assignment workflows usable without disclosing content.
"""

import frappe
from frappe import _

TODO = "ToDo"
ASSIGNED_SPLIT = ": "
HIDDEN = None


def hidden_note():
	return _("Details are visible to the assignee and the person who assigned it.")


def sees_all(user):
	from todo_privacy.permissions import is_auditor

	return user == "Administrator" or is_auditor(user)


def participant_todos(todo_names, user):
	"""Names, out of ``todo_names``, where ``user`` is the stored assignee or assigner."""
	todo_names = {n for n in todo_names if n}
	if not todo_names:
		return set()
	if sees_all(user):
		return set(todo_names)
	rows = frappe.get_all(TODO, filters={"name": ("in", list(todo_names))}, fields=["name", "allocated_to", "assigned_by"])
	return {r.name for r in rows if user in (r.allocated_to, r.assigned_by)}


def strip_assignment_text(content):
	"""Keep "X assigned Y"; drop the ToDo description Frappe appends after ": "."""
	if not content or ASSIGNED_SPLIT not in content:
		return content
	return content.split(ASSIGNED_SPLIT, 1)[0]


def redact_todo_rows(rows, user, field="description", replacement=HIDDEN):
	"""In a list of dicts keyed by ToDo ``name``, hide ``field`` where ``user`` is not a participant."""
	rows = rows or []
	allowed = participant_todos({r.get("name") for r in rows}, user)
	for row in rows:
		if row.get("name") not in allowed and field in row:
			row[field] = replacement
	return rows


def filter_communications(rows, user):
	"""Drop Communications linked to a ToDo (primary or timeline link) the user is not part of.

	Frappe loads a document's timeline Communications with raw SQL, matching
	the primary reference or any timeline link, so a Communication filed on a
	public record but also linked to a private ToDo would ride along.
	"""
	rows = rows or []
	if sees_all(user) or not rows:
		return rows
	from todo_privacy.permissions import is_participant

	names = [r.get("name") for r in rows if r.get("name")]
	refs = {}
	for row in rows:
		if row.get("reference_doctype") == TODO and row.get("reference_name"):
			refs.setdefault(row.get("name"), set()).add(row.get("reference_name"))
	if names:
		for link in frappe.get_all(
			"Communication Link",
			filters={"parenttype": "Communication", "parent": ("in", names), "link_doctype": TODO},
			fields=["parent", "link_name"],
		):
			refs.setdefault(link.parent, set()).add(link.link_name)
	hidden = {
		name for name, todos in refs.items() if not all(is_participant(todo, user) for todo in todos)
	}
	return [r for r in rows if r.get("name") not in hidden]


TIMELINE_PAGE = 21  # Frappe loads 21 and shows "Load More" when more than 20 arrive


def _with_attachments(rows):
	# what frappe.desk.form.load._get_communications adds to each row
	import json

	for c in rows:
		if c.communication_type in ("Communication", "Automated Message"):
			c.attachments = json.dumps(
				frappe.get_all(
					"File",
					fields=["file_url", "is_private"],
					filters={"attached_to_doctype": "Communication", "attached_to_name": c.name},
				)
			)
	return rows


def visible_communications(doctype, name, start=0, limit=TIMELINE_PAGE, user=None):
	"""A page of a document's timeline Communications, counted in VISIBLE rows.

	Hiding rows after Frappe has paged them would leave short or empty pages,
	and the timeline only offers "Load More" when a page is full, so ordinary
	emails behind hidden ones became unreachable. This pages through the raw
	list and skips/collects only rows the user may see, so ``start`` and
	``limit`` mean the same thing the browser thinks they mean.
	"""
	from frappe.desk.form.load import get_communication_data

	user = user or frappe.session.user
	start, limit = int(start or 0), int(limit or TIMELINE_PAGE)
	if sees_all(user):
		return _with_attachments(get_communication_data(doctype, name, start, limit))

	batch = max(limit * 2, 50)
	raw_start, skipped, out = 0, 0, []
	while len(out) < limit:
		rows = get_communication_data(doctype, name, raw_start, batch)
		if not rows:
			break
		for row in filter_communications(rows, user):
			if skipped < start:
				skipped += 1
				continue
			out.append(row)
			if len(out) == limit:
				break
		if len(rows) < batch:
			break
		raw_start += batch
	return _with_attachments(out)


def redact_docinfo(docinfo, user):
	"""Form sidebar and timeline payload (``frappe.response.docinfo``) for any document."""
	if not docinfo or user == "Administrator":
		return docinfo
	initial = (docinfo.get("communications") or []) + (docinfo.get("automated_messages") or [])
	if initial and len(filter_communications(initial, user)) < len(initial):
		# Something was hidden: rebuild the first page from visible rows only.
		page = visible_communications(docinfo.get("doctype"), docinfo.get("name"), 0, TIMELINE_PAGE, user)
		docinfo["automated_messages"] = [m for m in page if m.get("communication_type") == "Automated Message"]
		docinfo["communications"] = [m for m in page if m.get("communication_type") != "Automated Message"]
	redact_todo_rows(docinfo.get("assignments"), user)
	for log in docinfo.get("assignment_logs") or []:
		if log.get("comment_type") == "Assigned":
			log["content"] = strip_assignment_text(log.get("content"))
	return docinfo


def redact_activities(data, user):
	"""ERPNext CRM activity panel payload."""
	if data and user != "Administrator":
		redact_todo_rows(data.get("tasks"), user, replacement=hidden_note())
	return data
