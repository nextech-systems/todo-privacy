"""Open ToDos assigned to me or by me.

Frappe's standard "ToDo" report keeps only rows where the user is ``owner`` or
``assigned_by``, so ToDos someone else assigned to the user are missing. This
report lists both sides and, like everything else, only the user's own rows.
"""

import frappe
from frappe import _
from frappe.utils import getdate

PRIORITY = {"High": 3, "Medium": 2, "Low": 1}


def execute(filters=None):
	user = frappe.session.user
	rows = frappe.get_list(
		"ToDo",
		fields=["name", "priority", "date", "description", "allocated_to", "assigned_by", "reference_type", "reference_name"],
		filters={"status": "Open"},
		or_filters={"allocated_to": user, "assigned_by": user},
		limit_page_length=0,
	)
	rows.sort(
		key=lambda r: (PRIORITY.get(r.priority, 0), getdate(r.date) if r.date else getdate("1900-01-01")),
		reverse=True,
	)
	columns = [
		{"label": _("ID"), "fieldname": "name", "fieldtype": "Link", "options": "ToDo", "width": 110},
		{"label": _("Priority"), "fieldname": "priority", "fieldtype": "Data", "width": 80},
		{"label": _("Date"), "fieldname": "date", "fieldtype": "Date", "width": 100},
		{"label": _("Description"), "fieldname": "description", "fieldtype": "Data", "width": 260},
		{"label": _("Assigned To"), "fieldname": "allocated_to", "fieldtype": "Link", "options": "User", "width": 160},
		{"label": _("Assigned By"), "fieldname": "assigned_by", "fieldtype": "Link", "options": "User", "width": 160},
		{"label": _("Reference Type"), "fieldname": "reference_type", "fieldtype": "Link", "options": "DocType", "width": 120},
		{
			"label": _("Reference"),
			"fieldname": "reference_name",
			"fieldtype": "Dynamic Link",
			"options": "reference_type",
			"width": 160,
		},
	]
	return columns, rows
