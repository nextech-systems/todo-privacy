"""Marker harness: ToDo text must not reach an outsider through any reader.

A unique marker is planted in a ToDo description, an old-style "Assigned"
timeline comment, a comment on the ToDo, and the ToDo's change history. Every
reader surface is then called as two outsiders (a System Manager and an
Employee) and the whole response is searched for the marker. A positive
control proves the harness detects the marker when access is legitimate.

Add any new reader surface to SURFACES.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

DOMAIN = "tp-marker.example.invalid"
MARKER = "zz-marker-c41f9e"


def _user(key, roles=()):
	email = f"{key}@{DOMAIN}"
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": key, "send_welcome_email": 0, "user_type": "System User"}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		existing = [r for r in roles if frappe.db.exists("Role", r)]
		if existing:
			user.add_roles(*existing)
	return email


class TestRedactionSurfaces(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.flags.mute_emails = True
		frappe.set_user("Administrator")
		cls.assignee = _user("assignee")
		cls.assigner = _user("assigner")
		cls.outsiders = {
			"System Manager": _user("out-sysmgr", ["System Manager"]),
			"Employee": _user("out-employee", ["Employee"]),
		}
		cls.note = frappe.get_doc({"doctype": "Note", "title": "marker note", "public": 1}).insert()
		cls.todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": MARKER,
				"allocated_to": cls.assignee,
				"assigned_by": cls.assigner,
				"reference_type": "Note",
				"reference_name": cls.note.name,
			}
		).insert(ignore_permissions=True)
		# An "Assigned" comment written before install, with the description in it.
		# Stored with raw SQL because the app now strips it on every write.
		legacy = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Assigned", "reference_doctype": "Note", "reference_name": cls.note.name,
			 "content": "placeholder"}
		).insert(ignore_permissions=True)
		cls.legacy_comment = legacy.name
		frappe.db.sql("update tabComment set content=%s where name=%s", (f"Someone assigned Someone: {MARKER}", legacy.name))
		# ...then cleaned the way install and migrate clean it
		with patch("todo_privacy.snapshot.write", return_value="/dev/null"):
			from todo_privacy.policy import sanitise_assignment_comments

			sanitise_assignment_comments(reason="test")
		frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "ToDo", "reference_name": cls.todo.name,
			 "content": MARKER}
		).insert(ignore_permissions=True)
		# Saves in test mode write no Version rows, so seed the history explicitly.
		cls.version = frappe.get_doc(
			{"doctype": "Version", "ref_doctype": "ToDo", "docname": cls.todo.name,
			 "data": json.dumps({"changed": [["description", "old", MARKER]]})}
		).insert(ignore_permissions=True)
		cls.todo_comment = frappe.get_value("Comment", {"reference_doctype": "ToDo", "reference_name": cls.todo.name})
		# Communications: one filed on the ToDo, one filed on the public Note with a ToDo timeline link
		cls.comm_names = []
		for reference, links in ((("ToDo", cls.todo.name), []), (("Note", cls.note.name), [cls.todo.name])):
			comm = frappe.get_doc(
				{"doctype": "Communication", "communication_type": "Communication", "communication_medium": "Other",
				 "subject": MARKER, "content": MARKER, "reference_doctype": reference[0], "reference_name": reference[1]}
			)
			for link in links:
				comm.append("timeline_links", {"link_doctype": "ToDo", "link_name": link})
			cls.comm_names.append(comm.insert(ignore_permissions=True).name)

	def tearDown(self):
		frappe.set_user("Administrator")

	def comment_names(self):
		return frappe.get_all(
			"Comment",
			filters=[["reference_name", "in", [self.todo.name, self.note.name]]],
			pluck="name",
		)

	# Each surface returns whatever the browser would receive.
	def surfaces(self):
		from frappe.client import get_list as client_get_list
		from frappe.desk import reportview
		from frappe.desk.search import search_link

		from todo_privacy import overrides
		from todo_privacy.todo_privacy.report.my_open_todos.my_open_todos import execute as my_todos

		note, todo = self.note.name, self.todo.name

		def with_response(fn):
			def run():
				frappe.local.response = frappe._dict(docs=[])
				result = fn()
				return {"result": result, "response": dict(frappe.local.response)}

			return run

		def form_dict(**kw):
			frappe.local.form_dict = frappe._dict(kw)

		def reportview_get():
			form_dict(doctype="ToDo", fields=json.dumps(["`tabToDo`.`name`", "`tabToDo`.`description`"]), filters="[]")
			return reportview.get()

		def sidebar_stats():
			return reportview.get_sidebar_stats(json.dumps(["_assign", "status"]), "ToDo", "[]")

		def standard_todo_report():
			from frappe.desk.query_report import run

			return run("ToDo")

		def digest():
			if "erpnext" not in frappe.get_installed_apps():
				return None
			return frappe.new_doc("Email Digest").get_todo_list(frappe.session.user)

		def activities():
			if "erpnext" not in frappe.get_installed_apps():
				return None
			return overrides.get_open_activities("Note", note)

		return {
			"form load of the referenced Note": with_response(lambda: overrides.getdoc("Note", note)),
			"docinfo of the referenced Note": with_response(lambda: overrides.get_docinfo(doctype="Note", name=note)),
			"CRM open activities": activities,
			"assigned-to counts": lambda: overrides.get_group_by_count("Note", json.dumps({"name": note}), "assigned_to"),
			"ToDo list with description": lambda: frappe.get_list("ToDo", fields=["name", "description"], limit_page_length=0),
			"client.get_list ToDo": lambda: client_get_list("ToDo", fields=["name", "description"], limit_page_length=0),
			# bounded to the synthetic rows: staging has ~569,000 Version rows
			"client.get_list Comment": lambda: client_get_list(
				"Comment", fields=["content"], filters={"name": ("in", self.comment_names())}, limit_page_length=0
			),
			"client.get_list Version": lambda: client_get_list(
				"Version", fields=["data"], filters={"name": ("in", [self.version.name])}, limit_page_length=0
			),
			"client.get_list Communication": lambda: client_get_list(
				"Communication", fields=["subject", "content"], filters={"name": ("in", self.comm_names)}, limit_page_length=0
			),
			"client.get primary Communication": lambda: __import__("frappe.client", fromlist=["get"]).get("Communication", self.comm_names[0]),
			"client.get secondary-linked Communication": lambda: __import__("frappe.client", fromlist=["get"]).get(
				"Communication", self.comm_names[1]
			),
			"Note timeline Communications": lambda: overrides.get_communications("Note", note),
			"report view of ToDo": reportview_get,
			"sidebar stats of ToDo": sidebar_stats,
			"link search on ToDo": with_response(lambda: search_link("ToDo", MARKER)),
			"form load of the ToDo itself": with_response(lambda: overrides.getdoc("ToDo", todo)),
			"My Open ToDos report": lambda: my_todos(),
			"client.get the ToDo's Comment": lambda: __import__("frappe.client", fromlist=["get"]).get("Comment", self.todo_comment),
			"client.get the ToDo's Version": lambda: __import__("frappe.client", fromlist=["get"]).get("Version", str(self.version.name)),
			"share the ToDo's Comment to self": lambda: frappe.share.add("Comment", self.todo_comment, frappe.session.user),
			"standard ToDo report": standard_todo_report,
			"Email Digest ToDo list": digest,
		}

	def _collect(self, user):
		frappe.set_user(user)
		seen = {}
		for label, call in self.surfaces().items():
			try:
				payload = call()
			except frappe.PermissionError:
				payload = "refused"
			except Exception as e:  # a surface failing outright is reported, not hidden
				payload = f"error: {type(e).__name__}"
			seen[label] = frappe.as_json(payload)
			frappe.clear_messages()
		return seen

	def test_outsiders_never_receive_the_marker(self):
		for role, user in self.outsiders.items():
			seen = self._collect(user)
			for label, text in seen.items():
				with self.subTest(role=role, surface=label):
					self.assertNotIn(MARKER, text)
					self.assertFalse(text.startswith('"error'), text)

	def test_positive_control_assignee_does_receive_it(self):
		seen = self._collect(self.assignee)
		self.assertIn(MARKER, seen["form load of the ToDo itself"])
		self.assertIn(MARKER, seen["My Open ToDos report"])
		self.assertIn(MARKER, seen["ToDo list with description"])

	def test_positive_control_administrator_sees_comment_and_version(self):
		seen = self._collect("Administrator")
		self.assertIn(MARKER, seen["client.get_list Version"])
		self.assertIn(MARKER, seen["client.get_list Comment"])
		self.assertIn(MARKER, seen["client.get the ToDo's Version"])
		self.assertIn(MARKER, seen["client.get_list Communication"])
		self.assertIn(MARKER, seen["client.get secondary-linked Communication"])

	def test_positive_control_participants_see_communications(self):
		# the assignee has no roles: they see ToDo emails through timelines
		seen = self._collect(self.assignee)
		self.assertIn(MARKER, seen["Note timeline Communications"])
		# a System Manager on the ToDo can also open them directly
		participant = _user("sysmgr-participant", ["System Manager"])
		frappe.db.set_value("ToDo", self.todo.name, "assigned_by", participant, update_modified=False)
		seen = self._collect(participant)
		self.assertIn(MARKER, seen["client.get secondary-linked Communication"])
		self.assertIn(MARKER, seen["client.get_list Communication"])

	def test_harness_would_catch_a_leak(self):
		"""Switch redaction off and put the description back in the comment: the harness must notice."""
		original = frappe.db.get_value("Comment", self.legacy_comment, "content")
		frappe.db.sql("update tabComment set content=%s where name=%s", (f"A assigned B: {MARKER}", self.legacy_comment))
		try:
			with patch("todo_privacy.redaction.redact_docinfo", side_effect=lambda docinfo, user: docinfo):
				seen = self._collect(self.outsiders["Employee"])
			self.assertIn(MARKER, seen["docinfo of the referenced Note"])
		finally:
			frappe.set_user("Administrator")
			frappe.db.sql("update tabComment set content=%s where name=%s", (original, self.legacy_comment))

	def test_any_new_assigned_comment_is_stored_without_text(self):
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Assigned", "reference_doctype": "Note", "reference_name": self.note.name,
			 "content": f"X assigned Y: {MARKER}"}
		).insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Comment", comment.name, "content"), "X assigned Y")
