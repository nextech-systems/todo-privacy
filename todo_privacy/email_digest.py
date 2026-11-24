"""Email Digest override: list each recipient's own ToDos."""

import frappe
from frappe.utils import get_url_to_form

from erpnext.setup.doctype.email_digest.email_digest import EmailDigest as _EmailDigest


class EmailDigest(_EmailDigest):
	"""ERPNext's digest lists ToDos by ``owner`` or ``assigned_by`` and so
	leaves out ToDos assigned to the recipient by someone else. List the
	recipient's own ToDos: assigned to them or by them."""

	def get_todo_list(self, user_id=None):
		user_id = user_id or frappe.session.user
		todo_list = frappe.db.sql(
			"""select * from `tabToDo`
			where (allocated_to=%(u)s or assigned_by=%(u)s) and status='Open'
			order by field(priority, 'High', 'Medium', 'Low') asc, date asc limit 20""",
			{"u": user_id},
			as_dict=True,
		)
		for t in todo_list:
			t.link = get_url_to_form("ToDo", t.name)
		return todo_list

	def get_todo_count(self, user_id=None):
		user_id = user_id or frappe.session.user
		return frappe.db.sql(
			"""select count(*) from `tabToDo`
			where status='Open' and (allocated_to=%(u)s or assigned_by=%(u)s)""",
			{"u": user_id},
		)[0][0]
