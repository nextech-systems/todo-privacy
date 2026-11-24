"""Document event guards for ToDo privacy."""

import frappe
from frappe import _
from frappe.utils import get_fullname

TODO = "ToDo"


def _system_context():
	flags = frappe.flags
	return bool(flags.in_install or flags.in_migrate or flags.in_patch or flags.in_import)


def todo_before_insert(doc, method=None):
	"""Record the creator as ``assigned_by`` when nothing set it.

	Covers inserts that skip the permission check (``ignore_permissions``),
	where the permission hook never runs.
	"""
	if not doc.get("assigned_by"):
		creator = doc.get("owner") or frappe.session.user
		if creator and creator != "Guest":
			doc.assigned_by = creator


def todo_validate(doc, method=None):
	"""Runs after Frappe's own ToDo.validate.

	1. ``assigned_by`` cannot be changed once stored, except by Administrator
	   or by trusted server code (``ignore_permissions``). Stops a participant
	   from forging who assigned a ToDo.
	2. The "Assigned" timeline comment Frappe writes on the referenced document
	   normally includes the ToDo description. Anyone who can open that document
	   (and every System Manager, through the Comment list) could read it.
	   The comment keeps who assigned whom, without the description.
	"""
	if not doc.is_new() and not doc.flags.ignore_permissions and not _system_context():
		if frappe.session.user != "Administrator":
			stored = frappe.db.get_value(TODO, doc.name, "assigned_by")
			if stored and doc.get("assigned_by") != stored:
				frappe.throw(_("The person who assigned a ToDo cannot be changed."), frappe.PermissionError)

	assignment = getattr(doc, "_assignment", None)
	if assignment and assignment.get("comment_type") == "Assigned":
		if doc.assigned_by == doc.allocated_to:
			assignment["text"] = _("{0} self assigned this task").format(get_fullname(doc.assigned_by))
		else:
			assignment["text"] = _("{0} assigned {1}").format(
				get_fullname(doc.assigned_by), get_fullname(doc.allocated_to)
			)


def block_todo_share(doc, method=None):
	"""Refuse any DocShare that would expose ToDo content, for everyone including Administrator.

	Covers shares on a ToDo and on records linked to one (comments, change
	history, emails, attachments; see linked.py), for named users and for
	"everyone". A share overrides permission denials in Frappe, so these must
	never exist. Runs on insert and on every save, so an existing share cannot
	be repointed at such a record either. Administrator already sees every ToDo.
	"""
	from todo_privacy.linked import is_todo_content

	if is_todo_content(doc.get("share_doctype"), doc.get("share_name")):
		frappe.throw(
			_("ToDos and their comments, history, emails and attachments cannot be shared."),
			frappe.PermissionError,
		)


def block_linking_shared_record(doc, method=None):
	"""A shared Comment, Version, Communication or File cannot be moved onto a ToDo,
	and a shared Communication cannot gain a ToDo timeline link.

	Otherwise a record shared while it belonged to some other document would
	keep its share after being attached to a ToDo.
	"""
	from todo_privacy.linked import submitted_todo_refs

	# primary reference or (Communication) a pending timeline link to a ToDo
	if doc.is_new() or not submitted_todo_refs(doc):
		return
	if frappe.db.exists("DocShare", {"share_doctype": doc.doctype, "share_name": doc.name}):
		frappe.throw(
			_("This record is shared, so it cannot be attached to a ToDo. Remove the shares first."),
			frappe.PermissionError,
		)


def strip_assigned_comment(doc, method=None):
	"""Every "Assigned" timeline comment is stored without the ToDo description.

	Frappe writes these on the referenced document, and System Managers can
	list every Comment. This catches any code path that writes one, not only
	ToDo's own; existing ones are cleaned on install, migrate and daily.
	"""
	if doc.get("comment_type") == "Assigned":
		from todo_privacy.redaction import strip_assignment_text

		doc.content = strip_assignment_text(doc.get("content"))
