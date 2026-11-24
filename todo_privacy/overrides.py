"""Wrappers for whitelisted methods that read ToDos without ToDo permissions.

Each wrapper calls the original method and then narrows what goes back to the
browser. None of Frappe or ERPNext is edited.

v14 resolves ``override_whitelisted_methods`` to the FIRST app that overrides a
method (v15 switches to the LAST). Staging must confirm no other installed app
overrides the same methods; see README.

What is hidden, and from whom, is decided in redaction.py.
"""

import frappe
from frappe import _

from todo_privacy import redaction


def _redact_response_docinfo():
	docinfo = frappe.response.get("docinfo") if hasattr(frappe, "response") else None
	redaction.redact_docinfo(docinfo, frappe.session.user)


@frappe.whitelist()
def getdoc(doctype, name, user=None):
	from frappe.desk.form import load

	result = load.getdoc(doctype, name, user)
	_redact_response_docinfo()
	return result


@frappe.whitelist()
def get_docinfo(doc=None, doctype=None, name=None):
	from frappe.desk.form import load

	result = load.get_docinfo(doc=doc, doctype=doctype, name=name)
	_redact_response_docinfo()
	return result


@frappe.whitelist()
def get_communications(doctype, name, start=0, limit=20):
	"""Timeline paging of Communications on a document, without ToDo-linked ones the user is not part of.

	Same permission check as Frappe's original; ``start`` counts visible rows.
	"""
	doc = frappe.get_doc(doctype, name)
	if not doc.has_permission("read"):
		raise frappe.PermissionError
	return redaction.visible_communications(doctype, name, start, limit, frappe.session.user)


@frappe.whitelist()
def get_group_by_count(doctype: str, current_filters: str, field: str) -> list[dict]:
	"""Sidebar "Assigned To" counts, limited to records the user can read.

	Upstream counts ToDos for every record matching the filters, ignoring the
	user's permissions on that DocType and without filtering on
	``reference_type``. Other fields pass straight through.
	"""
	from frappe.desk import listview

	if field != "assigned_to":
		return listview.get_group_by_count(doctype, current_filters, field)

	filters = frappe.parse_json(current_filters)
	readable = frappe.get_list(
		doctype,
		filters=filters,
		fields=[f"`tab{doctype}`.`name`"],
		order_by=None,
		limit_page_length=0,
		run=0,
	)
	# The subquery is already escaped by Frappe; double any % (e.g. from a
	# "like" filter) so it survives the parameter substitution below.
	readable = readable.replace("%", "%%")
	return frappe.db.sql(
		f"""select t.allocated_to as name, count(*) as count
		from `tabToDo` t
		inner join `tabUser` u on u.name = t.allocated_to
		where t.status != 'Cancelled'
			and u.user_type = 'System User'
			and t.reference_type = %(doctype)s
			and t.reference_name in ({readable})
		group by t.allocated_to
		order by count desc
		limit 50""",
		{"doctype": doctype},
		as_dict=True,
	)


@frappe.whitelist()
def get_open_activities(ref_doctype, ref_docname):
	"""ERPNext CRM activity panel, with a read check on the record and redacted ToDo text.

	Upstream returns open ToDo descriptions for any record name, with no check
	that the caller can read that record.
	"""
	from erpnext.crm.utils import get_open_activities as original

	if not frappe.has_permission(ref_doctype, "read", ref_docname):
		raise frappe.PermissionError(_("Not permitted"))

	data = original(ref_doctype, ref_docname)
	redaction.redact_activities(data, frappe.session.user)
	return data


def _refuse_if_todo(doctype):
	"""ToDo permissions are owned by this app: refuse the change before it happens.

	Applying a change and then reverting it would leave a window (and, if
	another run held the lock, a committed change until the next run).
	"""
	if doctype == "ToDo":
		frappe.throw(
			_(
				"ToDo permissions are managed by the ToDo Privacy app and cannot be changed here."
				" Grant the ToDo Auditor role for read-only access to all ToDos."
			),
			frappe.PermissionError,
		)


@frappe.whitelist()
def permission_manager_add(parent, role, permlevel):
	from frappe.core.page.permission_manager import permission_manager as pm

	_refuse_if_todo(parent)
	return pm.add(parent, role, permlevel)


@frappe.whitelist()
def permission_manager_update(doctype, role, permlevel, ptype, value=None, if_owner=0):
	from frappe.core.page.permission_manager import permission_manager as pm

	_refuse_if_todo(doctype)
	return pm.update(doctype, role, permlevel, ptype, value=value, if_owner=if_owner)


@frappe.whitelist()
def permission_manager_remove(doctype, role, permlevel, if_owner=0):
	from frappe.core.page.permission_manager import permission_manager as pm

	_refuse_if_todo(doctype)
	return pm.remove(doctype, role, permlevel, if_owner=if_owner)


@frappe.whitelist()
def permission_manager_reset(doctype):
	from frappe.core.page.permission_manager import permission_manager as pm

	_refuse_if_todo(doctype)
	return pm.reset(doctype)
