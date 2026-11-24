"""A Custom Field on ToDo is protected by the re-link rule like any standard field.

This test adds a real Custom Field (schema change, which commits) and removes
it afterwards, including its database column. Staging only.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

DOMAIN = "tp-cf.example.invalid"
FIELD = "tp_relink_probe"


def _user(key):
	email = f"{key}@{DOMAIN}"
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": key, "send_welcome_email": 0, "user_type": "System User"}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
	return email


class TestCustomFieldRelink(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": FIELD}):
			frappe.get_doc(
				{"doctype": "Custom Field", "dt": "ToDo", "fieldname": FIELD, "label": "TP Relink Probe", "fieldtype": "Data", "insert_after": "description"}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.clear_cache(doctype="ToDo")
		super().setUpClass()
		cls.addClassCleanup(cls._drop_field)
		cls.outsider = _user("outsider")
		cls.assignee = _user("assignee")
		cls.assigner = _user("assigner")

	@staticmethod
	def _drop_field():
		frappe.set_user("Administrator")
		frappe.db.rollback()  # test rows must not be committed by the DDL below
		name = frappe.db.get_value("Custom Field", {"dt": "ToDo", "fieldname": FIELD})
		if name:
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
		if frappe.db.has_column("ToDo", FIELD):
			frappe.db.sql_ddl(f"alter table `tabToDo` drop column `{FIELD}`")
		frappe.db.commit()
		frappe.clear_cache(doctype="ToDo")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _setup(self):
		frappe.set_user(self.outsider)
		a = frappe.get_doc({"doctype": "Note", "title": "tp cf " + frappe.generate_hash(length=8), "public": 1}).insert()
		b = frappe.get_doc({"doctype": "Note", "title": "tp cf " + frappe.generate_hash(length=8), "public": 1}).insert()
		frappe.set_user("Administrator")
		todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "x", "allocated_to": self.assignee, "assigned_by": self.assigner,
			 "reference_type": "Note", "reference_name": a.name, FIELD: "original"}
		).insert(ignore_permissions=True)
		return todo, a, b

	def test_relink_plus_custom_field_change_is_refused(self):
		todo, a, b = self._setup()
		frappe.set_user(self.outsider)
		doc = frappe.get_doc("ToDo", todo.name)
		doc.reference_name = b.name
		doc.set(FIELD, "changed")
		self.assertRaises(frappe.PermissionError, doc.save)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, [FIELD, "reference_name"]), ("original", a.name))

	def test_relink_alone_still_works_with_the_custom_field_present(self):
		todo, _a, b = self._setup()
		frappe.set_user(self.outsider)
		doc = frappe.get_doc("ToDo", todo.name)
		doc.reference_name = b.name
		doc.save()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, [FIELD, "reference_name"]), ("original", b.name))
