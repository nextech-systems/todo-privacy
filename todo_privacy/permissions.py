"""Permission hooks for ToDo, and for the Comment and Version records that copy ToDo text.

Every decision about an existing record is made from the row stored in the
database, never from the document the caller submitted. A caller can post any
field values (for example ``assigned_by`` set to themselves) through
``frappe.client.save`` or ``savedocs``; those values are ignored here.

Frappe evaluates ``has_permission`` hooks in reverse registration order and
uses the first result that is not None. This app is installed after Frappe, so
its ToDo hook runs before Frappe's own ``todo.has_permission``.

ToDo rule, for anyone except Administrator:
- Existing ToDo: the stored ``allocated_to`` or ``assigned_by`` user may read,
  write and delete it. A ToDo Auditor may read it. Anyone else may only re-link
  it to a new reference document (ERPNext Lead -> Opportunity), and only when
  nothing else changes. Everything else is refused (False, not None), so the
  decision cannot fall through to role permissions.
- New ToDo: the creator is always recorded as ``assigned_by``.
"""

import frappe
from frappe.model import no_value_fields
from frappe.utils import cint, cstr, flt, get_datetime, getdate

TODO = "ToDo"
AUDITOR_ROLE = "ToDo Auditor"
READ_PTYPES = frozenset({"read", "select", "report", "print", "email", "export"})
RELINK_FIELDS = frozenset({"reference_type", "reference_name"})
# standard columns that are not in meta.fields but must not change on a re-link
AUDIT_FIELDS = ("owner", "docstatus")
NUMERIC = frozenset({"Int", "Check", "Float", "Currency", "Percent", "Rating", "Duration"})


def is_auditor(user):
	return AUDITOR_ROLE in frappe.get_roles(user)


def participants(todo_name):
	"""Stored (allocated_to, assigned_by) for a ToDo, or None if it does not exist."""
	row = frappe.db.get_value(TODO, todo_name, ["allocated_to", "assigned_by"], as_dict=True)
	return (row.allocated_to, row.assigned_by) if row else None


def is_participant(todo_name, user):
	stored = participants(todo_name)
	return bool(stored) and user in stored


def todo_has_permission(doc, ptype="read", user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return None

	stored = participants(doc.name) if doc.get("name") else None
	if stored is None:
		# New record. Frappe checks "create" before before_insert runs.
		if ptype == "create":
			doc.assigned_by = user
			return True
		return None

	if user in stored:
		return True
	if ptype in READ_PTYPES and is_auditor(user):
		return None  # the Auditor role row grants read; Frappe's own check confirms it
	if ptype == "write" and _relink_only(doc, user):
		return True
	return False


# ---------------------------------------------------------------- re-link


def _norm(df, value):
	if df.fieldtype in NUMERIC:
		return flt(value) if df.fieldtype not in ("Int", "Check") else cint(value)
	if value in (None, ""):
		return None
	if df.fieldtype == "Date":
		return str(getdate(value))
	if df.fieldtype == "Datetime":
		return str(get_datetime(value))
	return cstr(value)


def _changed_fields(doc):
	"""Every business field whose submitted value differs from the stored row.

	Covers all fields in the ToDo meta, including Custom Fields added later.
	Fields fetched from another record (fetch_from) and virtual fields are
	derived, so they are skipped.
	"""
	stored = frappe.db.get_value(TODO, doc.name, "*", as_dict=True)
	if not stored:
		return None
	changed = set()
	for df in frappe.get_meta(TODO).fields:
		if df.fieldtype in no_value_fields or df.get("fetch_from") or df.get("is_virtual"):
			continue
		if _norm(df, doc.get(df.fieldname)) != _norm(df, stored.get(df.fieldname)):
			changed.add(df.fieldname)
	for field in AUDIT_FIELDS:
		if cstr(doc.get(field)) != cstr(stored.get(field)):
			changed.add(field)
	return changed, stored


def _relink_only(doc, user):
	result = _changed_fields(doc)
	if not result:
		return False
	changed, stored = result
	if not changed or changed - RELINK_FIELDS:
		return False
	if not (stored.reference_type and stored.reference_name and doc.reference_type and doc.reference_name):
		return False
	can_read_old = frappe.has_permission(
		stored.reference_type, "read", doc=stored.reference_name, user=user, throw=False
	)
	can_write_new = frappe.has_permission(doc.reference_type, "write", doc=doc.reference_name, user=user, throw=False)
	return bool(can_read_old and can_write_new)


# ----------------------------------------------- Comment and Version records


def _hides_todo_rows(user):
	return user != "Administrator" and not is_auditor(user)


def _participant_subquery(user):
	u = frappe.db.escape(user)
	return f"(select `name` from `tabToDo` where `allocated_to` = {u} or `assigned_by` = {u})"


def comment_query_conditions(user=None):
	user = user or frappe.session.user
	if not _hides_todo_rows(user):
		return ""
	return (
		"(ifnull(`tabComment`.`reference_doctype`, '') != 'ToDo'"
		f" or `tabComment`.`reference_name` in {_participant_subquery(user)})"
	)


def version_query_conditions(user=None):
	user = user or frappe.session.user
	if not _hides_todo_rows(user):
		return ""
	return (
		"(ifnull(`tabVersion`.`ref_doctype`, '') != 'ToDo'"
		f" or `tabVersion`.`docname` in {_participant_subquery(user)})"
	)


def _todo_refs(doc, doctype_field, name_field):
	"""ToDo names this record points at, both as stored and as submitted.

	Decisions use the stored row, never only the submitted document: a caller
	could otherwise re-point a ToDo comment at a public record and read it.
	The submitted value also counts, so nobody can attach a record to a ToDo
	they are not part of.
	"""
	refs = []
	if doc.get("name"):
		stored = frappe.db.get_value(doc.doctype, doc.name, [doctype_field, name_field], as_dict=True)
		if stored and stored.get(doctype_field) == TODO:
			refs.append(stored.get(name_field))
	if doc.get(doctype_field) == TODO:
		refs.append(doc.get(name_field))
	return refs


def _decide(doc, user, doctype_field, name_field):
	user = user or frappe.session.user
	if not _hides_todo_rows(user):
		return None
	refs = _todo_refs(doc, doctype_field, name_field)
	if not refs:
		return None
	return None if all(is_participant(ref, user) for ref in refs) else False


def comment_has_permission(doc, ptype="read", user=None):
	return _decide(doc, user, "reference_doctype", "reference_name")


def version_has_permission(doc, ptype="read", user=None):
	return _decide(doc, user, "ref_doctype", "docname")


# ------------------------------------------------------------ Communication
# Frappe's own Communication check only asks whether the user can read the
# PRIMARY reference, and Communication lists are open to the "All" role for
# non-email records and to System Manager for everything. A Communication
# linked to a ToDo, as primary reference or as a secondary timeline link,
# follows the participant rule for every link.


def communication_query_conditions(user=None):
	user = user or frappe.session.user
	if not _hides_todo_rows(user):
		return ""
	mine = _participant_subquery(user)
	return (
		"((ifnull(`tabCommunication`.`reference_doctype`, '') != 'ToDo'"
		f" or `tabCommunication`.`reference_name` in {mine})"
		" and not exists (select 1 from `tabCommunication Link` `tp_cl`"
		" where `tp_cl`.`parent` = `tabCommunication`.`name`"
		" and `tp_cl`.`parenttype` = 'Communication'"
		" and `tp_cl`.`link_doctype` = 'ToDo'"
		f" and `tp_cl`.`link_name` not in {mine}))"
	)


def communication_has_permission(doc, ptype="read", user=None):
	from todo_privacy.linked import stored_todo_refs, submitted_todo_refs

	user = user or frappe.session.user
	if not _hides_todo_rows(user):
		return None
	refs = submitted_todo_refs(doc)
	if doc.get("name") and not doc.is_new():
		refs += stored_todo_refs("Communication", doc.name)
	if not refs:
		return None
	return None if all(is_participant(ref, user) for ref in refs) else False
