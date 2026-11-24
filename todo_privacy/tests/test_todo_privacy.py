"""Staging acceptance tests for todo_privacy. Negative tests first.

Run on a STAGING site only, after install-app:

    bench --site <staging> set-config allow_tests true
    bench --site <staging> set-config mute_emails 1
    bench --site <staging> run-tests --app todo_privacy

Synthetic users (example.invalid) and records only, rolled back when the class
finishes. ToDos are inserted directly, never through assign_to.add, so no
assignment notifications are queued. HTTP-level attacks are covered by
tests/http_attacks.py, which needs a running web server.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from todo_privacy import overrides, policy
from todo_privacy.permissions import comment_has_permission

DOMAIN = "tp-test.example.invalid"
TEST_CUSTOM_ROLE = "ToDo Privacy Test Role"
BROAD_ROLES = ("System Manager", "Accounts User", "Employee", "HR User", TEST_CUSTOM_ROLE)
MARKER = "zz-secret-marker-7731"


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


def _todo(allocated_to=None, assigned_by=None, owner=None, description=MARKER, **extra):
	doc = frappe.get_doc(
		{"doctype": "ToDo", "description": description, "allocated_to": allocated_to, "assigned_by": assigned_by, **extra}
	)
	doc.insert(ignore_permissions=True)
	if owner:
		frappe.db.set_value("ToDo", doc.name, "owner", owner, update_modified=False)
	return doc


def _note(title, owner, public=1):
	frappe.set_user(owner)
	note = frappe.get_doc({"doctype": "Note", "title": title, "public": public}).insert()
	frappe.set_user("Administrator")
	return note


def _version(todo_name, marker=MARKER):
	return frappe.get_doc(
		{
			"doctype": "Version",
			"ref_doctype": "ToDo",
			"docname": todo_name,
			"data": json.dumps({"changed": [["description", "old", marker]]}),
		}
	).insert(ignore_permissions=True)


def _todo_comment(todo_name, content=MARKER):
	return frappe.get_doc(
		{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "ToDo", "reference_name": todo_name, "content": content}
	).insert(ignore_permissions=True)


def _raw_share(doctype, name, user=None, everyone=0):
	"""A share written straight to the table, as if it existed before install."""
	share = frappe.get_doc(
		{"doctype": "DocShare", "share_doctype": doctype, "share_name": name, "user": user, "everyone": everyone, "read": 1}
	)
	share.db_insert()
	return share


def _names(doctype, names, fields=None):
	"""Permission-checked list, bounded to the given synthetic records.

	The name filter keeps the permission checks in the query and limits it
	to the synthetic test records.
	"""
	rows = frappe.get_list(doctype, filters={"name": ("in", list(names))}, fields=fields or ["name"], limit_page_length=0)
	return [r.name for r in rows] if not fields else rows


def _communication(content=MARKER, reference=None, todo_links=(), medium="Other"):
	doc = frappe.get_doc(
		{
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": medium,
			"subject": "tp synthetic",
			"content": content,
			"reference_doctype": reference[0] if reference else None,
			"reference_name": reference[1] if reference else None,
		}
	)
	for todo in todo_links:
		doc.append("timeline_links", {"link_doctype": "ToDo", "link_name": todo})
	return doc.insert(ignore_permissions=True)


def _stored(name):
	return frappe.db.get_value("ToDo", name, ["allocated_to", "assigned_by", "description", "color"], as_dict=True)


class TestToDoPrivacy(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.flags.mute_emails = True
		frappe.set_user("Administrator")
		if not frappe.db.exists("Role", TEST_CUSTOM_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": TEST_CUSTOM_ROLE}).insert(ignore_permissions=True)
		cls.assignee = _user("assignee")
		cls.assigner = _user("assigner")
		cls.outsider_by_role = {
			role: _user("outsider-" + role.lower().replace(" ", "-"), [role]) for role in BROAD_ROLES
		}
		cls.sysmgr = cls.outsider_by_role["System Manager"]
		cls.plain = _user("plain")
		cls.todo = _todo(allocated_to=cls.assignee, assigned_by=cls.assigner, owner=cls.assigner)

	def setUp(self):
		frappe.set_user("Administrator")
		self.assertEqual(policy.check(), [])

	def tearDown(self):
		frappe.set_user("Administrator")
		policy.enforce_permissions(reason="test teardown")

	# ---------------------------------------------------------------- baseline
	def test_00_baseline_and_wiring(self):
		self.assertEqual(policy.check(), [])
		for doctype, info in policy.hooks_active().items():
			self.assertTrue(info["evaluated_first"], f"{doctype}: {info}")
		for original, info in policy.overrides_active().items():
			self.assertTrue(info["ours"], f"{original} resolves to {info['resolves_to']}")
		self.assertEqual(policy.auditor_holders(), [])

	# ----------------------------------------------------------- negatives first
	def test_01_unrelated_users_cannot_see_todo(self):
		for role, user in self.outsider_by_role.items():
			with self.subTest(role=role):
				frappe.set_user(user)
				self.assertNotIn(self.todo.name, frappe.get_list("ToDo", pluck="name", limit_page_length=0))
				for ptype in ("read", "write", "delete", "share", "print", "email"):
					self.assertFalse(frappe.has_permission("ToDo", ptype, doc=self.todo.name), ptype)
				count = frappe.get_list("ToDo", fields=["count(name) as c"], filters={"name": self.todo.name})[0].c
				self.assertEqual(count, 0)

	def test_02_unrelated_user_cannot_search_or_report_todo(self):
		from frappe.desk.reportview import get as reportview_get
		from frappe.desk.search import search_link

		frappe.set_user(self.outsider_by_role["Employee"])
		search_link("ToDo", self.todo.name)
		self.assertNotIn(self.todo.name, [r.get("value") for r in (frappe.response.get("results") or [])])
		frappe.local.form_dict = frappe._dict(
			doctype="ToDo", fields=json.dumps(["`tabToDo`.`name`"]), filters=json.dumps({"name": self.todo.name})
		)
		result = reportview_get()
		self.assertFalse(result.get("values") if isinstance(result, dict) else result)

	def test_03_share_is_refused_for_everyone(self):
		for user in (self.assigner, "Administrator"):
			with self.subTest(user=user):
				frappe.set_user(user)
				self.assertRaises(frappe.PermissionError, frappe.share.add, "ToDo", self.todo.name, self.plain)
		frappe.set_user("Administrator")
		note = _note("tp share repoint", self.plain)
		frappe.share.add("Note", note.name, self.assignee)
		share = frappe.get_doc("DocShare", {"share_doctype": "Note", "share_name": note.name})
		share.share_doctype, share.share_name = "ToDo", self.todo.name
		self.assertRaises(frappe.PermissionError, share.save, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "ToDo"}))

	# Blocker 1: forged participant fields -------------------------------------
	def _forged(self, **changes):
		doc = frappe.get_doc("ToDo", self.todo.name).as_dict()
		doc.update(changes)
		return json.loads(frappe.as_json(doc))

	def test_04_forged_assigned_by_is_refused_through_client_save(self):
		from frappe.client import save

		before = _stored(self.todo.name)
		frappe.set_user(self.plain)
		for changes in (
			{"assigned_by": self.plain, "description": "tampered"},
			{"allocated_to": self.plain, "description": "tampered"},
			{"assigned_by": self.plain, "allocated_to": self.plain},
		):
			with self.subTest(changes=list(changes)):
				self.assertRaises(frappe.PermissionError, save, self._forged(**changes))
		frappe.set_user("Administrator")
		self.assertEqual(_stored(self.todo.name), before)

	def test_05_forged_assigned_by_is_refused_through_savedocs_and_delete(self):
		from frappe.client import delete
		from frappe.desk.form.save import savedocs

		frappe.set_user(self.plain)
		forged = self._forged(assigned_by=self.plain, description="tampered")
		self.assertRaises(frappe.PermissionError, savedocs, json.dumps(forged), "Save")
		self.assertRaises(frappe.PermissionError, delete, "ToDo", self.todo.name)
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("ToDo", self.todo.name))
		self.assertEqual(_stored(self.todo.name).description, MARKER)

	def test_06_assignee_cannot_rewrite_who_assigned_it(self):
		from frappe.client import save

		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		frappe.set_user(self.assignee)
		doc = frappe.get_doc("ToDo", todo.name).as_dict()
		doc["assigned_by"] = self.plain
		self.assertRaises(frappe.PermissionError, save, json.loads(frappe.as_json(doc)))

	# Blocker 3: re-link must change nothing else -------------------------------
	def test_07_relink_with_any_other_change_is_refused(self):
		note_a = _note("tp relink a", self.plain)
		note_b = _note("tp relink b", self.plain)
		for field, value in (("description", "changed"), ("color", "#ff0000"), ("priority", "High"), ("status", "Closed")):
			with self.subTest(field=field):
				todo = _todo(
					allocated_to=self.assignee,
					assigned_by=self.assigner,
					reference_type="Note",
					reference_name=note_a.name,
				)
				frappe.set_user(self.plain)
				doc = frappe.get_doc("ToDo", todo.name)
				doc.reference_name = note_b.name
				doc.set(field, value)
				self.assertRaises(frappe.PermissionError, doc.save)
				frappe.set_user("Administrator")
				self.assertEqual(frappe.db.get_value("ToDo", todo.name, "reference_name"), note_a.name)

	# Blocker 2: timeline, comments and versions --------------------------------
	def test_08_new_assignment_comment_has_no_description(self):
		note = _note("tp comment", self.plain)
		frappe.set_user(self.assigner)
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": MARKER,
				"allocated_to": self.assignee,
				"reference_type": "Note",
				"reference_name": note.name,
			}
		).insert()
		frappe.set_user("Administrator")
		comments = frappe.get_all(
			"Comment", filters={"reference_doctype": "Note", "reference_name": note.name, "comment_type": "Assigned"}, pluck="content"
		)
		self.assertTrue(comments)
		self.assertTrue(all(MARKER not in (c or "") for c in comments), comments)

	def test_09_docinfo_hides_descriptions_and_legacy_comment_text(self):
		note = _note("tp docinfo", self.plain)
		_todo(allocated_to=self.assignee, assigned_by=self.assigner, reference_type="Note", reference_name=note.name)
		legacy = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Assigned", "reference_doctype": "Note", "reference_name": note.name, "content": "x"}
		).insert(ignore_permissions=True)
		# a pre-install comment still carrying the description (raw SQL: writes are now stripped)
		frappe.db.sql("update tabComment set content=%s where name=%s", (f"Someone assigned Someone Else: {MARKER}", legacy.name))
		frappe.set_user(self.plain)
		frappe.local.response = frappe._dict(docs=[])
		overrides.getdoc("Note", note.name)
		docinfo = frappe.response["docinfo"]
		self.assertTrue(docinfo["assignments"], "assignee list should stay visible")
		self.assertNotIn(MARKER, frappe.as_json(docinfo))

	def test_10_existing_assignment_comments_are_sanitised(self):
		note = _note("tp legacy comment", self.plain)
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Assigned", "reference_doctype": "Note", "reference_name": note.name, "content": "x"}
		).insert(ignore_permissions=True)
		frappe.db.sql("update tabComment set content=%s where name=%s", (f"A assigned B: {MARKER}", comment.name))
		self.assertGreaterEqual(policy.sanitise_assignment_comments(dry_run=True)["candidates"], 1)
		with patch("todo_privacy.snapshot.write", return_value="/dev/null"):
			policy.sanitise_assignment_comments(reason="test")
		self.assertEqual(frappe.db.get_value("Comment", comment.name, "content"), "A assigned B")

	def test_11_system_manager_cannot_read_todo_comments_or_versions(self):
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "ToDo", "reference_name": self.todo.name, "content": MARKER}
		).insert(ignore_permissions=True)
		# Saves in test mode do not write Version rows, so seed one explicitly.
		version = _version(self.todo.name)
		frappe.set_user("Administrator")
		self.assertIn(
			version.name, _names("Version", [version.name]),
			"positive control: Administrator must see the seeded Version",
		)
		frappe.set_user(self.sysmgr)
		comments = _names("Comment", [comment.name])
		self.assertNotIn(comment.name, comments)
		versions = _names("Version", [version.name])
		self.assertNotIn(version.name, versions)
		self.assertFalse(frappe.has_permission("Version", "read", doc=str(version.name)))
		self.assertFalse(frappe.has_permission("Comment", "read", doc=comment.name))
		# Participants reach ToDo comments through the ToDo form, not the Comment list
		frappe.set_user(self.assignee)
		self.assertIsNone(comment_has_permission(frappe.get_doc("Comment", comment.name)))

	def test_12_open_activities_require_read_and_redact(self):
		if "erpnext" not in frappe.get_installed_apps():
			self.skipTest("ERPNext not installed")
		note = _note("tp crm", self.assigner)
		_todo(allocated_to=self.assignee, assigned_by=self.assigner, reference_type="Note", reference_name=note.name)
		frappe.set_user(self.plain)
		data = overrides.get_open_activities("Note", note.name)
		self.assertNotIn(MARKER, frappe.as_json(data))
		frappe.set_user(self.assignee)
		self.assertIn(MARKER, frappe.as_json(overrides.get_open_activities("Note", note.name)))

	def test_13_group_by_count_only_counts_readable_records(self):
		frappe.set_user(self.assigner)
		private = frappe.get_doc({"doctype": "Note", "title": "tp private", "public": 0}).insert()
		frappe.set_user("Administrator")
		_todo(allocated_to=self.assignee, assigned_by=self.assigner, reference_type="Note", reference_name=private.name)
		frappe.set_user(self.plain)
		self.assertEqual(overrides.get_group_by_count("Note", json.dumps({"name": private.name}), "assigned_to"), [])

	# Blocker 4: exact baseline -----------------------------------------------
	def test_14_any_deviation_from_the_baseline_is_detected_and_fixed(self):
		def all_row():
			return frappe.db.get_value("Custom DocPerm", {"parent": "ToDo", "role": "All"})

		cases = {
			"All read off": lambda: frappe.db.set_value("Custom DocPerm", all_row(), "read", 0),
			"All write off": lambda: frappe.db.set_value("Custom DocPerm", all_row(), "write", 0),
			"All owner-only": lambda: frappe.db.set_value("Custom DocPerm", all_row(), "if_owner", 1),
			"All export on": lambda: frappe.db.set_value("Custom DocPerm", all_row(), "export", 1),
			"duplicate All": lambda: frappe.get_doc(
				{"doctype": "Custom DocPerm", "parent": "ToDo", "parenttype": "DocType", "parentfield": "permissions", "role": "All", "read": 1}
			).db_insert(),
			"extra Employee": lambda: frappe.get_doc(
				{"doctype": "Custom DocPerm", "parent": "ToDo", "parenttype": "DocType", "parentfield": "permissions", "role": "Employee", "read": 1}
			).db_insert(),
			"Auditor removed": lambda: frappe.db.delete("Custom DocPerm", {"parent": "ToDo", "role": policy.AUDITOR_ROLE}),
		}
		for label, damage in cases.items():
			with self.subTest(label):
				damage()
				self.assertTrue(policy.check(), label)
				policy.enforce_permissions(reason="test")
				self.assertEqual(policy.check(), [], label)

	def test_15_permission_manager_refuses_todo_changes(self):
		calls = (
			lambda: overrides.permission_manager_add("ToDo", "Employee", 0),
			lambda: overrides.permission_manager_update("ToDo", "All", 0, "share", "1"),
			lambda: overrides.permission_manager_update("ToDo", "All", 0, "read", "0"),
			lambda: overrides.permission_manager_remove("ToDo", policy.AUDITOR_ROLE, 0),
			lambda: overrides.permission_manager_reset("ToDo"),
		)
		before = frappe.db.sql("select * from `tabCustom DocPerm` where parent='ToDo' order by idx", as_dict=True)
		for i, call in enumerate(calls):
			with self.subTest(call=i):
				self.assertRaises(frappe.PermissionError, call)
		after = frappe.db.sql("select * from `tabCustom DocPerm` where parent='ToDo' order by idx", as_dict=True)
		self.assertEqual(before, after)
		self.assertEqual(policy.check(), [])

	def test_15b_permission_manager_still_works_for_other_doctypes(self):
		from frappe.core.page.permission_manager import permission_manager as pm

		with patch.object(pm, "add") as add:
			overrides.permission_manager_add("Note", "Employee", 0)
		add.assert_called_once()

	# Blocker 6: Administrator sees everything --------------------------------
	def test_16_administrator_lists_every_todo(self):
		frappe.set_user("Administrator")
		listed = frappe.get_list("ToDo", fields=["count(name) as c"])[0].c
		self.assertEqual(listed, frappe.db.count("ToDo"))

	def test_17_auditor_reads_all_but_cannot_change(self):
		auditor = _user("auditor", [policy.AUDITOR_ROLE])
		frappe.set_user(auditor)
		self.assertIn(self.todo.name, frappe.get_list("ToDo", pluck="name", limit_page_length=0))
		self.assertTrue(frappe.has_permission("ToDo", "read", doc=self.todo.name))
		for ptype in ("write", "delete", "share", "create"):
			self.assertFalse(frappe.has_permission("ToDo", ptype, doc=self.todo.name), ptype)

	# Blocker 5: report and digest -------------------------------------------
	def test_18_my_open_todos_report_and_digest_include_assignee_only_rows(self):
		from todo_privacy.todo_privacy.report.my_open_todos.my_open_todos import execute

		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		frappe.set_user(self.assignee)
		_cols, rows = execute()
		self.assertIn(todo.name, [r.name for r in rows])
		frappe.set_user(self.plain)
		self.assertNotIn(todo.name, [r.name for r in execute()[1]])
		if "erpnext" in frappe.get_installed_apps():
			digest = frappe.new_doc("Email Digest")
			self.assertEqual(type(digest).__module__, "todo_privacy.email_digest")
			self.assertIn(todo.name, [t.name for t in digest.get_todo_list(self.assignee)])
			self.assertNotIn(todo.name, [t.name for t in digest.get_todo_list(self.plain)])

	# --------------------------------------------------------------- positives
	def test_20_assignee_and_assigner_can_see_and_edit(self):
		for user in (self.assignee, self.assigner):
			with self.subTest(user=user):
				frappe.set_user(user)
				self.assertIn(self.todo.name, frappe.get_list("ToDo", pluck="name", limit_page_length=0))
				self.assertTrue(frappe.has_permission("ToDo", "write", doc=self.todo.name))

	def test_21_creator_records_themselves_as_assigner(self):
		frappe.set_user(self.outsider_by_role["Employee"])
		mine = frappe.get_doc({"doctype": "ToDo", "description": "my own note"}).insert()
		self.assertEqual(mine.assigned_by, frappe.session.user)
		forged = frappe.get_doc(
			{"doctype": "ToDo", "description": "pretend the boss set this", "allocated_to": frappe.session.user, "assigned_by": self.assigner}
		).insert()
		self.assertEqual(forged.assigned_by, frappe.session.user)
		self.assertIn(mine.name, frappe.get_list("ToDo", pluck="name", limit_page_length=0))

	def test_22_assignee_can_hand_a_todo_on(self):
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		frappe.set_user(self.assignee)
		doc = frappe.get_doc("ToDo", todo.name)
		doc.allocated_to = self.plain
		doc.save()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "allocated_to"), self.plain)

	def test_23_relink_only_is_allowed_with_reference_access(self):
		note_a = _note("tp relink ok a", self.plain)
		note_b = _note("tp relink ok b", self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner, reference_type="Note", reference_name=note_a.name)
		frappe.set_user(self.plain)
		doc = frappe.get_doc("ToDo", todo.name)
		doc.reference_name = note_b.name
		doc.save()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "reference_name"), note_b.name)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), MARKER)

	def test_24_assignment_removal_by_reference_user_still_works(self):
		from frappe.desk.form.assign_to import remove

		note = _note("tp remove", self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner, reference_type="Note", reference_name=note.name)
		frappe.set_user(self.plain)
		with patch("frappe.desk.form.assign_to.notify_assignment"):
			remove("Note", note.name, self.assignee)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "status"), "Cancelled")

	def test_25_ignore_permissions_insert_records_creator(self):
		frappe.set_user(self.plain)
		doc = frappe.get_doc({"doctype": "ToDo", "description": "system made", "allocated_to": self.assignee})
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.assigned_by, self.plain)

	def test_26_backfill_only_fills_empty_values(self):
		todo = _todo(allocated_to=self.assignee)
		frappe.db.set_value("ToDo", todo.name, {"assigned_by": None, "owner": self.plain}, update_modified=False)
		kept = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		frappe.db.set_value("ToDo", kept.name, "owner", self.plain, update_modified=False)
		with patch("todo_privacy.snapshot.write", return_value="/dev/null"):
			result = policy.backfill_assigned_by(reason="test")
		self.assertGreaterEqual(result["changed"], 1)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "assigned_by"), self.plain)
		self.assertEqual(frappe.db.get_value("ToDo", kept.name, "assigned_by"), self.assigner)

	def test_27_assigner_can_reassign(self):
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		frappe.set_user(self.assigner)
		doc = frappe.get_doc("ToDo", todo.name)
		doc.allocated_to = self.plain
		doc.save()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, ["allocated_to", "assigned_by"]), (self.plain, self.assigner))

	def test_28_daily_check_repairs_drift(self):
		from todo_privacy.install import daily_check

		frappe.get_doc(
			{"doctype": "Custom DocPerm", "parent": "ToDo", "parenttype": "DocType", "parentfield": "permissions", "role": "Employee", "read": 1}
		).db_insert()
		self.assertTrue(policy.check())
		with patch("todo_privacy.snapshot.write", return_value="/dev/null"), patch.object(frappe.db, "commit"):
			daily_check()
		self.assertEqual(policy.check(), [])

	def test_29_second_run_waits_for_the_lock(self):
		from frappe.database import get_db

		from todo_privacy import install

		other = get_db(user=frappe.conf.db_name, password=frappe.conf.db_password)
		other.connect()
		try:
			self.assertEqual(other.sql("select get_lock(%s, 0)", (install._lock_name(),))[0][0], 1)
			with patch.object(frappe.db, "commit"):
				self.assertIsNone(install._apply("test while locked"))
		finally:
			other.sql("select release_lock(%s)", (install._lock_name(),))
			other.close()
		with patch.object(frappe.db, "commit"):
			self.assertIsNotNone(install._apply("test after unlock"))

	def test_30_snapshot_provenance_is_checked(self):
		import os

		from todo_privacy import snapshot

		path = snapshot.write("permissions", {"reason": "test", "custom_docperm": [], "docperm": [], "docshare": []})
		try:
			snap = json.load(open(path))
			for key in ("site", "server_time", "app_version", "frappe_version", "sha256"):
				self.assertTrue(snap.get(key), key)
			self.assertEqual(snapshot._read(path, "permissions")["reason"], "test")

			snap["app_version"] = "0.0.1"
			json.dump(snap, open(path, "w"))
			self.assertRaises(frappe.ValidationError, snapshot._read, path, "permissions")
			self.assertEqual(snapshot._read(path, "permissions", force=True)["reason"], "test")

			snap["custom_docperm"] = [{"role": "Employee"}]
			json.dump(snap, open(path, "w"))
			self.assertRaises(frappe.ValidationError, snapshot._read, path, "permissions", force=True)
		finally:
			os.remove(path)

	# Found by the staging agent's extra checks on 1.2.0 ---------------------
	def test_31_comment_cannot_be_repointed_away_from_a_todo(self):
		from frappe.client import set_value

		note = _note("tp repoint comment", self.sysmgr)
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "ToDo", "reference_name": self.todo.name, "content": MARKER}
		).insert(ignore_permissions=True)
		frappe.set_user(self.sysmgr)
		self.assertRaises(
			frappe.PermissionError, set_value, "Comment", comment.name, {"reference_doctype": "Note", "reference_name": note.name}
		)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Comment", comment.name, "reference_doctype"), "ToDo")

	def test_32_comment_cannot_be_attached_to_someone_elses_todo(self):
		note = _note("tp attach comment", self.sysmgr)
		frappe.set_user(self.sysmgr)
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "Note", "reference_name": note.name, "content": "hi"}
		).insert()
		comment.reference_doctype, comment.reference_name = "ToDo", self.todo.name
		self.assertRaises(frappe.PermissionError, comment.save)

	def test_33_relink_cannot_change_owner(self):
		note_a = _note("tp owner a", self.plain)
		note_b = _note("tp owner b", self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner, reference_type="Note", reference_name=note_a.name)
		frappe.set_user(self.plain)
		doc = frappe.get_doc("ToDo", todo.name)
		doc.reference_name = note_b.name
		doc.owner = self.plain
		self.assertRaises(frappe.PermissionError, doc.save)

	def test_34_snapshot_without_checksum_is_refused(self):
		import os

		from todo_privacy import snapshot

		path = snapshot.write("permissions", {"reason": "test", "custom_docperm": [], "docperm": [], "docshare": []})
		try:
			snap = json.load(open(path))
			snap.pop("sha256")
			snap["custom_docperm"] = [{"role": "Employee"}]
			json.dump(snap, open(path, "w"))
			self.assertRaises(frappe.ValidationError, snapshot._read, path, "permissions")
			self.assertRaises(frappe.ValidationError, snapshot._read, path, "permissions", force=True)
		finally:
			os.remove(path)

	# Staging finding on 1.2.1: shares on records linked to a ToDo -------------
	def _sysmgr_participant(self):
		user = _user("participant-sysmgr", ["System Manager"])
		todo = _todo(allocated_to=user, assigned_by=self.assigner)
		return user, todo

	def test_35_comment_on_a_todo_cannot_be_shared(self):
		user, todo = self._sysmgr_participant()
		comment = _todo_comment(todo.name)
		for actor in (user, "Administrator"):
			with self.subTest(actor=actor):
				frappe.set_user(actor)
				self.assertRaises(frappe.PermissionError, frappe.share.add, "Comment", comment.name, self.plain)
				self.assertRaises(frappe.PermissionError, frappe.share.add, "Comment", comment.name, None, everyone=1)
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "Comment", "share_name": comment.name}))

	def test_36_version_communication_and_file_of_a_todo_cannot_be_shared(self):
		user, todo = self._sysmgr_participant()
		version = _version(todo.name)
		communication = frappe.get_doc(
			{"doctype": "Communication", "communication_type": "Comment", "subject": "tp", "content": MARKER,
			 "reference_doctype": "ToDo", "reference_name": todo.name}
		).insert(ignore_permissions=True)
		attachment = frappe.get_doc(
			{"doctype": "File", "file_name": "tp-share.txt", "content": MARKER, "is_private": 1,
			 "attached_to_doctype": "ToDo", "attached_to_name": todo.name}
		).insert(ignore_permissions=True)
		frappe.set_user(user)
		# Version names are integers in v14; HTTP callers always pass strings
		for doctype, name in (("Version", str(version.name)), ("Communication", communication.name), ("File", attachment.name)):
			with self.subTest(doctype=doctype):
				self.assertRaises(frappe.PermissionError, frappe.share.add, doctype, name, self.plain)

	def test_37_existing_share_cannot_be_repointed_at_todo_content(self):
		user, todo = self._sysmgr_participant()
		comment = _todo_comment(todo.name)
		note = _note("tp share repoint linked", self.plain)
		frappe.share.add("Note", note.name, self.assignee)
		share = frappe.get_doc("DocShare", {"share_doctype": "Note", "share_name": note.name})
		share.share_doctype, share.share_name = "Comment", comment.name
		self.assertRaises(frappe.PermissionError, share.save, ignore_permissions=True)

	def test_38_shared_record_cannot_be_moved_onto_a_todo(self):
		user, todo = self._sysmgr_participant()
		note = _note("tp shared comment move", self.plain)
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "Note", "reference_name": note.name, "content": "hi"}
		).insert(ignore_permissions=True)
		frappe.share.add("Comment", comment.name, self.plain)
		frappe.set_user(user)
		doc = frappe.get_doc("Comment", comment.name)
		doc.reference_doctype, doc.reference_name = "ToDo", todo.name
		self.assertRaises(frappe.PermissionError, doc.save)

	def test_39_sharing_unrelated_records_still_works(self):
		note = _note("tp unrelated share", self.plain)
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "Note", "reference_name": note.name, "content": "hi"}
		).insert(ignore_permissions=True)
		frappe.set_user(self.sysmgr)
		frappe.share.add("Note", note.name, self.assignee)
		frappe.share.add("Comment", comment.name, self.assignee)
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("DocShare", {"share_doctype": "Note", "share_name": note.name}))
		self.assertTrue(frappe.db.exists("DocShare", {"share_doctype": "Comment", "share_name": comment.name}))
		self.assertEqual(policy.check(), [])

	def test_40_pre_existing_linked_shares_are_detected_and_removed(self):
		_user_sm, todo = self._sysmgr_participant()
		comment = _todo_comment(todo.name)
		version = _version(todo.name)
		_raw_share("Comment", comment.name, user=self.plain)
		_raw_share("Comment", comment.name, user=self.sysmgr)
		_raw_share("Version", str(version.name), everyone=1)

		# positive controls: shares really do override the participant rule.
		# A user with no Comment role can open the shared comment...
		frappe.set_user(self.plain)
		self.assertTrue(frappe.has_permission("Comment", "read", doc=comment.name))
		# ...and a user who holds a Comment role (System Manager) also sees it in
		# lists, because Frappe ORs shares around every list condition.
		frappe.set_user(self.sysmgr)
		self.assertIn(comment.name, _names("Comment", [comment.name]))
		self.assertIn(version.name, _names("Version", [version.name]))
		frappe.set_user("Administrator")

		problems = policy.check()
		self.assertTrue(any("DocShare" in p for p in problems), problems)
		policy.enforce_permissions(reason="test")
		self.assertEqual(policy.check(), [])

		frappe.set_user(self.plain)
		self.assertFalse(frappe.has_permission("Comment", "read", doc=comment.name))
		frappe.set_user(self.sysmgr)
		self.assertFalse(frappe.has_permission("Comment", "read", doc=comment.name))
		self.assertNotIn(comment.name, _names("Comment", [comment.name]))
		self.assertNotIn(version.name, _names("Version", [version.name]))

	def test_41_snapshot_covers_linked_shares(self):
		from todo_privacy import snapshot

		_user_sm, todo = self._sysmgr_participant()
		comment = _todo_comment(todo.name)
		share = _raw_share("Comment", comment.name, user=self.plain)
		state = snapshot._state()
		self.assertEqual(state["share_scope"], snapshot.SHARE_SCOPE)
		self.assertIn(share.name, [r.name for r in state["docshare"]])

	# Staging findings on 1.2.2: Communication ---------------------------------
	# Frappe lets the "All" role read only Communications the user created, so
	# positive controls use a System Manager who is on the ToDo.
	def test_42_communication_on_a_todo_is_hidden_from_lists_and_documents(self):
		participant, todo = self._sysmgr_participant()
		comm = _communication(reference=("ToDo", todo.name))
		frappe.set_user("Administrator")
		self.assertIn(comm.name, _names("Communication", [comm.name]), "positive control")
		for user in (self.sysmgr, self.plain):
			with self.subTest(user=user):
				frappe.set_user(user)
				rows = _names("Communication", [comm.name], fields=["name", "content"])
				self.assertEqual(rows, [])
				self.assertFalse(frappe.has_permission("Communication", "read", doc=comm.name))
				count = frappe.get_list("Communication", fields=["count(name) as c"], filters={"name": comm.name})[0].c
				self.assertEqual(count, 0)
		frappe.set_user(participant)
		self.assertTrue(frappe.has_permission("Communication", "read", doc=comm.name))
		self.assertIn(comm.name, _names("Communication", [comm.name]))

	def test_43_secondary_todo_link_is_protected_like_a_primary_one(self):
		participant, todo = self._sysmgr_participant()
		note = _note("tp comm secondary", self.plain)
		comm = _communication(reference=("Note", note.name), todo_links=[todo.name])
		self.assertTrue(frappe.db.exists("Communication Link", {"parent": comm.name, "link_doctype": "ToDo"}))
		for user in (self.sysmgr, self.plain):
			with self.subTest(user=user):
				frappe.set_user(user)
				self.assertFalse(frappe.has_permission("Communication", "read", doc=comm.name))
				self.assertEqual(_names("Communication", [comm.name]), [])
		frappe.set_user(participant)
		self.assertTrue(frappe.has_permission("Communication", "read", doc=comm.name))
		self.assertIn(comm.name, _names("Communication", [comm.name]))

	def test_44_secondary_linked_communication_cannot_be_shared(self):
		participant, todo = self._sysmgr_participant()
		note = _note("tp comm share", self.plain)
		comm = _communication(reference=("Note", note.name), todo_links=[todo.name])
		frappe.set_user(participant)
		self.assertRaises(frappe.PermissionError, frappe.share.add, "Communication", comm.name, self.plain)
		self.assertRaises(frappe.PermissionError, frappe.share.add, "Communication", comm.name, None, everyone=1)

	def test_45_shared_communication_cannot_gain_a_todo_link(self):
		_participant, todo = self._sysmgr_participant()
		note = _note("tp comm add link", self.plain)
		comm = _communication(content="ordinary", reference=("Note", note.name))
		frappe.share.add("Communication", comm.name, self.plain)
		doc = frappe.get_doc("Communication", comm.name)
		doc.append("timeline_links", {"link_doctype": "ToDo", "link_name": todo.name})
		self.assertRaises(frappe.PermissionError, doc.save, ignore_permissions=True)

	def test_46_pre_existing_share_on_secondary_linked_communication(self):
		note = _note("tp comm drift", self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		comm = _communication(reference=("Note", note.name), todo_links=[todo.name])
		_raw_share("Communication", comm.name, user=self.plain)
		from todo_privacy import linked, snapshot

		self.assertIn(comm.name, [r.share_name for r in linked.scoped_shares()])
		self.assertIn(comm.name, [r.share_name for r in snapshot._state()["docshare"]])
		self.assertTrue(policy.check())
		policy.enforce_permissions(reason="test")
		self.assertEqual(policy.check(), [])
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "Communication", "share_name": comm.name}))

	def test_47_note_timeline_hides_todo_linked_communications(self):
		note = _note("tp comm timeline", self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		comm = _communication(reference=("Note", note.name), todo_links=[todo.name])
		ordinary = _communication(content="ordinary note email", reference=("Note", note.name))
		frappe.set_user(self.plain)
		frappe.local.response = frappe._dict(docs=[])
		overrides.getdoc("Note", note.name)
		names = [c.get("name") for c in frappe.response["docinfo"]["communications"]]
		self.assertNotIn(comm.name, names)
		self.assertIn(ordinary.name, names, "unrelated Communications stay visible")
		self.assertNotIn(MARKER, frappe.as_json(overrides.get_communications("Note", note.name)))
		frappe.set_user(self.assignee)
		self.assertIn(comm.name, [c.get("name") for c in overrides.get_communications("Note", note.name)])

	# Staging P2 findings on 1.2.3 ---------------------------------------------
	def _timeline_fixture(self, hidden=21, ordinary=45):
		"""A public Note: `hidden` newest Communications linked to a private ToDo,
		then `ordinary` older ones. Dates are explicit so order is deterministic."""
		from frappe.utils import add_to_date, now_datetime

		note = _note("tp paging " + frappe.generate_hash(length=6), self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		base = now_datetime()
		hidden_names, ordinary_names = [], []
		for i in range(hidden):
			c = _communication(content=MARKER, reference=("Note", note.name), todo_links=[todo.name])
			frappe.db.set_value("Communication", c.name, "communication_date", add_to_date(base, minutes=-i), update_modified=False)
			hidden_names.append(c.name)
		for i in range(ordinary):
			c = _communication(content=f"ordinary {i}", reference=("Note", note.name))
			frappe.db.set_value(
				"Communication", c.name, "communication_date", add_to_date(base, minutes=-(hidden + i)), update_modified=False
			)
			ordinary_names.append(c.name)
		return note, hidden_names, ordinary_names

	def _walk_timeline(self, note):
		"""Do what the browser does: docinfo first, then Load More while a page is full."""
		frappe.local.response = frappe._dict(docs=[])
		overrides.getdoc("Note", note.name)
		docinfo = frappe.response["docinfo"]
		shown = (docinfo["communications"] or []) + (docinfo["automated_messages"] or [])
		pages = [len(shown)]
		while len(shown) > 20:  # form_timeline.js offers Load More only above 20
			start = len(shown) - 1  # form_timeline.js: start = shown - 1
			shown = shown[:-1]
			page = overrides.get_communications("Note", note.name, start=start, limit=21)
			pages.append(len(page))
			shown += page
			if len(page) <= 20:
				break
		return [c.get("name") for c in shown], pages

	def test_48_hidden_emails_do_not_block_older_ordinary_ones(self):
		note, hidden, ordinary = self._timeline_fixture()
		frappe.set_user(self.plain)
		names, pages = self._walk_timeline(note)
		self.assertEqual(names, ordinary, f"pages={pages}")  # every ordinary email, in order, once
		self.assertFalse(set(names) & set(hidden))
		self.assertEqual(pages[0], 21, "first page is a full visible page, so Load More appears")

	def test_49_participant_and_administrator_still_see_everything_on_the_timeline(self):
		note, hidden, ordinary = self._timeline_fixture(hidden=5, ordinary=30)
		for user in (self.assignee, "Administrator"):
			with self.subTest(user=user):
				frappe.set_user(user)
				names, _pages = self._walk_timeline(note)
				self.assertEqual(names, hidden + ordinary)

	def _share_on_todo_comment(self):
		_user_sm, todo = self._sysmgr_participant()
		comment = _todo_comment(todo.name)
		return _raw_share("Comment", comment.name, user=self.plain)

	def _secondary_only_share(self):
		note = _note("tp v1 secondary " + frappe.generate_hash(length=6), self.plain)
		todo = _todo(allocated_to=self.assignee, assigned_by=self.assigner)
		comm = _communication(content="x", reference=("Note", note.name), todo_links=[todo.name])
		return _raw_share("Communication", comm.name, user=self.plain)

	def _write_snapshot(self, scope, docshare):
		from todo_privacy import snapshot

		state = snapshot._state()
		body = {k: v for k, v in state.items() if k not in ("docshare", "share_scope")}
		body["docshare"] = docshare
		if scope is not None:
			body["share_scope"] = scope
		return snapshot.write("permissions", {"reason": "test", **body})

	def test_50_snapshot_from_1_2_2_restores_in_its_own_scope(self):
		import os

		from todo_privacy import linked, snapshot

		v1_share = self._share_on_todo_comment()
		secondary = self._secondary_only_share()  # outside 1.2.2's scope
		path = self._write_snapshot(linked.SCOPE_V1, linked.scoped_shares(linked.SCOPE_V1))
		try:
			self.assertEqual(snapshot.compare_permissions(path), [], "identical before any change")
			frappe.db.delete("DocShare", {"name": v1_share.name})
			self.assertTrue(snapshot.compare_permissions(path))
			with patch.object(frappe.db, "commit"):
				result = snapshot.restore_permissions(path)
			self.assertTrue(result["verified_identical"])
			self.assertTrue(frappe.db.exists("DocShare", v1_share.name), "1.2.2 share restored")
			self.assertTrue(frappe.db.exists("DocShare", secondary.name), "share outside 1.2.2's scope left alone")
		finally:
			os.remove(path)

	def test_51_snapshot_without_scope_is_direct_todo_shares_only(self):
		import os

		from todo_privacy import linked, snapshot

		linked_share = self._share_on_todo_comment()
		path = self._write_snapshot(None, linked.scoped_shares(linked.SCOPE_DIRECT))
		try:
			self.assertEqual(snapshot.compare_permissions(path), [])
			with patch.object(frappe.db, "commit"):
				snapshot.restore_permissions(path)
			self.assertTrue(frappe.db.exists("DocShare", linked_share.name), "linked share untouched by a direct-scope restore")
		finally:
			os.remove(path)

	def test_52_unknown_scope_is_refused_before_any_write(self):
		import os

		from todo_privacy import snapshot

		path = self._write_snapshot("todo-and-everything-v9", [])
		before = frappe.db.sql("select * from `tabCustom DocPerm` where parent='ToDo' order by idx", as_dict=True)
		try:
			with patch.object(frappe.db, "commit"):
				self.assertRaises(frappe.ValidationError, snapshot.restore_permissions, path)
			after = frappe.db.sql("select * from `tabCustom DocPerm` where parent='ToDo' order by idx", as_dict=True)
			self.assertEqual(before, after)
		finally:
			os.remove(path)
